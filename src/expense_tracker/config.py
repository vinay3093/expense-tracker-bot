"""Centralized application configuration.

All configuration goes through one ``Settings`` class so that:

* Type errors fail at import time, not at runtime in some far-away function.
* There is a single source of truth for "what env var is named what" — no
  ``os.getenv("...")`` sprinkled across the codebase.
* Tests can override values cleanly by instantiating ``Settings(...)``
  with kwargs, bypassing the real environment.

Loading order (pydantic-settings default):

    constructor kwargs  >  environment variables  >  .env file  >  defaults

So tests pass kwargs; production reads env / .env; everything else gets a
sensible default.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["groq", "ollama", "openai", "anthropic", "fake"]
ChatStoreBackend = Literal["jsonl"]


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Read once via :func:`get_settings()` and reused for the rest of the
    process. To override in tests, call
    :func:`reset_settings_cache_for_tests` and instantiate ``Settings(...)``
    directly.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ─── LLM provider routing ───────────────────────────────────────────────
    LLM_PROVIDER: ProviderName = Field(
        default="groq",
        description=(
            "Which LLM backend to use. Switch the entire stack with one env var."
        ),
    )

    # ─── Groq (primary, free tier, OpenAI-compatible) ───────────────────────
    GROQ_API_KEY: SecretStr | None = Field(default=None)
    GROQ_MODEL: str = Field(default="llama-3.1-8b-instant")

    # ─── Ollama (local, offline fallback) ───────────────────────────────────
    OLLAMA_BASE_URL: str = Field(default="http://localhost:11434")
    OLLAMA_MODEL: str = Field(default="llama3.1")

    # ─── OpenAI (ChatGPT — kept ready, not enabled by default) ──────────────
    OPENAI_API_KEY: SecretStr | None = Field(default=None)
    OPENAI_MODEL: str = Field(default="gpt-4o-mini")

    # ─── Anthropic (Claude — kept ready, not enabled by default) ────────────
    ANTHROPIC_API_KEY: SecretStr | None = Field(default=None)
    ANTHROPIC_MODEL: str = Field(default="claude-3-5-sonnet-latest")

    # ─── Common LLM behavior (shared across all providers) ──────────────────
    LLM_TIMEOUT_S: float = Field(
        default=30.0,
        description="Per-request timeout in seconds.",
    )
    LLM_MAX_RETRIES: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Total attempts on retryable errors (rate-limit, 5xx, network).",
    )
    LLM_TEMPERATURE: float = Field(
        default=0.1,
        ge=0.0,
        le=2.0,
        description="Low default — extraction tasks want determinism, not creativity.",
    )
    LLM_MAX_TOKENS: int = Field(
        default=1024,
        ge=1,
        description="Soft cap for completion length. Plenty for our extraction outputs.",
    )

    # ─── Storage / observability ────────────────────────────────────────
    LLM_TRACE: bool = Field(
        default=True,
        description=(
            "When true, every LLM call is appended to the chat store as an "
            "LLMCallRecord. Disable only for raw benchmarking; tracing is "
            "free and pays for itself the first time you debug a wrong answer."
        ),
    )
    LOG_DIR: str = Field(
        default="./logs",
        description="Directory holding llm_calls.jsonl and conversations.jsonl.",
    )
    CHAT_STORE_BACKEND: ChatStoreBackend = Field(
        default="jsonl",
        description=(
            "Storage backend for chat / trace history. JSONL today; "
            "SQLite/DuckDB swap-in points reserved for later."
        ),
    )


# Module-level singleton so we don't re-parse env on every call.
_settings_cache: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    Constructed lazily on first call. Tests should call
    :func:`reset_settings_cache_for_tests` between cases to avoid leaking
    state.
    """
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = Settings()
    return _settings_cache


def reset_settings_cache_for_tests() -> None:
    """Drop the cached singleton. Tests only — do not call from production."""
    global _settings_cache
    _settings_cache = None
