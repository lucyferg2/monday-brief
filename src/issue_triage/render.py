"""Render the canonical Brief into Markdown, JSON, HTML, and run.json.

Four pure functions, each taking the same ``Brief`` (or ``RunMetadata``)
Pydantic object and returning a string. The CLI writes those strings to
files under ``reports/<date>/<owner>__<repo>/``.

Design points worth defending in interview:

- **Single source of truth.** All three brief renderers consume the
  same ``Brief`` object — no per-format data structure, so the
  formats can't drift apart. Adding a fourth output format (CSV,
  RSS, whatever) is one new function, not a new pipeline.
- **HTML escaping is the primary XSS defence.** Every dynamic value
  flows through ``safe()`` before insertion into HTML. URLs are
  scheme-allowlisted (http / https only) via ``safe_url()``. A CSP
  meta tag provides defence in depth. Tested with hostile fixture
  input.
- **Single-file HTML.** Inline CSS, no external assets, no JavaScript
  required (the expand/collapse uses native ``<details>``). The
  output renders offline in any browser and can be emailed as an
  attachment without losing fidelity.
- **No emoji.** Pure typography + colour + spacing for visual
  hierarchy. Easier to defend in interview and friendlier to
  enterprise environments where emoji rendering varies.
"""

import html
import json
import urllib.parse
from datetime import datetime
from typing import Any

from issue_triage.models import (
    ActivitySpotlight,
    Brief,
    Issue,
    PrioritisedIssue,
    RunMetadata,
    Theme,
)


# --- Small safety helpers used by the HTML renderer --------------------

def safe(value: Any) -> str:
    """HTML-escape any value for safe insertion into HTML output.

    Wraps ``html.escape(s, quote=True)`` so it can be used uniformly on
    every dynamic value in the renderer. ``quote=True`` escapes both
    single and double quotes — important because we use double-quoted
    attribute values throughout.
    """
    return html.escape(str(value), quote=True)


def safe_url(url: str) -> str | None:
    """Return the URL if it's safe to use as an ``href``, else ``None``.

    Allowlists ``http`` and ``https`` schemes. Returns ``None`` for any
    other scheme (``javascript:``, ``data:``, ``file:``, …) — the
    caller renders the value as plain text instead of a link.
    """
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return url


# --- Issue-meta helpers ------------------------------------------------

def _format_issue_date(dt: datetime) -> str:
    """Format a GitHub timestamp as DD-MM-YYYY for the meta strips.

    UK format with full year — distinct from the DD-MM-YY filename
    suffix so a reader doesn't confuse the two.
    """
    return dt.strftime("%d-%m-%Y")


def _issue_meta_md(issue: Issue, *, ongoing: bool) -> str:
    """Render the meta strip (dates / author / counts / labels) for Markdown.

    For new (§1) issues: only the open date is shown. For ongoing-
    activity (§2) issues, both the open date and the last-update date
    are shown so the maintainer can see how long it's been simmering
    before the recent activity.
    """
    parts: list[str] = [f"**Opened** {_format_issue_date(issue.created_at)}"]
    if ongoing:
        parts.append(
            f"**Last update** {_format_issue_date(issue.updated_at)}"
        )
    parts.append(f"**by** @{issue.author}")
    if issue.comments_count:
        suffix = "" if issue.comments_count == 1 else "s"
        parts.append(f"{issue.comments_count} comment{suffix}")
    total_reactions = issue.reactions.get("total_count", 0) or 0
    if total_reactions:
        suffix = "" if total_reactions == 1 else "s"
        parts.append(f"{total_reactions} reaction{suffix}")
    if issue.labels:
        parts.append(f"_labels:_ {', '.join(issue.labels)}")
    return " · ".join(parts)


def _issue_meta_html(issue: Issue, *, ongoing: bool) -> str:
    """Render the meta strip for HTML. Same fields as the Markdown variant."""
    parts: list[str] = [
        f"<span><strong>Opened</strong> "
        f"{safe(_format_issue_date(issue.created_at))}</span>"
    ]
    if ongoing:
        parts.append(
            f"<span><strong>Last update</strong> "
            f"{safe(_format_issue_date(issue.updated_at))}</span>"
        )
    parts.append(f"<span><strong>by</strong> @{safe(issue.author)}</span>")
    if issue.comments_count:
        suffix = "" if issue.comments_count == 1 else "s"
        parts.append(
            f"<span>{safe(issue.comments_count)} comment{suffix}</span>"
        )
    total_reactions = issue.reactions.get("total_count", 0) or 0
    if total_reactions:
        suffix = "" if total_reactions == 1 else "s"
        parts.append(
            f"<span>{safe(total_reactions)} reaction{suffix}</span>"
        )
    if issue.labels:
        labels_html = ", ".join(safe(label) for label in issue.labels)
        parts.append(
            f'<span class="labels"><em>labels:</em> {labels_html}</span>'
        )
    return f'<div class="meta-strip">{"".join(parts)}</div>'


# --- Sorting + counting -----------------------------------------------

# High priority ranks first; ties broken by reactions count desc, then
# comments count desc. This is what a maintainer scanning 100 issues
# actually wants — most-pressing thing at the top.
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _sort_by_priority(
    items: "list[PrioritisedIssue] | list[ActivitySpotlight]",
) -> list:
    """Sort issues high → medium → low; ties broken by reactions, then comments."""
    return sorted(
        items,
        key=lambda x: (
            _PRIORITY_RANK.get(x.priority, 99),
            -int(x.issue.reactions.get("total_count", 0) or 0),
            -int(x.issue.comments_count or 0),
        ),
    )


def _count_by_priority(items: list) -> dict[str, int]:
    """Return a ``{"high": N, "medium": M, "low": K}`` dict for a section."""
    counts = {"high": 0, "medium": 0, "low": 0}
    for item in items:
        counts[item.priority] = counts.get(item.priority, 0) + 1
    return counts


def _format_priority_counts(items: list) -> str:
    """Format a section header's count line: '23 total · 5 high · 12 medium · 6 low'."""
    counts = _count_by_priority(items)
    parts = [f"{len(items)} total"]
    for priority in ("high", "medium", "low"):
        n = counts.get(priority, 0)
        if n > 0:
            parts.append(f"{n} {priority}")
    return " · ".join(parts)


# --- Markdown -----------------------------------------------------------

def render_markdown(brief: Brief) -> str:
    """Render the canonical Brief as Markdown.

    Output is readable in any markdown viewer (GitHub, VS Code preview,
    pandoc → HTML, etc.). Structure mirrors the HTML render:

    1. Header
    2. Table of contents (issue numbers grouped by priority)
    3. Themes this week (executive summary)
    4. New issues this week (sorted high → medium → low)
    5. Ongoing activity (sorted high → medium → low)
    6. Run details footer

    Sorting puts the most-pressing items at the top — important for
    the busy-repo case where a maintainer might be skimming 100 entries.
    """
    lines: list[str] = []
    generated = _format_timestamp(brief.generated_at)
    lookback = brief.run_metadata.lookback_days

    new_issues_sorted = _sort_by_priority(brief.new_issues)
    ongoing_sorted = _sort_by_priority(brief.ongoing_activity)
    issue_lookup = {item.issue.number: item.issue for item in new_issues_sorted}
    for item in ongoing_sorted:
        issue_lookup.setdefault(item.issue.number, item.issue)

    lines.append(f"# Monday brief — {brief.repo}")
    lines.append("")
    lines.append(
        f"Generated {generated} · "
        f"Provider `{brief.run_metadata.provider}` "
        f"(`{brief.run_metadata.model}`) · "
        f"{lookback}-day lookback"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # Table of contents: issue numbers grouped by priority, so a
    # reader scanning a 100-issue brief can jump straight to the high
    # bucket. Each number links to the GitHub issue.
    lines.extend(_markdown_toc(new_issues_sorted, ongoing_sorted))
    lines.append("")

    # Themes first — they're the executive summary.
    if brief.themes:
        lines.append(f"## Themes this week ({len(brief.themes)})")
        lines.append("")
        for theme in brief.themes:
            lines.extend(_markdown_for_theme(theme, issue_lookup))
            lines.append("")

    # New issues — sorted by priority.
    lines.append(
        f"## New issues this week — {_format_priority_counts(new_issues_sorted)}"
        if new_issues_sorted
        else "## New issues this week (0 total)"
    )
    lines.append("")
    if new_issues_sorted:
        for item in new_issues_sorted:
            lines.extend(_markdown_for_new_issue(item))
            lines.append("")
    else:
        lines.append("_No issues opened in the past week._")
        lines.append("")

    # Ongoing activity — sorted by priority.
    lines.append(
        f"## Ongoing activity — {_format_priority_counts(ongoing_sorted)}"
        if ongoing_sorted
        else "## Ongoing activity (0 total)"
    )
    lines.append("")
    if ongoing_sorted:
        for spotlight in ongoing_sorted:
            lines.extend(_markdown_for_spotlight(spotlight))
            lines.append("")
    else:
        lines.append(
            "_No older issues gained new comments in the past week._"
        )
        lines.append("")

    # Run metadata footer
    lines.append("---")
    lines.append("")
    lines.append("### Run details")
    lines.append("")
    lines.extend(_markdown_for_run_metadata(brief.run_metadata))
    lines.append("")

    return "\n".join(lines)


def _markdown_toc(
    new_issues: list[PrioritisedIssue],
    ongoing: list[ActivitySpotlight],
) -> list[str]:
    """Build a priority-grouped table of contents at the top of the brief.

    Each line lists the issue numbers for one priority bucket within
    one section, e.g.:

        **New this week (high · 3):** [#1234](…), [#1235](…), [#1240](…)

    Empty buckets are skipped so the TOC stays tight.
    """
    if not new_issues and not ongoing:
        return ["_No issues to brief this week._"]

    out: list[str] = ["**At a glance**", ""]

    def _group(items: list, label: str) -> None:
        if not items:
            return
        buckets: dict[str, list] = {"high": [], "medium": [], "low": []}
        for item in items:
            buckets.setdefault(item.priority, []).append(item)
        for priority in ("high", "medium", "low"):
            bucket = buckets.get(priority, [])
            if not bucket:
                continue
            links = ", ".join(
                f"[#{item.issue.number}]({item.issue.html_url})"
                for item in bucket
            )
            out.append(
                f"- **{label} · {priority} ({len(bucket)}):** {links}"
            )

    _group(new_issues, "New this week")
    _group(ongoing, "Ongoing activity")
    return out


def _markdown_for_new_issue(item: PrioritisedIssue) -> list[str]:
    failure = " · **parse failure**" if item.parse_failure else ""
    out = [
        (
            f"### `{item.category}` · `{item.priority}` "
            f"· [#{item.issue.number}]({item.issue.html_url}) — "
            f"{item.issue.title}{failure}"
        ),
        "",
    ]
    if item.translated_title:
        out.append(f"_English:_ {item.translated_title}")
        out.append("")
    out.extend([
        _issue_meta_md(item.issue, ongoing=False),
        "",
        item.summary,
        "",
        f"_Priority rationale:_ {item.priority_rationale}",
    ])
    return out


def _markdown_for_spotlight(item: ActivitySpotlight) -> list[str]:
    failure = " · **parse failure**" if item.parse_failure else ""
    comments = (
        f" · {item.new_comments_count} new comment"
        f"{'' if item.new_comments_count == 1 else 's'}"
    )
    out = [
        (
            f"### `{item.category}` · `{item.priority}` "
            f"· [#{item.issue.number}]({item.issue.html_url}) — "
            f"{item.issue.title}{comments}{failure}"
        ),
        "",
    ]
    if item.translated_title:
        out.append(f"_English:_ {item.translated_title}")
        out.append("")
    out.extend([
        _issue_meta_md(item.issue, ongoing=True),
        "",
        item.summary,
        "",
        f"_Why this is moving now:_ {item.new_activity}",
        "",
        f"_Priority rationale:_ {item.priority_rationale}",
    ])
    return out


def _markdown_for_theme(theme: Theme, issue_lookup: dict[int, Issue]) -> list[str]:
    """Render one theme. Issue numbers link to the underlying GitHub issues."""
    issue_links: list[str] = []
    for number in theme.issue_numbers:
        issue = issue_lookup.get(number)
        if issue:
            issue_links.append(f"[#{number}]({issue.html_url})")
        else:
            issue_links.append(f"#{number}")
    return [
        f"### {theme.name}",
        "",
        theme.summary,
        "",
        f"_Issues:_ {', '.join(issue_links)}",
    ]


def _markdown_for_run_metadata(rm: RunMetadata) -> list[str]:
    cost = (
        f"${rm.estimated_cost_usd:.4f}"
        if rm.estimated_cost_usd is not None
        else "unknown"
    )
    return [
        f"- LLM calls: **{rm.llm_calls}**",
        (
            f"- Tokens: **{rm.tokens_in:,}** in / "
            f"**{rm.tokens_out:,}** out (estimated cost: {cost})"
        ),
        f"- Duration: **{rm.duration_seconds:.1f} s**",
        f"- Parse failures: **{len(rm.parse_failures)}**",
        f"- Injection warnings: **{len(rm.injection_warnings)}**",
        (
            f"- Ongoing-activity candidates skipped "
            f"(follow-up call failed): **{len(rm.section_2_skipped)}**"
        ),
    ]


# --- JSON --------------------------------------------------------------

def render_json(brief: Brief) -> str:
    """Render the Brief as canonical Pydantic JSON (indent=2)."""
    return brief.model_dump_json(indent=2)


def render_run_metadata(run_metadata: RunMetadata) -> str:
    """Render the RunMetadata as standalone run.json.

    Same data as ``brief.json["run_metadata"]`` — separate file because
    a reviewer / future automation will often want just the snapshot
    without parsing the whole brief.
    """
    return run_metadata.model_dump_json(indent=2)


# --- HTML --------------------------------------------------------------

def render_html(brief: Brief) -> str:
    """Render the Brief as a single self-contained HTML file.

    Inline CSS, no external assets, no JavaScript. Native ``<details>``
    for expand/collapse. Every dynamic value passes through ``safe()``
    before insertion. URL ``href``s use ``safe_url()`` to allow only
    http / https.

    Issues within each section are sorted high → medium → low (then by
    reactions count), so the most-pressing items rise to the top. Themes
    render *before* the issue lists as a weekly executive summary; theme
    issue numbers link to the original GitHub issues (with a hover
    tooltip showing the issue title).
    """
    repo = safe(brief.repo)
    generated = safe(_format_timestamp(brief.generated_at))
    provider = safe(brief.run_metadata.provider)
    model = safe(brief.run_metadata.model)
    lookback = safe(brief.run_metadata.lookback_days)

    # Sort each section by priority for the busy-repo case.
    new_issues_sorted = _sort_by_priority(brief.new_issues)
    ongoing_sorted = _sort_by_priority(brief.ongoing_activity)

    # Lookup so the themes section can hyperlink issue numbers to the
    # underlying GitHub URL (and put the title in a hover tooltip).
    issue_lookup = {item.issue.number: item.issue for item in new_issues_sorted}
    for item in ongoing_sorted:
        issue_lookup.setdefault(item.issue.number, item.issue)

    body_parts: list[str] = [
        _html_header(repo, generated, provider, model, lookback),
        _html_about(),
        _html_at_a_glance(new_issues_sorted, ongoing_sorted),
        _html_themes(brief.themes, issue_lookup),
        _html_new_issues(new_issues_sorted),
        _html_ongoing_activity(ongoing_sorted),
        _html_footer(brief.run_metadata),
    ]
    body = "\n".join(body_parts)

    return _HTML_SHELL.format(
        title=f"Monday brief · {repo} · {generated}",
        css=_CSS,
        body=body,
    )


# Layout & CSS — kept as module-level constants so the renderer is just
# string concatenation. Single inline stylesheet, dark mode via
# prefers-color-scheme, no JavaScript.

_HTML_SHELL = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""


_CSS = """\
:root {
  --bg: #ffffff;
  --bg-soft: #f8fafc;
  --bg-strong: #f1f5f9;
  --fg: #0f172a;
  --fg-soft: #334155;
  --fg-muted: #64748b;
  --border: #e2e8f0;
  --border-strong: #cbd5e1;
  --accent: #2563eb;
  --accent-soft: #dbeafe;
  --prio-high-bg: #fee2e2;
  --prio-high-fg: #991b1b;
  --prio-medium-bg: #fef3c7;
  --prio-medium-fg: #92400e;
  --prio-low-bg: #dcfce7;
  --prio-low-fg: #166534;
  --warn-bg: #fef3c7;
  --warn-fg: #92400e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b1220;
    --bg-soft: #111a2e;
    --bg-strong: #15213a;
    --fg: #e2e8f0;
    --fg-soft: #cbd5e1;
    --fg-muted: #94a3b8;
    --border: #1e293b;
    --border-strong: #334155;
    --accent: #60a5fa;
    --accent-soft: #1e293b;
    --prio-high-bg: #450a0a;
    --prio-high-fg: #fecaca;
    --prio-medium-bg: #451a03;
    --prio-medium-fg: #fed7aa;
    --prio-low-bg: #052e16;
    --prio-low-fg: #bbf7d0;
    --warn-bg: #451a03;
    --warn-fg: #fed7aa;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  font-size: 15px;
  line-height: 1.55;
  color: var(--fg);
  background: var(--bg);
}
.wrap {
  max-width: 64rem;
  margin: 0 auto;
  padding: 2.5rem 1.5rem 4rem;
}
header h1 {
  font-size: 1.75rem;
  font-weight: 600;
  margin: 0 0 0.25rem;
  letter-spacing: -0.01em;
}
header .meta {
  color: var(--fg-muted);
  font-size: 0.9rem;
  margin: 0;
}
header .meta code {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, monospace;
  font-size: 0.85em;
  background: var(--bg-strong);
  padding: 0.1em 0.4em;
  border-radius: 4px;
  color: var(--fg-soft);
}
section, footer, details.about { margin-top: 2rem; }
section h2 {
  font-size: 1.15rem;
  font-weight: 600;
  margin: 0 0 1rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
section h2 .count {
  font-weight: 400;
  font-size: 0.85em;
  color: var(--fg-muted);
}
.empty-section {
  color: var(--fg-muted);
  font-style: italic;
  padding: 1rem 0;
}
.issue {
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 0.75rem;
  background: var(--bg-soft);
  overflow: hidden;
}
.issue.parse-failure {
  border-left: 4px solid var(--warn-fg);
  background: var(--warn-bg);
}
.issue summary {
  list-style: none;
  cursor: pointer;
  padding: 0.85rem 1rem;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.6rem;
  font-size: 0.95rem;
}
.issue summary::-webkit-details-marker { display: none; }
.issue summary::before {
  content: "›";
  display: inline-block;
  width: 0.75rem;
  text-align: center;
  color: var(--fg-muted);
  transition: transform 0.15s ease-out;
}
.issue[open] summary::before { transform: rotate(90deg); }
.issue:hover { border-color: var(--border-strong); }
.issue .title {
  flex: 1 1 auto;
  min-width: 12rem;
  color: var(--fg);
  font-weight: 500;
}
.issue .title .title-en {
  display: block;
  margin-top: 0.2rem;
  font-size: 0.82rem;
  font-weight: 400;
  font-style: italic;
  color: var(--fg-muted);
}
.issue .num {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, monospace;
  font-size: 0.85rem;
  color: var(--fg-muted);
  text-decoration: none;
}
.issue .num:hover { color: var(--accent); text-decoration: underline; }
.issue .body {
  padding: 0 1rem 1rem 2.25rem;
  border-top: 1px solid var(--border);
  background: var(--bg);
}
.issue .body > * { margin: 0.75rem 0; }
.issue .body .meta-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 1rem;
  padding-bottom: 0.65rem;
  margin-top: 0.75rem;
  margin-bottom: 0;
  border-bottom: 1px dashed var(--border);
  font-size: 0.82rem;
  color: var(--fg-muted);
}
.issue .body .meta-strip strong {
  color: var(--fg-soft);
  font-weight: 500;
}
.issue .body .meta-strip .labels {
  flex: 1 1 100%;
}
.issue .body .meta-strip .labels em {
  font-style: normal;
  color: var(--fg-soft);
  font-weight: 500;
}
.issue .body p { color: var(--fg-soft); }
.issue .body .label {
  display: block;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--fg-muted);
  margin-bottom: 0.2rem;
}
.issue .body .gh-link {
  display: inline-block;
  margin-top: 0.25rem;
  color: var(--accent);
  text-decoration: none;
  font-size: 0.9rem;
}
.issue .body .gh-link:hover { text-decoration: underline; }
.pill {
  display: inline-flex;
  align-items: center;
  font-size: 0.72rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0.18rem 0.5rem;
  border-radius: 999px;
  white-space: nowrap;
}
.pill.prio-high { background: var(--prio-high-bg); color: var(--prio-high-fg); }
.pill.prio-medium { background: var(--prio-medium-bg); color: var(--prio-medium-fg); }
.pill.prio-low { background: var(--prio-low-bg); color: var(--prio-low-fg); }
.pill.category {
  background: var(--bg-strong);
  color: var(--fg-soft);
  font-weight: 500;
}
.pill.warn { background: var(--warn-bg); color: var(--warn-fg); }
.pill.activity {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 500;
}
details.about {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-soft);
}
details.about > summary {
  cursor: pointer;
  padding: 0.85rem 1rem;
  font-weight: 500;
  color: var(--fg-soft);
  list-style: none;
}
details.about > summary::-webkit-details-marker { display: none; }
details.about > summary::before {
  content: "›";
  display: inline-block;
  width: 0.75rem;
  margin-right: 0.4rem;
  color: var(--fg-muted);
  transition: transform 0.15s ease-out;
}
details.about[open] > summary::before { transform: rotate(90deg); }
details.about .about-body {
  padding: 0 1.25rem 1rem 2.25rem;
  border-top: 1px solid var(--border);
}
details.about h3 {
  font-size: 0.95rem;
  font-weight: 600;
  margin: 1.25rem 0 0.5rem;
  color: var(--fg);
}
details.about p, details.about li, details.about dd { color: var(--fg-soft); }
details.about dl { margin: 0.5rem 0; }
details.about dt { font-weight: 600; margin-top: 0.5rem; color: var(--fg); }
details.about dd { margin-left: 1.5rem; }
details.about .legend {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin: 0.75rem 0;
}
details.about .legend > span { display: inline-flex; align-items: center; gap: 0.4rem; }
.glance {
  list-style: none;
  margin: 0;
  padding: 0.5rem 0 0;
}
.glance li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 0;
  font-size: 0.9rem;
  border-bottom: 1px solid var(--border);
}
.glance li:last-child { border-bottom: none; }
.glance .glance-label {
  color: var(--fg-soft);
  font-weight: 500;
  min-width: 9rem;
}
.glance .glance-count {
  color: var(--fg-muted);
  font-size: 0.8rem;
}
.glance .glance-numbers {
  flex: 1 1 100%;
  margin-left: 0;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, monospace;
  font-size: 0.85rem;
  color: var(--fg-muted);
  word-break: break-all;
}
.glance .glance-numbers a {
  color: var(--accent);
  text-decoration: none;
}
.glance .glance-numbers a:hover {
  text-decoration: underline;
}
@media (min-width: 40rem) {
  .glance .glance-numbers { flex: 1 1 auto; margin-left: auto; }
}
/* Highlight the issue card the user just clicked through to. */
.issue:target {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  scroll-margin-top: 1rem;
}
.theme {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.85rem 1rem;
  margin-bottom: 0.75rem;
  background: var(--bg-soft);
}
.theme h3 { margin: 0 0 0.35rem; font-size: 1rem; font-weight: 600; }
.theme p { margin: 0 0 0.5rem; color: var(--fg-soft); }
.theme .issues { font-size: 0.85rem; color: var(--fg-muted); }
footer {
  margin-top: 3rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
  font-size: 0.85rem;
  color: var(--fg-muted);
}
footer dl {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.35rem 1.5rem;
  margin: 0.5rem 0;
}
footer dt { color: var(--fg-soft); font-weight: 500; }
footer dd { margin: 0; color: var(--fg); }
footer code {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, monospace;
}
"""


def _html_header(
    repo: str,
    generated: str,
    provider: str,
    model: str,
    lookback: str,
) -> str:
    return (
        "<header>"
        f"<h1>Monday brief — {repo}</h1>"
        f"<p class=\"meta\">Generated {generated} · Provider "
        f"<code>{provider}</code> · Model <code>{model}</code> · "
        f"{lookback}-day lookback</p>"
        "</header>"
    )


def _html_at_a_glance(
    new_issues: list[PrioritisedIssue],
    ongoing: list[ActivitySpotlight],
) -> str:
    """Render the priority-grouped 'At a glance' overview panel.

    A flat scannable list of every issue number, grouped by section
    and priority, where each number is an anchor link to its full
    card lower on the page. Mirrors the markdown TOC so the three
    output formats stay consistent.

    Returns an empty string when there's nothing to overview — the
    section header simply doesn't render.
    """
    if not new_issues and not ongoing:
        return ""

    rows: list[str] = []

    def _emit(items: list, label: str) -> None:
        if not items:
            return
        buckets: dict[str, list] = {"high": [], "medium": [], "low": []}
        for item in items:
            buckets.setdefault(item.priority, []).append(item)
        for priority in ("high", "medium", "low"):
            bucket = buckets.get(priority, [])
            if not bucket:
                continue
            links = ", ".join(
                f'<a href="#issue-{safe(item.issue.number)}" '
                f'title="{safe(item.issue.title)}">'
                f'#{safe(item.issue.number)}</a>'
                for item in bucket
            )
            rows.append(
                f'<li>'
                f'<span class="glance-label">{label}</span>'
                f'<span class="pill prio-{priority}">{priority}</span>'
                f'<span class="glance-count">({len(bucket)})</span>'
                f'<span class="glance-numbers">{links}</span>'
                f'</li>'
            )

    _emit(new_issues, "New this week")
    _emit(ongoing, "Ongoing activity")

    if not rows:
        return ""

    return (
        '<section id="at-a-glance">'
        '<h2>At a glance</h2>'
        f'<ul class="glance">{"".join(rows)}</ul>'
        '</section>'
    )


def _html_about() -> str:
    """A collapsible 'About this brief' section.

    Static content describing what the brief is, what the sections mean,
    and what the priority colours indicate. Lets a team member who
    receives the file by email read it without needing the tool.
    """
    return """\
<details class="about">
<summary>About this brief — click to expand</summary>
<div class="about-body">

<h3>What you're looking at</h3>
<p>This brief surfaces what changed in this repository's issues over the past week. It is generated by feeding open-issue text through a language model that categorises each issue, prioritises it relative to the maintainer's stated context, and writes a short summary. The output is a starting point for Monday-morning triage — not the final word. Always sanity-check before acting on a recommendation.</p>

<h3>How the brief is organised</h3>
<dl>
<dt>Themes this week</dt>
<dd>A short executive summary clustering the week's new issues into named themes (e.g. "macOS install regressions"). Shown only when there are enough new issues to cluster meaningfully. Each cluster's issue numbers link to the originals on GitHub — hover for the issue title.</dd>
<dt>New issues this week</dt>
<dd>Issues opened during the past lookback window (default: 7 days) that are still open. Each one has a category, priority, and one-paragraph summary. Sorted high → medium → low (then by reactions) so the most-pressing items are at the top.</dd>
<dt>Ongoing activity</dt>
<dd>Older open issues that gained new comments inside the lookback window. Each entry has the same three things as a new issue, plus a "why this is moving now" line interpreting the recent comments.</dd>
</dl>

<h3>Priority colour coding</h3>
<div class="legend">
<span><span class="pill prio-high">high</span> needs attention this week</span>
<span><span class="pill prio-medium">medium</span> worth looking at soon, not blocking</span>
<span><span class="pill prio-low">low</span> routine — can wait</span>
</div>

<h3>Reading the entries</h3>
<p>Click any issue header to expand its details. The summary describes what the user is reporting; the "why this is moving now" line (ongoing-activity entries only) interprets the recent comments; the priority rationale explains the model's reasoning. The issue number links to the original GitHub issue.</p>

<h3>Notes on reliability</h3>
<ul>
<li>An entry marked <span class="pill warn">parse failure</span> means the language model's response did not validate against the expected schema. The issue is still listed so it isn't silently dropped — see the run-details footer for failure counts.</li>
<li>Injection-warning counts (also in the footer) flag issues whose text contained patterns resembling prompt-injection attempts. The brief still uses them, but you may want to inspect the originals.</li>
<li>The run-details footer records the exact provider, model, and prompt versions used. The same input + same versions should produce a comparable brief.</li>
</ul>

</div>
</details>
"""


def _html_new_issues(items: list[PrioritisedIssue]) -> str:
    """Render the 'new issues' section (issues opened in the lookback window)."""
    if not items:
        inner = (
            "<p class=\"empty-section\">"
            "No issues opened in the past week."
            "</p>"
        )
        count_str = "0 total"
    else:
        inner = "\n".join(
            _html_issue_card(item, ongoing=False) for item in items
        )
        count_str = _format_priority_counts(items)
    return (
        "<section id=\"new-issues\">"
        f"<h2>New issues this week "
        f"<span class=\"count\">{safe(count_str)}</span></h2>"
        f"{inner}"
        "</section>"
    )


def _html_ongoing_activity(items: list[ActivitySpotlight]) -> str:
    """Render the 'ongoing activity' section (older issues with new comments)."""
    if not items:
        inner = (
            "<p class=\"empty-section\">"
            "No older issues gained new comments in the past week."
            "</p>"
        )
        count_str = "0 total"
    else:
        inner = "\n".join(
            _html_issue_card(item, ongoing=True) for item in items
        )
        count_str = _format_priority_counts(items)
    return (
        "<section id=\"ongoing-activity\">"
        f"<h2>Ongoing activity "
        f"<span class=\"count\">{safe(count_str)}</span></h2>"
        f"{inner}"
        "</section>"
    )


def _html_issue_card(
    item: PrioritisedIssue | ActivitySpotlight,
    *,
    ongoing: bool,
) -> str:
    """Render one issue card. Same shape for new + ongoing; ongoing adds 'why now'."""
    issue = item.issue
    priority = safe(item.priority)
    category = safe(item.category)
    number = safe(issue.number)
    title = safe(issue.title)
    summary = safe(item.summary)
    rationale = safe(item.priority_rationale)
    url = safe_url(issue.html_url)
    failure_class = " parse-failure" if item.parse_failure else ""
    failure_pill = (
        "<span class=\"pill warn\">parse failure</span>"
        if item.parse_failure
        else ""
    )

    # Activity badge appears on ongoing-activity entries only.
    activity_pill = ""
    why_now_block = ""
    if ongoing:
        spotlight: ActivitySpotlight = item  # type: ignore[assignment]
        comments_count = spotlight.new_comments_count
        comment_label = "new comment" if comments_count == 1 else "new comments"
        activity_pill = (
            f"<span class=\"pill activity\">"
            f"{safe(comments_count)} {comment_label}</span>"
        )
        why_now_block = (
            "<p><span class=\"label\">Why this is moving now</span>"
            f"{safe(spotlight.new_activity)}</p>"
        )

    issue_link = (
        f"<a class=\"num\" href=\"{url}\">#{number}</a>"
        if url
        else f"<span class=\"num\">#{number}</span>"
    )
    gh_link = (
        f"<a class=\"gh-link\" href=\"{url}\">View on GitHub</a>"
        if url
        else ""
    )

    # Show the English translation of the title underneath the original
    # only when the LLM populated translated_title (i.e. the original
    # wasn't English). Keeps English-language repos visually clean.
    translated_block = ""
    if item.translated_title:
        translated_block = (
            f'<span class="title-en">English: {safe(item.translated_title)}'
            f'</span>'
        )

    meta_strip = _issue_meta_html(issue, ongoing=ongoing)

    return (
        f"<article id=\"issue-{number}\" class=\"issue{failure_class}\"><details>"
        "<summary>"
        f"<span class=\"pill prio-{priority}\">{priority}</span>"
        f"<span class=\"pill category\">{category}</span>"
        f"{activity_pill}"
        f"{issue_link}"
        f"<span class=\"title\">{title}{translated_block}</span>"
        f"{failure_pill}"
        "</summary>"
        "<div class=\"body\">"
        f"{meta_strip}"
        f"<p><span class=\"label\">Summary</span>{summary}</p>"
        f"{why_now_block}"
        f"<p><span class=\"label\">Priority rationale</span>{rationale}</p>"
        f"{gh_link}"
        "</div>"
        "</details></article>"
    )


def _html_themes(themes: list[Theme], issue_lookup: dict[int, "Issue"]) -> str:
    """Render the themes section.

    Each theme's issue number list is rendered as hyperlinks pointing
    at the underlying GitHub issue, with the issue title in a
    ``title=`` attribute so hovering shows a native browser tooltip.
    If a theme references an unknown issue number (shouldn't happen in
    practice — the themes prompt only sees §1 numbers), the number
    falls back to plain text rather than a broken link.
    """
    if not themes:
        return ""

    cards: list[str] = []
    for theme in themes:
        name = safe(theme.name)
        summary = safe(theme.summary)
        issue_links: list[str] = []
        for number in theme.issue_numbers:
            issue = issue_lookup.get(number)
            url = safe_url(issue.html_url) if issue else None
            if issue and url:
                issue_links.append(
                    f"<a href=\"{url}\" title=\"{safe(issue.title)}\">"
                    f"#{safe(number)}</a>"
                )
            else:
                issue_links.append(f"#{safe(number)}")
        issue_list = ", ".join(issue_links)
        cards.append(
            "<div class=\"theme\">"
            f"<h3>{name}</h3>"
            f"<p>{summary}</p>"
            f"<p class=\"issues\">Issues: {issue_list}</p>"
            "</div>"
        )
    return (
        "<section id=\"themes\">"
        f"<h2>Themes this week "
        f"<span class=\"count\">{len(themes)} cluster"
        f"{'' if len(themes) == 1 else 's'}</span></h2>"
        f"{''.join(cards)}"
        "</section>"
    )


def _html_footer(rm: RunMetadata) -> str:
    cost = (
        f"${rm.estimated_cost_usd:.4f}"
        if rm.estimated_cost_usd is not None
        else "unknown"
    )
    prompt_versions = ", ".join(
        f"<code>{safe(name)}</code> v{safe(version)}"
        for name, version in sorted(rm.prompt_versions.items())
    )
    return (
        "<footer>"
        "<h3>Run details</h3>"
        "<dl>"
        f"<dt>LLM calls</dt><dd>{safe(rm.llm_calls)}</dd>"
        f"<dt>Tokens (in / out)</dt><dd>{rm.tokens_in:,} / {rm.tokens_out:,}</dd>"
        f"<dt>Estimated cost</dt><dd>{safe(cost)}</dd>"
        f"<dt>Duration</dt><dd>{safe(rm.duration_seconds)} s</dd>"
        f"<dt>Parse failures</dt><dd>{len(rm.parse_failures)}</dd>"
        f"<dt>Injection warnings</dt><dd>{len(rm.injection_warnings)}</dd>"
        f"<dt>Ongoing-activity candidates skipped</dt>"
        f"<dd>{len(rm.section_2_skipped)}</dd>"
        f"<dt>Prompt versions</dt><dd>{prompt_versions}</dd>"
        "</dl>"
        "</footer>"
    )


# --- Small shared formatting helpers -----------------------------------

def _format_timestamp(dt: datetime) -> str:
    """Format a datetime as 'YYYY-MM-DD HH:MM UTC' for footer / header use."""
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    return dt.strftime("%Y-%m-%d %H:%M %Z").rstrip()