"""GitHub REST API client.

Talks to the GitHub Issues API and produces the two lists the rest of
the pipeline needs: §1 (issues opened in the past N days) and §2
(open issues with new comments in the past N days). Two real
gotchas worth naming up front, both handled here:

1. **GitHub's `/issues` endpoint also returns pull requests.** Items
   with a ``pull_request`` key are dropped before any further
   processing.

2. **The `since=` query param filters by ``updated_at``, not
   ``created_at``.** The endpoint returns issues that have been
   *touched* (commented, labelled, edited, …) since the cutoff —
   not just newly opened ones. So §1 vs §2 is split client-side
   using ``created_at``.

Authentication: reads ``GITHUB_TOKEN`` from the process environment
when set; works anonymously otherwise (at the much-lower 60 req/hr
rate limit). The token is held in a header on the httpx client and
never logged.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from types import TracebackType

import httpx

from issue_triage.config import Config
from issue_triage.models import Comment, Issue


_LOG = logging.getLogger(__name__)
_API_ROOT = "https://api.github.com"

# GitHub's Link header looks like:
#   <https://api.github.com/...?page=2>; rel="next", <...>; rel="last"
# We only care about the next-page URL when paginating.
_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


# --- Errors ------------------------------------------------------------

class GitHubError(RuntimeError):
    """Generic GitHub-side failure (non-2xx, network issue, parse error)."""


class RateLimitError(GitHubError):
    """Raised on a 403 / 429 that's identifiably a rate-limit response.

    ``reset_at`` is the timestamp from ``X-RateLimit-Reset``, parsed
    into a tz-aware datetime so the CLI can show "resets at HH:MM UTC".
    May be ``None`` if the header was missing or malformed.
    """

    def __init__(self, message: str, reset_at: datetime | None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class TooManyIssuesError(GitHubError):
    """Raised when the fetch returns more issues than ``safety_max_issues``.

    A pathological-case guard, not a feature cap. Real users never
    hit it; if they do, the message names ``--lookback-days`` as
    the fix.
    """


# --- Client ------------------------------------------------------------

class GitHubClient:
    """A small, sync GitHub REST client.

    Use as a context manager so the underlying ``httpx.Client``
    closes cleanly when the run ends.
    """

    def __init__(self, config: Config) -> None:
        self._config = config

        token = os.getenv("GITHUB_TOKEN")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            _LOG.info(
                "no GITHUB_TOKEN set; using anonymous rate limit (60 req/hr). "
                "Set GITHUB_TOKEN for the 5000 req/hr authenticated limit."
            )

        self._client = httpx.Client(
            base_url=_API_ROOT,
            headers=headers,
            timeout=config.request_timeout_s,
        )

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.close()

    def fetch_issues(
        self, owner: str, repo: str, lookback_days: int,
    ) -> tuple[list[Issue], list[Issue]]:
        """Fetch open issues touched in the past ``lookback_days`` and split.

        Args:
            owner: Repository owner (e.g. ``"anthropics"``).
            repo: Repository name (e.g. ``"claude-code"``).
            lookback_days: Window for the past-week filter.

        Returns:
            ``(new_issues, ongoing_activity)``:

            - ``new_issues`` — issues opened in the past ``lookback_days``,
              still open. Becomes §1 of the brief.
            - ``ongoing_activity`` — issues opened *before* the cutoff but
              with at least one new comment in the past ``lookback_days``.
              Each one's ``new_comments`` field is populated. Becomes §2.

        Raises:
            RateLimitError: GitHub returned 403 / 429 due to rate limiting.
            TooManyIssuesError: total issues exceeded ``safety_max_issues``.
            GitHubError: any other API-side failure.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        raw = self._fetch_paginated(
            f"/repos/{owner}/{repo}/issues",
            params={
                "state": "open",
                "since": cutoff_iso,
                "per_page": "100",
                "sort": "updated",
                "direction": "desc",
            },
        )

        # Drop PRs and split using created_at (since= filters by updated_at).
        new_raw: list[dict] = []
        ongoing_raw: list[dict] = []
        for item in raw:
            if "pull_request" in item:
                continue
            created = _parse_iso_datetime(item["created_at"])
            if created >= cutoff:
                new_raw.append(item)
            else:
                ongoing_raw.append(item)

        # Pathological-case safety net.
        total = len(new_raw) + len(ongoing_raw)
        if total > self._config.safety_max_issues:
            raise TooManyIssuesError(
                f"{total} issues match the past-{lookback_days}-day filter "
                f"for {owner}/{repo} (safety_max_issues = "
                f"{self._config.safety_max_issues}). This is well above "
                f"expected. Try a shorter --lookback-days, or raise "
                f"safety_max_issues in config.yaml if intentional."
            )

        # For §2 candidates, fetch the new-comment text and discard
        # candidates whose only "activity" was a metadata change.
        ongoing: list[Issue] = []
        for item in ongoing_raw:
            try:
                new_comments = self._fetch_new_comments(
                    owner, repo, item["number"], cutoff_iso,
                )
            except GitHubError as exc:
                # Don't fail the whole run because one issue's comment
                # call timed out. Skip this candidate, log it, move on.
                _LOG.warning(
                    "skipping issue #%s from §2: failed to fetch new comments (%s)",
                    item["number"], exc,
                )
                continue
            if not new_comments:
                continue  # only metadata changed (label, milestone, etc.)
            ongoing.append(_to_issue(item, new_comments=new_comments))

        new_issues = [_to_issue(item) for item in new_raw]
        return new_issues, ongoing

    # --- Internals -----------------------------------------------------

    def _fetch_paginated(self, path: str, params: dict[str, str]) -> list[dict]:
        """Walk ``Link``-header pagination, accumulate all pages."""
        results: list[dict] = []
        next_url: str | None = path
        # Only the first request needs query params; subsequent next-URLs
        # already carry the query string baked in.
        send_params: dict[str, str] | None = params

        while next_url:
            response = self._request("GET", next_url, params=send_params)
            results.extend(response.json())
            next_url = _next_link(response)
            send_params = None
        return results

    def _fetch_new_comments(
        self, owner: str, repo: str, number: int, since_iso: str,
    ) -> list[Comment]:
        """Fetch the comments on issue ``#number`` created after ``since_iso``."""
        raw = self._fetch_paginated(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"since": since_iso, "per_page": "100"},
        )
        return [_to_comment(item) for item in raw]

    def _request(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Make a single HTTP request and turn known failures into typed errors."""
        try:
            response = self._client.request(method, url, params=params)
        except httpx.RequestError as exc:
            raise GitHubError(f"network error contacting GitHub: {exc}") from exc

        if _is_rate_limited(response):
            reset = _parse_rate_limit_reset(response)
            reset_msg = (
                f"resets at {reset.strftime('%H:%M UTC')}" if reset
                else "reset time unknown"
            )
            raise RateLimitError(
                f"GitHub rate-limited; {reset_msg}. "
                f"Set GITHUB_TOKEN to raise the limit from 60 to 5000 req/hr.",
                reset,
            )

        if response.status_code >= 400:
            # Truncate the body so a verbose GitHub error doesn't dominate
            # the user-facing message.
            raise GitHubError(
                f"GitHub API {response.status_code} for {url}: "
                f"{response.text[:200]}"
            )

        return response


# --- Module-level helpers ---------------------------------------------

def _parse_iso_datetime(value: str) -> datetime:
    """Parse a GitHub-style ISO-8601 timestamp into a tz-aware datetime."""
    # GitHub uses "2024-04-12T12:34:56Z"; replace the Z so fromisoformat
    # accepts it on Python 3.10. (3.11+ handles Z natively.)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_issue(item: dict, new_comments: list[Comment] | None = None) -> Issue:
    """Map a GitHub issue dict to our Issue model."""
    return Issue(
        number=item["number"],
        title=item["title"],
        body=item.get("body"),
        state=item["state"],
        created_at=_parse_iso_datetime(item["created_at"]),
        updated_at=_parse_iso_datetime(item["updated_at"]),
        author=(item.get("user") or {}).get("login", "unknown"),
        html_url=item["html_url"],
        labels=[label["name"] for label in item.get("labels") or []],
        comments_count=item.get("comments", 0),
        reactions=_extract_reactions(item.get("reactions") or {}),
        new_comments=new_comments or [],
    )


def _to_comment(item: dict) -> Comment:
    """Map a GitHub comment dict to our Comment model."""
    return Comment(
        author=(item.get("user") or {}).get("login", "unknown"),
        body=item.get("body") or "",
        created_at=_parse_iso_datetime(item["created_at"]),
    )


def _extract_reactions(raw: dict) -> dict[str, int]:
    """Strip the GitHub ``reactions`` dict to just the integer counts.

    The full dict has a ``url`` key and ``total_count`` plus per-emoji
    keys (``+1``, ``-1``, ``laugh``, …). We only want the counts.
    """
    return {key: value for key, value in raw.items() if isinstance(value, int)}


def _next_link(response: httpx.Response) -> str | None:
    """Return the ``next`` URL from a Link header, or ``None`` on the last page."""
    link = response.headers.get("Link")
    if not link:
        return None
    match = _LINK_NEXT_RE.search(link)
    return match.group(1) if match else None


def _is_rate_limited(response: httpx.Response) -> bool:
    """True if this response is identifiably a rate-limit signal.

    GitHub uses 403 for several reasons (auth, abuse, rate limit). We
    treat a 403/429 as rate-limited only when the remaining-quota header
    is 0 *or* the body explicitly mentions rate limiting.
    """
    if response.status_code not in (403, 429):
        return False
    if response.headers.get("X-RateLimit-Remaining") == "0":
        return True
    return "rate limit" in response.text.lower()


def _parse_rate_limit_reset(response: httpx.Response) -> datetime | None:
    """Parse ``X-RateLimit-Reset`` (epoch seconds) into a tz-aware datetime."""
    header = response.headers.get("X-RateLimit-Reset")
    if not header:
        return None
    try:
        return datetime.fromtimestamp(int(header), tz=timezone.utc)
    except (TypeError, ValueError):
        return None
