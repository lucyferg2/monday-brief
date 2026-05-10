"""LLM pipeline orchestration.

Takes the two issue lists from the GitHub client and produces a fully
assembled ``Brief`` by running the configured LLM through five
prompts:

- ``categorise.md`` — choose one category from the configured set.
- ``summarise.md`` — 1–2 sentence neutral summary.
- ``prioritise.md`` — high / medium / low + a one-line rationale.
- ``new_activity.md`` — §2 only; what's driving the recent attention.
- ``themes.md`` — one cross-issue pass over §1, clustering into themes.

The dispatch is split into three small helpers so each is testable on
its own (see CLAUDE.md → "Implementation tactics"):

- ``render_prompt`` — pure: substitute the template, wrap issue text
  in ``<issue>`` delimiters with ``</issue>`` escape, scan for known
  injection patterns. No I/O.
- ``call_llm`` — the *only* network-touching function. Sends the
  rendered messages, accumulates token counts.
- ``parse_response`` — pass through schema-mode parsed objects;
  otherwise validate the text against the Pydantic schema.

A 5-line ``run_pipeline`` orchestrator stitches them together. When a
parse fails, the affected issue still ends up in the brief with a
``parse_failure=True`` flag and a clear placeholder — never silently
dropped (CLAUDE.md → "No silent skips").
"""

import logging
import re
from datetime import datetime, timezone
from string import Template
from typing import Any

from pydantic import BaseModel, ValidationError

from issue_triage.config import Config
from issue_triage.models import (
    ActivitySpotlight,
    Brief,
    CategoriseOutput,
    Comment,
    InjectionWarning,
    Issue,
    NewActivityOutput,
    ParseFailure,
    PrioritisedIssue,
    PrioritiseOutput,
    Prompt,
    RunMetadata,
    SummariseOutput,
    Theme,
    ThemesOutput,
)
from issue_triage.providers import Message, ModelProvider, ProviderResponse


_LOG = logging.getLogger(__name__)


# Patterns that look like prompt-injection attempts. Logged when found
# in issue / comment text; the wrap + structured-output combination is
# the actual defence — these are surfaced so the maintainer is aware.
_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"<\s*system\s*>", re.IGNORECASE), "<system>"),
    (
        re.compile(r"ignore\s+(previous|the\s+above|all)\s+instructions?", re.IGNORECASE),
        "ignore-previous",
    ),
    (re.compile(r"</\s*issue\s*>", re.IGNORECASE), "</issue>-break"),
    (re.compile(r"\bdisregard\s+(previous|all)", re.IGNORECASE), "disregard"),
]


# --- The three single-purpose helpers ---------------------------------

class Rendered(BaseModel):
    """A prompt rendered for one issue: ready-to-send system + user."""

    system: str
    user: str


def render_prompt(
    prompt: Prompt,
    issue: Issue,
    maintainer_context: str = "",
    new_comments: list[Comment] | None = None,
    **template_vars: Any,
) -> tuple[Rendered, list[str]]:
    """Substitute the template, wrap the issue, scan for injection.

    Pure: same input → same output, no I/O. The user message is built
    here (not passed in) because the wrapping / escape rules are
    invariant across prompts.

    Args:
        prompt: The loaded Prompt (frontmatter + body template).
        issue: The Issue to render the prompt for.
        maintainer_context: Text to substitute into ``$maintainer_context``.
        new_comments: For §2's ``new_activity`` prompt — appended to the
            user message inside ``<comment>`` blocks.
        **template_vars: Other ``$name`` placeholders the prompt body
            references (e.g. ``categories``, ``reactions``, ``age_days``).

    Returns:
        A tuple ``(Rendered, [injection-pattern names found])``.
    """
    sys_template = Template(prompt.body)
    sys_text = sys_template.safe_substitute(
        maintainer_context=(
            maintainer_context.strip() or "(no specific maintainer context provided)"
        ),
        **{key: str(value) for key, value in template_vars.items()},
    )

    # Escape any literal closing delimiters in attacker-controllable text
    # before wrapping. The substitution turns </issue> into < /issue> —
    # the LLM still reads the same content but the wrapping can't be
    # broken out of.
    title_safe = issue.title.replace("</issue>", "< /issue>")
    body_safe = (issue.body or "").replace("</issue>", "< /issue>")

    user_lines = [
        f'<issue id="{issue.number}">',
        f"Title: {title_safe}",
        "",
        body_safe or "(no body provided)",
        "</issue>",
    ]

    if new_comments:
        user_lines.append("")
        user_lines.append("New comments since the lookback cutoff:")
        for comment in new_comments:
            comment_safe = comment.body.replace("</comment>", "< /comment>")
            user_lines.append(f'<comment author="{comment.author}">')
            user_lines.append(comment_safe)
            user_lines.append("</comment>")

    user_text = "\n".join(user_lines)

    # Scan original (pre-escape) text for known injection signals.
    raw_parts = [issue.title or "", issue.body or ""]
    raw_parts.extend(comment.body for comment in (new_comments or []))
    raw = "\n".join(raw_parts)
    found: list[str] = []
    for pattern_re, label in _INJECTION_PATTERNS:
        if pattern_re.search(raw):
            found.append(label)

    return Rendered(system=sys_text, user=user_text), found


def call_llm(
    provider: ModelProvider,
    rendered: Rendered,
    response_schema: type[BaseModel] | None = None,
) -> ProviderResponse:
    """Send a Rendered prompt to the provider. The only network-touching helper."""
    messages = [
        Message(role="system", content=rendered.system),
        Message(role="user", content=rendered.user),
    ]
    return provider.complete(messages, response_schema=response_schema)


def parse_response(
    response: ProviderResponse,
    schema: type[BaseModel],
) -> BaseModel | None:
    """Pass through schema-mode results; otherwise validate text as JSON.

    Returns None on parse failure. The caller logs and records the
    failure so the issue stays in the brief flagged rather than
    disappearing.
    """
    if response.parsed is not None:
        return response.parsed
    try:
        return schema.model_validate_json(response.text)
    except (ValidationError, ValueError) as exc:
        _LOG.warning(
            "could not parse LLM response against %s: %s. Raw (truncated): %s",
            schema.__name__, exc, response.text[:200],
        )
        return None


# --- Per-issue dispatch -----------------------------------------------

def _run_step(
    prompt: Prompt,
    issue: Issue,
    provider: ModelProvider,
    maintainer_context: str,
    schema: type[BaseModel],
    metadata: RunMetadata,
    stage_name: str,
    template_vars: dict[str, Any] | None = None,
    new_comments: list[Comment] | None = None,
) -> BaseModel | None:
    """Render → call → parse for one prompt against one issue.

    Records token usage, injection warnings, and parse failures into
    ``metadata``. Returns ``None`` on parse failure; callers fill a
    fallback value and set ``parse_failure=True`` on their result.
    """
    rendered, injection_patterns = render_prompt(
        prompt,
        issue,
        maintainer_context=maintainer_context,
        new_comments=new_comments,
        **(template_vars or {}),
    )

    if injection_patterns and not _already_recorded_injection(metadata, issue.number):
        metadata.injection_warnings.append(
            InjectionWarning(issue_number=issue.number, patterns=injection_patterns)
        )

    response = call_llm(provider, rendered, response_schema=schema)
    metadata.llm_calls += 1
    metadata.tokens_in += response.tokens_in
    metadata.tokens_out += response.tokens_out

    parsed = parse_response(response, schema)
    if parsed is None:
        metadata.parse_failures.append(
            ParseFailure(
                issue_number=issue.number,
                stage=stage_name,
                reason="malformed response (could not validate against schema)",
            )
        )
    return parsed


def _already_recorded_injection(metadata: RunMetadata, issue_number: int) -> bool:
    """We only need one injection warning per issue, not one per stage."""
    return any(w.issue_number == issue_number for w in metadata.injection_warnings)


def _resolve_category(
    parsed: CategoriseOutput | None,
    valid_names: set[str],
    fallback: str,
    issue_number: int,
    metadata: RunMetadata,
) -> tuple[str, bool]:
    """Validate the LLM's category against the configured set.

    Returns ``(name, used_fallback)``. When the LLM emits a category
    outside the configured set, we fall back to the *last* configured
    entry (typically ``other``) and record it as a parse failure so
    the issue ends up flagged in the brief.
    """
    if parsed is None:
        return fallback, True
    if parsed.category in valid_names:
        return parsed.category, False
    _LOG.warning(
        "issue #%s: LLM emitted unknown category %r; falling back to %r",
        issue_number, parsed.category, fallback,
    )
    metadata.parse_failures.append(
        ParseFailure(
            issue_number=issue_number,
            stage="categorise",
            reason=(
                f"unknown category {parsed.category!r}; "
                f"fell back to {fallback!r}"
            ),
        )
    )
    return fallback, True


def _format_categories(categories: list[Any]) -> str:
    """Render the configured categories list for the categorise prompt."""
    return "\n".join(f"- {category.name}: {category.description}" for category in categories)


def _heuristic_vars(issue: Issue) -> dict[str, Any]:
    """Compute the priority-prompt heuristic inputs from the Issue."""
    age_days = max(0, (datetime.now(timezone.utc) - issue.created_at).days)
    return {
        "reactions": issue.reactions.get("total_count", 0),
        "comments": issue.comments_count,
        "age_days": age_days,
    }


def _process_section_1(
    issue: Issue,
    prompts: dict[str, Prompt],
    provider: ModelProvider,
    config: Config,
    maintainer_context: str,
    metadata: RunMetadata,
    valid_categories: set[str],
    fallback_category: str,
) -> PrioritisedIssue:
    """Categorise + summarise + prioritise a §1 (new) issue."""
    cat = _run_step(
        prompts["categorise"], issue, provider, maintainer_context,
        CategoriseOutput, metadata, "categorise",
        template_vars={"categories": _format_categories(config.categories)},
    )
    summary = _run_step(
        prompts["summarise"], issue, provider, maintainer_context,
        SummariseOutput, metadata, "summarise",
    )
    priority = _run_step(
        prompts["prioritise"], issue, provider, maintainer_context,
        PrioritiseOutput, metadata, "prioritise",
        template_vars=_heuristic_vars(issue),
    )

    category_name, cat_failed = _resolve_category(
        cat, valid_categories, fallback_category, issue.number, metadata,
    )
    parse_failed = cat_failed or (summary is None) or (priority is None)

    return PrioritisedIssue(
        issue=issue,
        category=category_name,
        summary=(summary.summary if summary else "(parse failure — see run.json)"),
        priority=(priority.priority if priority else "low"),
        priority_rationale=(
            priority.rationale if priority else "(parse failure — see run.json)"
        ),
        parse_failure=parse_failed,
    )


def _process_section_2(
    issue: Issue,
    prompts: dict[str, Prompt],
    provider: ModelProvider,
    config: Config,
    maintainer_context: str,
    metadata: RunMetadata,
    valid_categories: set[str],
    fallback_category: str,
) -> ActivitySpotlight:
    """Categorise + summarise + prioritise + new_activity for a §2 issue."""
    cat = _run_step(
        prompts["categorise"], issue, provider, maintainer_context,
        CategoriseOutput, metadata, "categorise",
        template_vars={"categories": _format_categories(config.categories)},
    )
    summary = _run_step(
        prompts["summarise"], issue, provider, maintainer_context,
        SummariseOutput, metadata, "summarise",
    )
    priority = _run_step(
        prompts["prioritise"], issue, provider, maintainer_context,
        PrioritiseOutput, metadata, "prioritise",
        template_vars=_heuristic_vars(issue),
    )
    activity = _run_step(
        prompts["new_activity"], issue, provider, maintainer_context,
        NewActivityOutput, metadata, "new_activity",
        new_comments=issue.new_comments,
    )

    category_name, cat_failed = _resolve_category(
        cat, valid_categories, fallback_category, issue.number, metadata,
    )
    parse_failed = (
        cat_failed
        or (summary is None)
        or (priority is None)
        or (activity is None)
    )

    return ActivitySpotlight(
        issue=issue,
        category=category_name,
        summary=(summary.summary if summary else "(parse failure — see run.json)"),
        priority=(priority.priority if priority else "low"),
        priority_rationale=(
            priority.rationale if priority else "(parse failure — see run.json)"
        ),
        new_activity=(
            activity.new_activity if activity else "(parse failure — see run.json)"
        ),
        new_comments_count=len(issue.new_comments),
        parse_failure=parse_failed,
    )


def _aggregate_themes(
    new_results: list[PrioritisedIssue],
    prompt: Prompt,
    provider: ModelProvider,
    maintainer_context: str,
    metadata: RunMetadata,
) -> list[Theme]:
    """One cross-issue LLM call to cluster §1 issues into named themes."""
    sys_text = Template(prompt.body).safe_substitute(
        maintainer_context=(
            maintainer_context.strip() or "(no specific maintainer context provided)"
        ),
    )

    user_lines = ["This week's new issues with their summaries:", ""]
    for item in new_results:
        user_lines.append(f"#{item.issue.number}: {item.issue.title}")
        user_lines.append(f"  Category: {item.category}; Priority: {item.priority}")
        user_lines.append(f"  Summary: {item.summary}")
        user_lines.append("")
    user_text = "\n".join(user_lines)

    rendered = Rendered(system=sys_text, user=user_text)
    response = call_llm(provider, rendered, response_schema=ThemesOutput)
    metadata.llm_calls += 1
    metadata.tokens_in += response.tokens_in
    metadata.tokens_out += response.tokens_out

    parsed = parse_response(response, ThemesOutput)
    if parsed is None:
        _LOG.warning(
            "themes aggregation failed to parse; brief will have no themes section"
        )
        metadata.parse_failures.append(
            ParseFailure(
                issue_number=0,  # cross-issue stage; no single issue to blame
                stage="themes",
                reason="malformed response (could not validate against schema)",
            )
        )
        return []
    return parsed.themes


# --- The orchestrator -------------------------------------------------

def run_pipeline(
    new_issues: list[Issue],
    ongoing: list[Issue],
    provider: ModelProvider,
    prompts: dict[str, Prompt],
    config: Config,
    maintainer_context: str,
    repo_full_name: str,
    lookback_days: int,
) -> Brief:
    """Run the full LLM pipeline against fetched issues, return a Brief."""
    started = datetime.now(timezone.utc)
    metadata = RunMetadata(
        timestamp=started,
        repo=repo_full_name,
        provider=config.provider,
        model=config.model,
        prompt_versions={name: prompt.version for name, prompt in prompts.items()},
        lookback_days=lookback_days,
        section_1_count=len(new_issues),
        section_2_count=len(ongoing),
    )

    valid_categories = {category.name for category in config.categories}
    fallback_category = config.categories[-1].name

    _LOG.info(
        "running pipeline: §1=%d issues, §2=%d issues, provider=%s, model=%s",
        len(new_issues), len(ongoing), config.provider, config.model,
    )

    new_results = [
        _process_section_1(
            issue, prompts, provider, config, maintainer_context,
            metadata, valid_categories, fallback_category,
        )
        for issue in new_issues
    ]

    ongoing_results = [
        _process_section_2(
            issue, prompts, provider, config, maintainer_context,
            metadata, valid_categories, fallback_category,
        )
        for issue in ongoing
    ]

    # Themes only run when there's enough §1 material to cluster.
    themes: list[Theme] = []
    if len(new_results) >= 3 and "themes" in prompts:
        themes = _aggregate_themes(
            new_results, prompts["themes"], provider, maintainer_context, metadata,
        )
    elif len(new_results) < 3:
        _LOG.info("§1 has fewer than 3 issues; skipping themes aggregation")

    finished = datetime.now(timezone.utc)
    metadata.duration_seconds = round((finished - started).total_seconds(), 2)

    return Brief(
        repo=repo_full_name,
        generated_at=started,
        new_issues=new_results,
        ongoing_activity=ongoing_results,
        themes=themes,
        run_metadata=metadata,
    )
