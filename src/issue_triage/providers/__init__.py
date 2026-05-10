"""Provider abstraction.

This module defines the contract that every LLM provider conforms to:

- ``Message`` — the role+content shape sent to the provider.
- ``ProviderResponse`` — the typed return value (text + parsed object
  + token counts + duration).
- ``ModelProvider`` — the ABC. One method, ``complete()``.
- ``build_provider(config)`` — the factory that turns a string name
  from ``config.yaml`` into a concrete provider. Validation that the
  string maps to a real implementation lives here, not in the Config
  model — so adding a new provider is one new file in this package
  plus one entry in the registry below.

Concrete implementations (``GeminiProvider``, ``OllamaProvider``)
land in their own files in T1.3 and register themselves in
``_REGISTRY`` at import time.
"""

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel

from issue_triage.config import Config


class Message(BaseModel):
    """One turn in the LLM conversation.

    We only ever send two messages — one ``system`` (role anchor +
    maintainer context + instructions) and one ``user`` (the wrapped
    issue text). ``assistant`` is here for completeness in case of
    future few-shot use; not used in v1.
    """

    role: Literal["system", "user", "assistant"]
    content: str


class ProviderResponse(BaseModel):
    """What ``ModelProvider.complete()`` returns.

    When ``response_schema`` was passed and the provider supports
    structured-output mode natively, ``parsed`` is the validated
    Pydantic instance and ``text`` is the same content as a JSON
    string (for logging). When the provider falls back to free-text
    JSON parsing, ``parsed`` is ``None`` and the caller parses
    ``text`` themselves.
    """

    text: str
    parsed: Any | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    duration_seconds: float = 0.0


class ModelProvider(ABC):
    """Single-method ABC. One concrete subclass per provider.

    ``complete()`` is the only method any caller ever uses. Per-call
    operational caps (``max_tokens_per_call``, ``request_timeout_s``,
    ``max_retries``) come from the ``Config`` passed at construction.
    """

    def __init__(self, config: Config) -> None:
        """Store config. Subclasses should call ``super().__init__()``."""
        self._config = config

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        response_schema: type[BaseModel] | None = None,
    ) -> ProviderResponse:
        """Send ``messages`` to the LLM and return the response.

        Args:
            messages: The system + user messages, in order.
            response_schema: Pydantic model the response must conform
                to. When set, the provider uses native structured-output
                mode (Gemini ``response_mime_type="application/json"`` +
                ``response_schema=...``; Ollama ``format="json"``) and
                returns the parsed instance in ``ProviderResponse.parsed``.
                When ``None``, returns raw text only.

        Returns:
            A ``ProviderResponse`` populated with text + (optional
            parsed) + token counts + duration.

        Raises:
            ProviderUnavailable: if the provider can't be reached
                (network down, missing API key, Ollama not running).
            ProviderError: if the provider returned a non-success
                response after retries.
        """


# --- Provider-construction errors --------------------------------------

class ProviderUnavailable(RuntimeError):
    """Raised when the provider can't be reached or set up.

    Distinct from ``ProviderError`` (which is "the API said no");
    this means we never got to make the call. The CLI catches both
    and produces a single-line user-facing message.
    """


class ProviderError(RuntimeError):
    """Raised on non-success responses from the provider."""


# --- The registry + factory --------------------------------------------

# Concrete providers register themselves here. T1.3 will add ``"gemini"``
# and ``"ollama"`` entries when the implementation files import this
# module. Adding a third provider is one new file + one ``register()``
# call — no edits to ``Config``, the CLI, or any other consumer.
_REGISTRY: dict[str, type[ModelProvider]] = {}


def register(name: str, provider_cls: type[ModelProvider]) -> None:
    """Register a concrete provider class under its config name.

    Called from each provider module at import time. Idempotent for
    the same (name, class) pair.
    """
    if name in _REGISTRY and _REGISTRY[name] is not provider_cls:
        raise ValueError(
            f"provider name {name!r} is already registered to a different class"
        )
    _REGISTRY[name] = provider_cls


def build_provider(config: Config) -> ModelProvider:
    """Construct the concrete ``ModelProvider`` for ``config.provider``.

    Args:
        config: The validated Config.

    Returns:
        A ready-to-use provider instance.

    Raises:
        ProviderUnavailable: if the configured provider name isn't
            registered, or the provider's setup fails (missing API
            key, can't reach Ollama, etc.). The error message lists
            the registered providers so the user can spot a typo.
    """
    name = config.provider.strip().lower()
    cls = _REGISTRY.get(name)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered yet)"
        raise ProviderUnavailable(
            f"unknown provider {name!r} in config.yaml. Known providers: {known}. "
            f"Add a new provider by dropping a file in src/issue_triage/providers/ "
            f"that calls register('<name>', <ClassName>)."
        )
    return cls(config)
