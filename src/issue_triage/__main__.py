"""Command-line entry point for ``issue-triage``.

This module is intentionally small. It does four things:

1. Parses CLI arguments (one positional URL plus optional override flags).
2. Validates the URL and loads ``config.yaml``.
3. Applies any CLI overrides on top of the loaded ``Config``.
4. Hands off to the pipeline (later tasks; for now it prints what it
   would have done).

Secrets (``GEMINI_API_KEY``, ``GITHUB_TOKEN``, ``OLLAMA_API_KEY``) are
read from the process environment via ``os.getenv()`` at the point of
use; the user sets them in their shell before running. The README
documents which env vars to set per provider.

Errors at every boundary turn into a single human-readable line on
stderr and a non-zero exit code — never a raw stack trace.
"""

import argparse
import logging
import sys
from pathlib import Path

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
    parse_repo_url,
)
from issue_triage.github import (
    GitHubClient,
    GitHubError,
    RateLimitError,
    TooManyIssuesError,
)


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
    # consistently. The JSON-line formatter lands in T1.8.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    # Quiet noisy third-party loggers — httpx logs every request at INFO
    # by default, which clutters the user-facing output. Keep them at
    # WARNING unless we're in --verbose mode (where the request trace
    # is genuinely useful for debugging).
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

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

    # Fetch issues. Each known failure mode prints a clean line and exits 1.
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

    print(f"  §1 new issues:        {len(new_issues)}")
    print(f"  §2 ongoing activity:  {len(ongoing_activity)}")
    print()
    print("(fetch complete — LLM pipeline lands in T1.3+)")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry
    sys.exit(main())
