"""Canonical data models for the pipeline.

These Pydantic models are the **contracts between modules**. The GitHub
client returns ``Issue``s; the pipeline produces ``PrioritisedIssue`` and
``ActivitySpotlight`` objects; the renderer consumes a ``Brief``. Keeping
all of these in one place means a reviewer can answer "what's the shape
of the data?" by reading a single file.

Three categories of model live here:

1. **Inputs** — what the GitHub client gives us (``Issue``, ``Comment``).
2. **LLM output schemas** — what each prompt's response is constrained to
   (``CategoriseOutput``, ``PrioritiseOutput``, etc.). These are the
   ``response_schema`` arguments passed to ``ModelProvider.complete()``.
3. **Assembled outputs** — the per-section types the pipeline produces,
   plus the canonical ``Brief`` the renderer consumes.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# --- Prompt files (loaded from src/issue_triage/prompts/) --------------

class Prompt(BaseModel):
    """A loaded markdown prompt file: frontmatter metadata + body.

    The body is a ``string.Template``-style template with $-placeholders
    (``$maintainer_context``, ``$categories``, etc.) that the pipeline
    substitutes at call time. $-style is used in preference to f-style
    so JSON examples and braces inside the prompt don't conflict.
    """

    name: str
    description: str = ""
    version: str = "0.1.0"
    model_preferences: dict[str, Any] = {}
    body: str  # the system-message template


# --- Inputs (from the GitHub client) -----------------------------------

class Comment(BaseModel):
    """A comment on a GitHub issue.

    Only the fields the pipeline / prompts actually use. Skipping
    ``user.id``, ``html_url`` etc. keeps the LLM input lean.
    """

    author: str
    body: str
    created_at: datetime


class Issue(BaseModel):
    """A GitHub issue as fetched by the client.

    ``new_comments`` is populated only for §2 candidates (issues with new
    comments in the lookback window) — the §2 follow-up call attaches
    the new comment text here so the ``new_activity`` prompt can read it.

    Reactions are kept as a flat ``dict[str, int]`` (the GitHub response
    shape) rather than a typed model: we don't enumerate every emoji,
    we only use ``total_count`` for the priority heuristic.
    """

    number: int
    title: str
    body: str | None = None
    state: str
    created_at: datetime
    updated_at: datetime
    author: str  # the user.login
    html_url: str
    labels: list[str] = []
    comments_count: int = 0
    reactions: dict[str, int] = {}
    new_comments: list[Comment] = []  # populated only for §2 issues


# --- LLM output schemas ------------------------------------------------

# These define exactly what each prompt's response must look like. They
# get passed as ``response_schema`` to the provider so structured-output
# mode can constrain the LLM directly. The free-text JSON parse is the
# fallback for providers that don't support schema mode.

class CategoriseOutput(BaseModel):
    """What ``prompts/categorise.md`` returns.

    ``category`` is validated against the configured taxonomy at the
    pipeline layer (last-entry fallback if the LLM picks something
    outside the configured set). The schema doesn't constrain it here
    because the legal values are user-configurable, not fixed.
    """

    category: str
    rationale: str = ""  # short reasoning; not rendered, kept for debugging


class SummariseOutput(BaseModel):
    """What ``prompts/summarise.md`` returns. One field, intentionally."""

    summary: str = Field(min_length=1, max_length=500)


class PrioritiseOutput(BaseModel):
    """What ``prompts/prioritise.md`` returns.

    ``priority`` is a ``Literal`` so the LLM is constrained to exactly
    these three values (in schema mode) or rejected at parse time
    (in free-text fallback).

    The ``rationale`` max length is set well above what the prompt asks
    for (≤ 200 chars). The prompt guides the LLM toward a short
    rationale; the schema acts as a safety net against truly runaway
    output rather than as a strict style enforcer — getting an issue
    booted from the brief because its rationale was 305 chars instead
    of 300 is bad UX.
    """

    priority: Literal["high", "medium", "low"]
    rationale: str = Field(min_length=1, max_length=500)


class NewActivityOutput(BaseModel):
    """What ``prompts/new_activity.md`` returns. §2 only.

    Same length philosophy as ``PrioritiseOutput.rationale``: the
    prompt asks for one short sentence, the schema allows more
    headroom so reasonable LLM outputs aren't rejected on a few
    extra characters.
    """

    new_activity: str = Field(min_length=1, max_length=600)


class Theme(BaseModel):
    """One cluster from the themes pass.

    Themes group §1 issues into named bundles ("auth bug cluster",
    "windows install regressions"). The pipeline call returns
    ``ThemesOutput`` (a wrapper); the assembled ``Brief`` exposes
    ``list[Theme]`` directly.
    """

    name: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=300)
    issue_numbers: list[int] = []


class ThemesOutput(BaseModel):
    """What ``prompts/themes.md`` returns. Wrapper so the schema is a model."""

    themes: list[Theme] = []


# --- Assembled per-section types ---------------------------------------

class PrioritisedIssue(BaseModel):
    """A §1 issue after the categorise + summarise + prioritise passes.

    ``parse_failure`` is set if any of the three LLM calls failed; the
    issue still ends up in the brief with a marker rather than being
    silently dropped (see CLAUDE.md → "No silent skips").
    """

    issue: Issue
    category: str
    summary: str
    priority: Literal["high", "medium", "low"]
    priority_rationale: str
    parse_failure: bool = False


class ActivitySpotlight(BaseModel):
    """A §2 issue: same as PrioritisedIssue plus the ``new_activity`` line.

    ``new_comments_count`` is exposed at the assembly stage so the
    HTML render can show "(8 new comments this week)" without going
    back to the underlying ``Issue.new_comments`` list.
    """

    issue: Issue
    category: str
    summary: str
    priority: Literal["high", "medium", "low"]
    priority_rationale: str
    new_activity: str
    new_comments_count: int = 0
    parse_failure: bool = False


# --- Run snapshot ------------------------------------------------------

class InjectionWarning(BaseModel):
    """Recorded when an issue body matches a known injection pattern."""

    issue_number: int
    patterns: list[str]


class ParseFailure(BaseModel):
    """Recorded when an LLM call's response can't be parsed against the schema."""

    issue_number: int
    stage: str  # "categorise" | "summarise" | "prioritise" | "new_activity"
    reason: str  # short, human-readable


class SectionTwoSkip(BaseModel):
    """Recorded when a §2 candidate's follow-up comments call fails."""

    issue_number: int
    reason: str


class RunMetadata(BaseModel):
    """Reproducibility snapshot for one pipeline run.

    Written to ``run.json`` alongside the brief outputs so that any run
    can be retraced: which provider, which model, which prompt versions,
    how many tokens, how long, what warnings.
    """

    timestamp: datetime
    repo: str  # "owner/repo"
    provider: str
    model: str
    prompt_versions: dict[str, str] = {}  # {prompt_name: version}
    lookback_days: int

    section_1_count: int = 0
    section_2_count: int = 0

    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    estimated_cost_usd: float | None = None
    duration_seconds: float = 0.0

    exit_status: str = "success"  # "success" | "halted_over_budget" | "error"

    injection_warnings: list[InjectionWarning] = []
    parse_failures: list[ParseFailure] = []
    section_2_skipped: list[SectionTwoSkip] = []


# --- The canonical Brief -----------------------------------------------

class Brief(BaseModel):
    """The single source of truth. All three renderers consume this.

    Markdown / JSON / HTML are *views* of the same Brief; there's no
    separate per-format data structure. Adding a fourth output format
    is one new function.
    """

    repo: str  # "owner/repo"
    generated_at: datetime
    new_issues: list[PrioritisedIssue] = []
    ongoing_activity: list[ActivitySpotlight] = []
    themes: list[Theme] = []
    run_metadata: RunMetadata
