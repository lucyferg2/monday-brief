"""Manual verification helper for the GitHub fetch.

GitHub's web UI doesn't directly expose the distinction between
"opened in the past N days" (§1) and "older issue with new comments
in the past N days" (§2) — both can show up under similar filters.
This script runs the same fetch the tool uses and prints each issue
in each section with timestamps and a clickable URL, so a reviewer
can spot-check that the split is correct.

Usage (with config.yaml in place and GITHUB_TOKEN set):

    python scripts/verify_fetch.py https://github.com/<owner>/<repo> [lookback_days]

What to check:

- Every §1 entry's "opened" timestamp should be AFTER the cutoff.
- Every §2 entry's "opened" timestamp should be BEFORE the cutoff,
  AND have at least one new comment timestamped after the cutoff.

Click any URL to verify against the actual issue page on GitHub.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Force UTF-8 stdout so emoji-containing issue titles render on Windows
# PowerShell (cp1252 by default) without a UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from issue_triage.config import load_config, parse_repo_url
from issue_triage.github import GitHubClient


def _format(dt: datetime) -> str:
    """Render a datetime as 'YYYY-MM-DD HH:MM UTC' for at-a-glance comparison."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2

    url = sys.argv[1]
    lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 7

    owner, repo = parse_repo_url(url)
    config = load_config(Path("config.yaml"))

    print(f"Fetching {owner}/{repo} (lookback {lookback} days)...\n")

    with GitHubClient(config) as gh:
        new_issues, ongoing = gh.fetch_issues(owner, repo, lookback)

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback)
    print(f"cutoff: {_format(cutoff)}")
    print(f"  §1 — new issues (opened after the cutoff): {len(new_issues)}")
    print(f"  §2 — ongoing activity (opened before, with new comments after): {len(ongoing)}")
    print()

    print("=" * 72)
    print(f"§1 — NEW ISSUES (every opened timestamp should be AFTER {_format(cutoff)})")
    print("=" * 72)
    for issue in new_issues:
        print(
            f"#{issue.number:<6}  opened {_format(issue.created_at)}  "
            f"({issue.comments_count} total comments)"
        )
        print(f"          {issue.title[:80]}")
        print(f"          {issue.html_url}")
        print()

    print("=" * 72)
    print(
        f"§2 — ONGOING ACTIVITY (opened BEFORE {_format(cutoff)}, "
        f"with NEW comments after)"
    )
    print("=" * 72)
    for issue in ongoing:
        new_count = len(issue.new_comments)
        latest_new = max((c.created_at for c in issue.new_comments), default=None)
        latest_str = _format(latest_new) if latest_new else "?"
        print(
            f"#{issue.number:<6}  opened {_format(issue.created_at)}  "
            f"({new_count} NEW comments since cutoff; latest at {latest_str})"
        )
        print(f"          {issue.title[:80]}")
        print(f"          {issue.html_url}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
