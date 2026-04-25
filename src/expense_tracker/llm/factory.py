"""Construct the configured LLM client.

Single entry point for the rest of the application:

    from expense_tracker.llm import get_llm_client
    client = get_llm_client()

The factory reads :class:`~expense_tracker.config.Settings` and returns a
provider-specific concrete class behind the
:class:`~expense_tracker.llm.base.LLMClient` protocol. Callers never
import a provider directly — they interact only with the protocol.

All provider imports are local (inside the relevant ``if`` branch) so:

* Pulling a single provider doesn't drag the others' SDKs into memory.
* Optional providers (OpenAI, Anthropic) don't break startup if their
  SDK isn't installed — failure happens only when actually selected,
  with an actionable error message.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ._fake import FakeLLMClient
from .base import LLMClient
from .exceptions import LLMConfigError


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Return a fresh LLM client for the configured provider.

    ``settings`` is optional — when ``None`` the global cached
    :class:`Settings` is used. Passing one explicitly is the recommended
    pattern for tests so they can override env without touching the
    global singleton.

    If ``LLM_TRACE`` is true (default), the returned client is wrapped in
    a :class:`~expense_tracker.llm._traced.TracedLLMClient` that records
    every call to the configured chat store. The wrapper is fully
    transparent — it satisfies the same protocol and forwards all calls.
    """
    cfg = settings or get_settings()
    raw = _build_raw(cfg)
    return _maybe_wrap_with_tracer(raw, cfg)


def _build_raw(cfg: Settings) -> LLMClient:
    provider = cfg.LLM_PROVIDER

    if provider == "fake":
        return FakeLLMClient()

    if provider == "groq":
        from .groq_client import GroqClient

        return GroqClient(
            api_key=_require_secret(cfg.GROQ_API_KEY, "GROQ_API_KEY"),
            model=cfg.GROQ_MODEL,
            timeout_s=cfg.LLM_TIMEOUT_S,
            max_retries=cfg.LLM_MAX_RETRIES,
            default_temperature=cfg.LLM_TEMPERATURE,
            default_max_tokens=cfg.LLM_MAX_TOKENS,
        )

    if provider == "ollama":
        from .ollama_client import OllamaClient

        return OllamaClient(
            base_url=cfg.OLLAMA_BASE_URL,
            model=cfg.OLLAMA_MODEL,
            timeout_s=cfg.LLM_TIMEOUT_S,
            max_retries=cfg.LLM_MAX_RETRIES,
            default_temperature=cfg.LLM_TEMPERATURE,
            default_max_tokens=cfg.LLM_MAX_TOKENS,
        )

    if provider == "openai":
        from .openai_client import OpenAIClient

        return OpenAIClient(
            api_key=_require_secret(cfg.OPENAI_API_KEY, "OPENAI_API_KEY"),
            model=cfg.OPENAI_MODEL,
            timeout_s=cfg.LLM_TIMEOUT_S,
            max_retries=cfg.LLM_MAX_RETRIES,
            default_temperature=cfg.LLM_TEMPERATURE,
            default_max_tokens=cfg.LLM_MAX_TOKENS,
        )

    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(
            api_key=_require_secret(cfg.ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY"),
            model=cfg.ANTHROPIC_MODEL,
            timeout_s=cfg.LLM_TIMEOUT_S,
            max_retries=cfg.LLM_MAX_RETRIES,
            default_temperature=cfg.LLM_TEMPERATURE,
            default_max_tokens=cfg.LLM_MAX_TOKENS,
        )

    # Unreachable in well-typed code (Settings.LLM_PROVIDER is a Literal),
    # but defensive fallback in case of a corrupted .env or a future
    # Literal extension that hasn't been wired here yet.
    raise LLMConfigError(  # pragma: no cover
        f"Unknown LLM_PROVIDER={provider!r}. "
        f"Expected one of: groq, ollama, openai, anthropic, fake"
    )


def _maybe_wrap_with_tracer(raw: LLMClient, cfg: Settings) -> LLMClient:
    """Optionally wrap *raw* in a tracing decorator.

    Tracing is opt-out (default ``True``) because the cost is negligible
    and the debugging value is high. We import lazily so the storage
    package isn't loaded when tracing is disabled.
    """
    if not cfg.LLM_TRACE:
        return raw

    from ..storage import get_chat_store
    from ._traced import TracedLLMClient

    store = get_chat_store(cfg)
    return TracedLLMClient(inner=raw, store=store)


def _require_secret(value, env_name: str) -> str:
    """Unwrap a ``SecretStr`` field, raising a friendly error if unset."""
    if value is None:
        raise LLMConfigError(
            f"{env_name} is not set. Add it to .env or your environment.",
        )
    # ``SecretStr`` exposes the underlying string only via ``.get_secret_value()``.
    return value.get_secret_value()
