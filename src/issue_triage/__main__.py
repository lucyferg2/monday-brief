"""Command-line entry point for ``issue-triage``.

This module is intentionally small. It does four things:

1. Parses CLI arguments (one positional URL plus optional override flags).
2. Validates the URL and loads ``config.yaml``.
3. Applies any CLI overrides on top of the loaded ``Config``.
4. Hands off to the pipeline (later tasks; for now it prints what it
   would have done).

Secrets are read from the process environment via ``os.getenv()`` at the point of
use; the user sets them in their shell before running. The README
documents which env vars to set per provider.

Errors at every boundary turn into a single human-readable line on
stderr and a non-zero exit code — never a raw stack trace.
"""

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path


# Env-var names whose values should never appear in log output.
# Listed by NAME, not value — the redaction filter resolves them at
# emit time, so this list stays env-agnostic and provider-agnostic.
_REDACTED_ENV_VARS = (
    "GITHUB_TOKEN",
    "GEMINI_API_KEY",
    "OLLAMA_API_KEY",
)


class _RedactingFilter(logging.Filter):
    """Scrub known credential env-var values from any log record.

    Defence in depth: the codebase already avoids logging credentials
    directly, but third-party libraries can include request data in
    their warnings / exceptions. This filter runs against every record
    before it's emitted and replaces any occurrence of a configured
    credential value with ``[REDACTED]``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for name in _REDACTED_ENV_VARS:
            value = os.environ.get(name)
            if value and value in message:
                # Overwrite both the cached message and the formatting
                # inputs so str(record) on any downstream handler also
                # sees the redacted form.
                redacted = message.replace(value, "[REDACTED]")
                record.msg = redacted
                record.args = ()
                message = redacted
        return True

# Issue titles, comment bodies and LLM rationales contain Unicode
# (emoji, non-ASCII names, code points outside cp1252). Force the CLI's
# stdout/stderr to UTF-8 so the same code prints cleanly on Windows
# PowerShell as well as POSIX shells. errors='replace' is a safety net
# for the very rare unencodable byte; the alternative is a crash, which
# isn't worth it for a console writer.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from issue_triage import __version__
from issue_triage.config import (
    Config,
    InvalidRepoURL,
    load_config,
    load_maintainer_context,
    load_prompts,
    parse_repo_url,
)
from issue_triage.github import (
    GitHubClient,
    GitHubError,
    RateLimitError,
    TooManyIssuesError,
)
from issue_triage.pipeline import run_pipeline
from issue_triage.providers import (
    ProviderError,
    ProviderUnavailable,
    build_provider,
)
from issue_triage.render import (
    render_html,
    render_json,
    render_markdown,
    render_run_metadata,
)


# The prompt files ship inside the package, alongside this module.
_PROMPTS_DIR = Path(__file__).parent / "prompts"


_LOG = logging.getLogger("issue_triage")


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser.

    Returns:
        A configured ``ArgumentParser``. The only required argument is
        the repo URL; everything else is an optional override for a
        value that normally lives in ``config.yaml``.
    """
    parser = argparse.ArgumentParser(
        prog="issue-triage",
        description=(
            "Generate a Monday-morning brief of open issues for a public "
            "GitHub repo. Provider, model and other knobs are read from "
            "config.yaml; the flags below override config.yaml for "
            "testing or scripting."
        ),
    )
    parser.add_argument(
        "url",
        help="Public GitHub repo URL, e.g. https://github.com/owner/repo",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml (default: ./config.yaml).",
    )
    parser.add_argument(
        "--provider",
        help="Override the provider declared in config.yaml. Accepted "
             "values are determined by the implementations registered "
             "under src/issue_triage/providers/ (e.g. gemini, ollama).",
    )
    parser.add_argument(
        "--model",
        help="Override the model name declared in config.yaml.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        help="Override the lookback window in days (config default: 7).",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="Hard cost ceiling in USD. Aborts before any LLM call if "
             "the pre-flight estimate exceeds this value.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the pre-flight estimate confirmation prompt "
             "(CI / scripted use).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override the output directory (config default: ./reports).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging (includes per-issue body content).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    """Apply CLI overrides to a loaded ``Config``.

    Returns a new ``Config`` so the original (loaded from YAML) stays
    available for round-trip / debugging. Only fields the user explicitly
    set on the command line are overridden.
    """
    updates: dict[str, object] = {}
    if args.provider:
        updates["provider"] = args.provider
    if args.model:
        updates["model"] = args.model
    if args.lookback_days is not None:
        updates["lookback_days"] = args.lookback_days
    if args.output_dir:
        updates["output_dir"] = args.output_dir
    return config.model_copy(update=updates) if updates else config


def _merge_dir_into(src: Path, dst: Path) -> None:
    """Move every entry under ``src`` into ``dst``, removing ``src`` when done.

    Recurses on subdirectories so date / repo folders merge cleanly when
    archiving. If a destination entry already exists it's overwritten —
    archived runs supersede whatever was already there.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_dir() and target.exists() and target.is_dir():
            _merge_dir_into(entry, target)
        else:
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(entry), str(target))
    src.rmdir()


def _archive_old_runs(output_dir: Path, today_folder_name: str) -> int:
    """Move any non-today date folder under ``output_dir`` into ``archive/``.

    Keeps ``reports/`` showing only today's runs at the top level; older
    briefs accumulate under ``reports/archive/<date>/<repo>/``. Returns
    the number of date folders moved (for the user-facing log line).
    """
    if not output_dir.exists():
        return 0
    archive_root = output_dir / "archive"
    moved = 0
    for entry in sorted(output_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Don't move the archive itself or today's folder.
        if entry.name in {"archive", today_folder_name}:
            continue
        target = archive_root / entry.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            _merge_dir_into(entry, target)
        else:
            shutil.move(str(entry), str(target))
        moved += 1
    return moved


# --- Pre-flight estimate -----------------------------------------------

# Rough constants for the ballpark estimate. Derived empirically from a
# real run on firebase/firebase-js-sdk: ~26.7k input tokens / ~1.7k
# output tokens across 34 calls → ~785 tokens in and ~50 out per call.
# Rounded up for headroom; pre-flight is intentionally a ballpark.
_AVG_TOKENS_IN_PER_CALL = 1000
_AVG_TOKENS_OUT_PER_CALL = 100
_AVG_DURATION_PER_CALL_S = 3.0


def _estimate_run(
    new_count: int, ongoing_count: int, config: Config,
) -> dict:
    """Compute a rough pre-flight estimate of LLM calls / tokens / cost / duration.

    The estimate is a ballpark — useful for distinguishing a $0.05 run
    from a $5 run, not a billing-grade prediction. Cost is ``None`` when
    pricing isn't configured for the chosen model.
    """
    # 3 calls per §1 (categorise + summarise + prioritise); +1 for §2
    # entries (new_activity). +1 for the cross-issue themes pass when
    # §1 has 3+ items.
    calls = 3 * new_count + 4 * ongoing_count
    if new_count >= 3:
        calls += 1

    tokens_in = calls * _AVG_TOKENS_IN_PER_CALL
    tokens_out = calls * _AVG_TOKENS_OUT_PER_CALL

    pricing = config.pricing.get(config.model)
    cost: float | None = None
    if pricing:
        cost = (
            (tokens_in / 1000) * pricing.in_per_1k
            + (tokens_out / 1000) * pricing.out_per_1k
        )

    return {
        "calls": calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost": cost,
        "duration_s": calls * _AVG_DURATION_PER_CALL_S,
    }


def _print_estimate(estimate: dict, model: str) -> None:
    """Print the pre-flight estimate to stdout in a scannable format."""
    cost_line = (
        f"${estimate['cost']:.4f}"
        if estimate["cost"] is not None
        else f"unknown (no pricing for {model!r} in config.yaml)"
    )
    print()
    print("Pre-flight estimate")
    print(f"  LLM calls:           ~{estimate['calls']}")
    print(
        f"  Tokens:              "
        f"~{estimate['tokens_in']:,} in / ~{estimate['tokens_out']:,} out"
    )
    print(f"  Estimated cost:      ~{cost_line}")
    print(f"  Estimated duration:  ~{estimate['duration_s']:.0f}s")
    print()


def _confirm_proceed(
    estimate: dict, args: argparse.Namespace, model: str,
) -> bool:
    """Print the estimate and return whether the run should proceed.

    Returns False when ``--max-cost`` is set and would be exceeded, or
    when the user (or piped stdin) declines the prompt. ``--yes`` skips
    the prompt entirely.
    """
    _print_estimate(estimate, model)

    if args.max_cost is not None:
        if estimate["cost"] is None:
            print(
                f"error: --max-cost ${args.max_cost:.2f} was set, but no "
                f"pricing for {model!r} is in config.yaml — cannot enforce "
                f"the ceiling. Add a `pricing.{model}` entry or remove "
                f"--max-cost.",
                file=sys.stderr,
            )
            return False
        if estimate["cost"] > args.max_cost:
            print(
                f"error: estimated cost ${estimate['cost']:.4f} exceeds "
                f"--max-cost ${args.max_cost:.2f}. Aborting before any "
                f"LLM call.",
                file=sys.stderr,
            )
            return False

    if args.yes:
        print("Proceeding (--yes).")
        return True

    try:
        reply = input("Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted by user.", file=sys.stderr)
        return False
    if reply in {"y", "yes"}:
        return True
    print("Aborted.", file=sys.stderr)
    return False


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Explicit argument list, used by tests. When ``None``,
            argparse reads from ``sys.argv``.

    Returns:
        Process exit code: 0 on success, 1 on any expected error
        (bad URL, missing config, invalid config). Higher codes are
        reserved for future task-specific failures.
    """
    args = build_parser().parse_args(argv)

    # Logging set up early so any error after this point is surfaced
    # consistently. The format here is intentionally plain — a JSON-line
    # formatter would help machine-readable log aggregation but isn't
    # needed for a single-shot CLI.
    #
    # Provider-agnostic idiom for "show our logs but not the libraries'":
    # the root logger sits at WARNING by default, so any third-party
    # library — HTTP clients, LLM SDKs, anything — stays quiet without
    # us having to enumerate specific names. This package's own logger
    # sits at INFO (or DEBUG with --verbose). New dependencies inherit
    # WARNING automatically; nothing in the CLI ever names a specific
    # provider library. --verbose lifts the root level too so all
    # third-party traces show up for debugging.
    package_level = logging.DEBUG if args.verbose else logging.INFO
    root_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=root_level,
        format="%(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("issue_triage").setLevel(package_level)
    # Attach the redaction filter to every handler currently on the
    # root logger. The filter scrubs known credential env-var values
    # from any record before it's emitted — defence in depth against
    # third-party libraries accidentally surfacing request data in
    # warnings or exception messages.
    redactor = _RedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)

    try:
        owner, repo = parse_repo_url(args.url)
    except InvalidRepoURL as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(
            f"error: config file not found: {args.config}\n"
            f"hint:  copy config.example.yaml to config.yaml and edit it.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        # Catches yaml.YAMLError, pydantic.ValidationError, etc. The
        # message Pydantic produces names the offending field, which
        # is what the user needs.
        print(f"error: failed to load config: {exc}", file=sys.stderr)
        return 1

    config = _apply_overrides(config, args)

    # Run intent + fetch summary go to stdout (pipe-friendly); diagnostic
    # logs go to stderr.
    print(f"issue-triage v{__version__}")
    print(f"  repo:     {owner}/{repo}")
    print(f"  provider: {config.provider}")
    print(f"  model:    {config.model}")
    print(f"  lookback: {config.lookback_days} days")
    print(f"  output:   {config.output_dir}")
    print()

    # 1. Fetch issues. Each known failure mode prints a clean line and exits 1.
    try:
        with GitHubClient(config) as gh:
            new_issues, ongoing_activity = gh.fetch_issues(
                owner, repo, config.lookback_days,
            )
    except RateLimitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except TooManyIssuesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except GitHubError as exc:
        print(f"error: GitHub fetch failed: {exc}", file=sys.stderr)
        return 1

    print(f"  New issues this week:  {len(new_issues)}")
    print(f"  Ongoing activity:      {len(ongoing_activity)}")

    if not new_issues and not ongoing_activity:
        print()
        print("Nothing to brief on this week. Exiting cleanly.")
        return 0

    # 2. Load prompts and maintainer context.
    prompts = load_prompts(_PROMPTS_DIR)
    maintainer_context = load_maintainer_context(Path("maintainer_context.md"))

    # 3. Build provider. Construction itself can fail (missing API key,
    # Ollama unreachable) — surface those as clean errors.
    try:
        provider = build_provider(config)
    except ProviderUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # 4. Pre-flight estimate + consent. The cost surface is bounded by
    # transparency, not truncation: the user sees the projected cost
    # before any LLM call and confirms (or --yes skips the prompt;
    # --max-cost aborts cleanly if a ceiling would be exceeded).
    estimate = _estimate_run(len(new_issues), len(ongoing_activity), config)
    if not _confirm_proceed(estimate, args, config.model):
        return 1

    # 5. Run the pipeline (categorise → prioritise → summarise → new_activity
    # for ongoing entries → themes pass when §1 has ≥ 3 issues).
    print()
    print("Running LLM pipeline...")
    try:
        brief = run_pipeline(
            new_issues=new_issues,
            ongoing=ongoing_activity,
            provider=provider,
            prompts=prompts,
            config=config,
            maintainer_context=maintainer_context,
            repo_full_name=f"{owner}/{repo}",
            lookback_days=config.lookback_days,
        )
    except (ProviderError, ProviderUnavailable) as exc:
        print(f"error: provider call failed: {exc}", file=sys.stderr)
        return 1

    # 6. Render the four output files. The renderers are pure functions
    # over the canonical Brief — no I/O. The file writing happens here.
    #
    # Filenames are dated (UK format: DD-MM-YY) so they survive being
    # moved out of the date folder — a reviewer can tell at a glance
    # which run a brief.html in their downloads folder belongs to.
    # Folder structure stays ISO (YYYY-MM-DD) for sortable listing.
    #
    # Before writing, any non-today date folder under reports/ is
    # moved into reports/archive/<date>/, so the top-level reports/
    # folder always shows just today's runs plus an archive subfolder.
    rm = brief.run_metadata
    date_folder = brief.generated_at.strftime("%Y-%m-%d")
    date_suffix = brief.generated_at.strftime("%d-%m-%y")

    archived = _archive_old_runs(config.output_dir, date_folder)
    if archived:
        print(
            f"  Archived {archived} previous date folder"
            f"{'' if archived == 1 else 's'} to "
            f"{config.output_dir / 'archive'}/"
        )

    out_dir = config.output_dir / date_folder / f"{owner}__{repo}"
    out_dir.mkdir(parents=True, exist_ok=True)

    files = {
        f"brief_{date_suffix}.md":   render_markdown(brief),
        f"brief_{date_suffix}.json": render_json(brief),
        f"brief_{date_suffix}.html": render_html(brief),
        f"run_{date_suffix}.json":   render_run_metadata(rm),
    }
    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8")

    print(
        f"Pipeline complete. "
        f"calls={rm.llm_calls}  "
        f"tokens=in:{rm.tokens_in} out:{rm.tokens_out}  "
        f"duration={rm.duration_seconds}s  "
        f"themes={len(brief.themes)}  "
        f"parse_failures={len(rm.parse_failures)}  "
        f"injection_warnings={len(rm.injection_warnings)}"
    )
    print()
    print(f"Brief written to {out_dir}/")
    print(f"  brief_{date_suffix}.md      — Markdown for reading")
    print(f"  brief_{date_suffix}.json    — canonical structured payload")
    print(f"  brief_{date_suffix}.html    — single-file styled report")
    print(f"  run_{date_suffix}.json      — reproducibility snapshot")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry
    sys.exit(main())
