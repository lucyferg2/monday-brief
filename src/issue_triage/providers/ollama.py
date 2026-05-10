"""Minimal Ollama provider, cloud-first.

Talks to Ollama's REST API via ``httpx``. The same code path covers
all three setups; only ``OLLAMA_HOST`` and ``OLLAMA_API_KEY`` change:

- **Cloud (default)**: host = https://ollama.com, ``OLLAMA_API_KEY``
  set, sent as ``Authorization: Bearer <key>``.
- **Local Ollama**: host = http://localhost:11434, no auth header.
- **Self-hosted with auth**: any host, ``OLLAMA_API_KEY`` set.

Structured output: when ``response_schema`` is passed we send the
generated JSON Schema in the ``format`` field so Ollama constrains
the output. The pipeline's ``parse_response`` still runs as a
defence-in-depth check against the same schema.

Credential handling: ``OLLAMA_API_KEY`` is read once in ``__init__``
into a local ``headers`` dict (kept as an attribute), never assigned
as a named attribute. ``repr(provider)`` deliberately omits headers.
"""

import json
import os
import time
from typing import Any

import httpx
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


_OLLAMA_CHAT_PATH = "/api/chat"
_DEFAULT_HOST = "https://ollama.com"


class OllamaProvider(ModelProvider):
    """Ollama via a small ``httpx`` client.

    Reads ``OLLAMA_HOST`` (default ``https://ollama.com``) and optional
    ``OLLAMA_API_KEY`` at construction. Non-streaming only — streaming
    adds complexity the pipeline doesn't need.
    """

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        host = os.getenv("OLLAMA_HOST", _DEFAULT_HOST).rstrip("/")
        api_key = os.getenv("OLLAMA_API_KEY")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._headers = headers
        self._client = httpx.Client(
            base_url=host,
            timeout=config.request_timeout_s,
        )
        self._host = host  # for repr only — never the key

    def __repr__(self) -> str:
        # Deliberately omits headers / credentials.
        return f"OllamaProvider(host={self._host!r}, model={self._config.model!r})"

    def close(self) -> None:
        """Release the underlying HTTP client."""
        self._client.close()

    def complete(
        self,
        messages: list[Message],
        response_schema: type[BaseModel] | None = None,
    ) -> ProviderResponse:
        """Send messages to Ollama and return a ProviderResponse."""
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "stream": False,
            "options": {"num_predict": self._config.max_tokens_per_call},
        }
        if response_schema is not None:
            # Sending the JSON Schema (not just "json") constrains the
            # output structure. Ollama supports both; the schema form
            # is strictly better for our use.
            payload["format"] = response_schema.model_json_schema()

        start = time.monotonic()
        try:
            response = self._client.post(
                _OLLAMA_CHAT_PATH, json=payload, headers=self._headers,
            )
        except httpx.RequestError as exc:
            raise ProviderUnavailable(
                f"Could not reach Ollama at {self._host}. "
                f"Check OLLAMA_HOST and OLLAMA_API_KEY. ({exc})"
            ) from exc
        duration = time.monotonic() - start

        if response.status_code >= 500:
            raise ProviderUnavailable(
                f"Ollama server error {response.status_code} from {self._host}"
            )
        if response.status_code != 200:
            body_snippet = response.text[:200]
            raise ProviderError(
                f"Ollama returned {response.status_code}: {body_snippet}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"Ollama returned non-JSON response: {exc}"
            ) from exc

        text = _extract_content(data)
        tokens_in = data.get("prompt_eval_count", 0) or 0
        tokens_out = data.get("eval_count", 0) or 0

        return ProviderResponse(
            text=text,
            parsed=None,  # pipeline's parse_response handles validation
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_seconds=duration,
        )


def _extract_content(data: dict) -> str:
    """Pull the assistant text out of Ollama's response shape.

    Ollama's /api/chat typically returns ``{"message": {"content": "..."}}``;
    some compat shims return an OpenAI-style ``choices[0].message.content``.
    Falling back to the raw JSON dump lets the caller surface something
    even if the shape's unfamiliar.
    """
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
    return json.dumps(data)


register("ollama", OllamaProvider)
