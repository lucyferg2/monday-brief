"""Runtime configuration and URL parsing.

Two responsibilities that share a *validate user input at the boundary*
theme:

1. Loading and validating ``config.yaml`` into a Pydantic ``Config`` model.
2. Parsing a GitHub repo URL into ``(owner, repo)`` strictly enough that the
   result is safe to use in filesystem paths and API URLs.

Both raise typed errors with clear messages so the CLI can surface a
single-line user-facing error rather than a stack trace.

The full ``Config`` schema lives here from day one — every knob the
pipeline / providers / renderers will eventually need is declared up
front. Tasks that don't read a particular field just ignore it; this
keeps the schema stable across the build instead of growing
incrementally and forcing config-file migrations.
"""

import re
from pathlib import Path
from urllib.parse import urlparse

import frontmatter
import yaml
from pydantic import BaseModel, Field, field_validator

from issue_triage.models import Prompt


# --- Sub-models --------------------------------------------------------

class Category(BaseModel):
    """One categorisation bucket the LLM may emit.

    Default ships as ``[bug, feature, question, docs, other]`` with
    one-line descriptions. The maintainer overrides this in
    ``config.yaml`` for their repo's taxonomy.
    """

    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=200)


class ModelPricing(BaseModel):
    """Per-1k-token pricing for the cost estimate.

    Optional — when a model isn't in the pricing map, the pre-flight
    estimate prints ``cost: unknown`` rather than aborting. Both
    fields are USD per 1000 tokens.
    """

    in_per_1k: float = Field(ge=0)
    out_per_1k: float = Field(ge=0)


# --- The main Config ---------------------------------------------------

_DEFAULT_CATEGORIES: list[Category] = [
    Category(name="bug", description="Something is broken or producing wrong output."),
    Category(name="feature", description="A request for new functionality."),
    Category(name="question", description="A user asking how to do something."),
    Category(name="docs", description="A documentation gap or correction."),
    Category(name="other", description="Anything that doesn't fit the above."),
]


class Config(BaseModel):
    """Validated configuration loaded from ``config.yaml``.

    Required fields (no defaults) are ``provider`` and ``model``.
    Everything else has a sensible default so a minimal config.yaml
    is enough to run.

    Why ``provider`` is a plain ``str`` (not an enum):
        The brief asks for swappable LLM providers without source
        edits. If the catalogue of known providers were hardcoded as
        an enum here, adding a third provider would force edits in
        this file *and* the CLI parser before the new implementation
        could even be tested. Validation that the provider name is
        *known* lives at the construction site instead —
        ``build_provider()`` raises a clear error if no implementation
        matches the configured name.
    """

    # --- Required identity ---
    provider: str
    model: str

    # --- Time window + output ---
    lookback_days: int = Field(default=7, ge=1, le=365)
    output_dir: Path = Field(default=Path("reports"))

    # --- Pathological-case safety net ---
    safety_max_issues: int = Field(default=1000, ge=1)

    # --- Per-call operational caps (single-call protection, not feature caps) ---
    max_tokens_per_call: int = Field(default=4000, ge=1)
    request_timeout_s: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=2, ge=0)

    # --- LLM-side knobs ---
    categories: list[Category] = Field(default_factory=lambda: list(_DEFAULT_CATEGORIES))

    # ``pricing`` keys are model names. Looked up at pre-flight estimate
    # time; unknown model -> cost shown as "unknown" without aborting.
    pricing: dict[str, ModelPricing] = {}

    @field_validator("provider", "model")
    @classmethod
    def _must_be_non_empty(cls, value: str) -> str:
        """Reject empty / whitespace-only strings for required name fields."""
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value.strip()

    @field_validator("categories")
    @classmethod
    def _categories_non_empty_unique(cls, value: list[Category]) -> list[Category]:
        """Reject empty list or duplicate category names."""
        if not value:
            raise ValueError("at least one category is required")
        names = [category.name for category in value]
        if len(names) != len(set(names)):
            raise ValueError(f"category names must be unique, got {names!r}")
        return value


def load_config(path: Path) -> Config:
    """Load and validate a YAML config file.

    Args:
        path: Path to ``config.yaml``.

    Returns:
        A validated ``Config`` instance.

    Raises:
        FileNotFoundError: if the file does not exist.
        yaml.YAMLError: if the file is not valid YAML.
        pydantic.ValidationError: if required fields are missing or values
            are out of range.
    """
    # ``safe_load`` of an empty string returns None; coerce to {} so the
    # Pydantic error names the missing required field rather than
    # complaining about a None input.
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)


# --- Prompt loading ----------------------------------------------------

def load_prompt(path: Path) -> Prompt:
    """Load a single markdown prompt file (YAML frontmatter + body).

    The frontmatter populates ``name`` / ``description`` / ``version`` /
    ``model_preferences``; the body becomes the system-message template.
    """
    parsed = frontmatter.load(str(path))
    return Prompt(
        name=parsed.metadata.get("name", path.stem),
        description=parsed.metadata.get("description", ""),
        version=str(parsed.metadata.get("version", "0.1.0")),
        model_preferences=parsed.metadata.get("model_preferences") or {},
        body=parsed.content,
    )


def load_prompts(prompts_dir: Path) -> dict[str, Prompt]:
    """Load all ``*.md`` prompt files in a directory, keyed by file stem.

    Returns a dict like ``{"categorise": Prompt(...), "summarise": ...}``
    so the pipeline can look prompts up by name.
    """
    return {
        path.stem: load_prompt(path)
        for path in sorted(prompts_dir.glob("*.md"))
    }


def load_maintainer_context(path: Path) -> str:
    """Load the maintainer's context preamble.

    Args:
        path: Path to ``maintainer_context.md`` (typically at the repo root).

    Returns:
        The file's contents as a string. Returns empty string if the file
        is missing — the prompts handle this gracefully via the template
        default in ``render_prompt``.
    """
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


# --- URL parsing -------------------------------------------------------

class InvalidRepoURL(ValueError):
    """Raised when the input URL is not a well-formed public GitHub repo URL."""


# More permissive than GitHub's own username rules but tight enough to
# defeat path-traversal and shell-injection inputs. Starts with
# alphanumeric, then any combination of alphanumerics, dots, underscores,
# hyphens.
_REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_NAME_LEN = 100  # GitHub's documented maximum for repository names.


def parse_repo_url(url: str) -> tuple[str, str]:
    """Parse a GitHub repo URL into ``(owner, repo)``.

    Strict on purpose. The returned values are used in filesystem
    paths and API URLs, so the parser rejects anything that could
    leak path-traversal segments or non-GitHub hosts.

    Accepts:
        - ``https://github.com/owner/repo``
        - ``https://github.com/owner/repo/`` (trailing slash)
        - ``https://github.com/owner/repo.git`` (clone-style suffix)

    Rejects:
        - Non-``github.com`` hosts (including ``www.github.com``)
        - SSH URLs (``git@github.com:owner/repo``)
        - Any path that doesn't have exactly two non-empty segments
        - owner / repo names containing path-traversal or shell characters

    Args:
        url: The URL string from the CLI.

    Returns:
        A tuple ``(owner, repo)`` with any trailing ``.git`` stripped.

    Raises:
        InvalidRepoURL: with a message naming what's wrong.
    """
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise InvalidRepoURL(
            f"URL must use http or https; got scheme {parsed.scheme!r} for {url!r}"
        )
    if parsed.hostname != "github.com":
        raise InvalidRepoURL(
            f"URL must point at github.com; got host {parsed.hostname!r} for {url!r}"
        )

    # Drop empty segments so a trailing slash or doubled slash doesn't
    # silently change the path's structure.
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 2:
        raise InvalidRepoURL(
            f"URL path must be exactly /owner/repo; got {parsed.path!r} for {url!r}"
        )

    owner, repo = segments
    # Strip optional .git suffix on the repo segment only.
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]

    for label, value in (("owner", owner), ("repo", repo)):
        if not value:
            raise InvalidRepoURL(f"empty {label} segment in URL: {url!r}")
        if len(value) > _MAX_NAME_LEN:
            raise InvalidRepoURL(
                f"{label} segment {value!r} exceeds {_MAX_NAME_LEN} characters"
            )
        if not _REPO_NAME_PATTERN.match(value):
            raise InvalidRepoURL(
                f"{label} segment {value!r} contains invalid characters; "
                f"must match {_REPO_NAME_PATTERN.pattern}"
            )

    return owner, repo
