"""Manual verification helper for the LLM provider abstraction.

Builds whichever provider is configured in ``config.yaml`` (with any
``--provider`` / ``--model`` overrides), sends a tiny test prompt,
and prints the response, token counts and duration. Use it to confirm
that the API key / Ollama instance is reachable before running the
full pipeline.

Usage (with config.yaml in place and the matching env var set):

    python scripts/verify_provider.py
    python scripts/verify_provider.py --provider ollama --model llama3
    python scripts/verify_provider.py --schema   # exercise structured output

Exits 0 on a successful response, non-zero on any provider error.
"""

import argparse
import sys
from pathlib import Path

# Force UTF-8 stdout so emoji / non-ASCII text in responses prints
# cleanly on Windows PowerShell (cp1252 by default).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pydantic import BaseModel

from issue_triage.config import load_config
from issue_triage.providers import (
    Message,
    ProviderError,
    ProviderUnavailable,
    build_provider,
)


class _Greeting(BaseModel):
    """Tiny schema used to exercise structured-output mode."""

    greeting: str
    language: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--provider",
        help="Override config.yaml's provider (e.g. gemini, ollama).",
    )
    parser.add_argument(
        "--model",
        help="Override config.yaml's model.",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Use structured-output mode (response_schema=_Greeting).",
    )
    args = parser.parse_args()

    config = load_config(Path("config.yaml"))
    updates: dict = {}
    if args.provider:
        updates["provider"] = args.provider
    if args.model:
        updates["model"] = args.model
    if updates:
        config = config.model_copy(update=updates)

    print(f"provider: {config.provider}")
    print(f"model:    {config.model}")
    print(f"schema:   {'on' if args.schema else 'off'}")
    print()

    try:
        provider = build_provider(config)
    except ProviderUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    messages = [
        Message(
            role="system",
            content="You are a friendly tester. Respond in the requested format.",
        ),
        Message(
            role="user",
            content=(
                "Say hello in 5 words or fewer. "
                "If asked for JSON, return {\"greeting\": ..., \"language\": ...}."
            ),
        ),
    ]

    schema = _Greeting if args.schema else None
    try:
        response = provider.complete(messages, response_schema=schema)
    except (ProviderError, ProviderUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("--- response ---")
    print(response.text or "(empty text)")
    if response.parsed is not None:
        print()
        print("--- parsed (schema) ---")
        print(response.parsed)
    print()
    print(
        f"--- usage --- "
        f"in:{response.tokens_in}  "
        f"out:{response.tokens_out}  "
        f"duration:{response.duration_seconds:.2f}s"
    )
    print()
    print("--- repr (should NOT contain credentials) ---")
    print(repr(provider))
    return 0


if __name__ == "__main__":
    sys.exit(main())
