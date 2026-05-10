"""Gemini provider implementation using the ``google-genai`` SDK.

Gemini's API takes a single string of "contents" plus a separate
``system_instruction``, rather than the OpenAI-style
``[{role, content}]`` list. This module bridges those: any messages
with ``role="system"`` get joined into ``system_instruction``; the
rest are concatenated into ``contents``. For our use case we
typically send one system + one user message, so the join is a
trivial concat — but it generalises if we ever add few-shot examples.

Structured output uses Gemini's native mode: when ``response_schema``
is passed, the SDK is told to return JSON conforming to that schema
and the parsed object lands in ``ProviderResponse.parsed``.

Credential handling: ``GEMINI_API_KEY`` is read once in ``__init__``,
passed into the SDK's ``Client`` (which holds it internally), and
discarded as a local variable. It's never stored as an attribute on
this class, so ``repr(provider)`` can't accidentally leak it.
"""

import logging
import os
import time
from typing import Any

from google import genai
from pydantic import BaseModel

from issue_triage.config import Config
from issue_triage.providers import (
    Message,
    ModelProvider,
    ProviderError,
    ProviderResponse,
    ProviderUnavailable,
    register,
)


_LOG = logging.getLogger(__name__)


class GeminiProvider(ModelProvider):
    """Google Gemini via the google-genai SDK.

    Reads ``GEMINI_API_KEY`` from the environment at construction. The
    actual model name comes from ``config.model`` (no hardcoded
    default — the brief explicitly forbids it).
    """

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ProviderUnavailable(
                "GEMINI_API_KEY is not set. Get one at "
                "https://aistudio.google.com/apikey, then set it in your "
                "shell (PowerShell: $env:GEMINI_API_KEY = '...'; "
                "bash: export GEMINI_API_KEY='...')."
            )
        # The SDK's Client holds the key internally; we don't keep a
        # local attribute, so repr/str of this provider can never leak it.
        self._client = genai.Client(api_key=api_key)

    def __repr__(self) -> str:
        # Explicitly omit credentials. Tested by repr-doesn't-leak smoke.
        return f"GeminiProvider(model={self._config.model!r})"

    def complete(
        self,
        messages: list[Message],
        response_schema: type[BaseModel] | None = None,
    ) -> ProviderResponse:
        """Call Gemini and return text + (optional parsed) + token counts."""
        system_parts = [m.content for m in messages if m.role == "system"]
        user_parts = [m.content for m in messages if m.role == "user"]
        if not user_parts:
            raise ValueError("at least one user message is required")

        # Gemini config dict — the SDK calls this `config` on
        # generate_content. The fields we set:
        #
        # - system_instruction: the joined system text (role anchor +
        #   maintainer context).
        # - max_output_tokens: per-call cap from Config.
        # - response_mime_type / response_schema: only when the caller
        #   asked for structured output.
        sdk_config: dict[str, Any] = {
            "max_output_tokens": self._config.max_tokens_per_call,
        }
        if system_parts:
            sdk_config["system_instruction"] = "\n\n".join(system_parts)
        if response_schema is not None:
            sdk_config["response_mime_type"] = "application/json"
            sdk_config["response_schema"] = response_schema

        start = time.monotonic()
        try:
            response = self._client.models.generate_content(
                model=self._config.model,
                contents="\n\n".join(user_parts),
                config=sdk_config,
            )
        except Exception as exc:  # broad on purpose: SDK exceptions vary
            raise ProviderError(f"Gemini call failed: {exc}") from exc
        duration = time.monotonic() - start

        text = response.text or ""
        # The SDK populates `.parsed` on the response when a schema was
        # given. It's None otherwise, which is what we want.
        parsed = getattr(response, "parsed", None) if response_schema else None

        # Usage metadata field names follow Google's naming.
        usage = getattr(response, "usage_metadata", None)
        tokens_in = (getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
        tokens_out = (getattr(usage, "candidates_token_count", 0) or 0) if usage else 0

        return ProviderResponse(
            text=text,
            parsed=parsed,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_seconds=duration,
        )


register("gemini", GeminiProvider)
