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
StorageBackend = Literal["sheets", "nocodb", "postgres"]


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

    # ─── Locale (drives date parsing & currency defaults) ───────────────
    TIMEZONE: str = Field(
        default="UTC",
        description=(
            "IANA timezone name (e.g. 'America/Chicago', 'Asia/Kolkata'). "
            "Used to resolve relative phrases like 'today' / 'yesterday' / "
            "'last week' from the user's perspective, not the server's. "
            "Defaults to UTC for deterministic tests; override in .env."
        ),
    )
    DEFAULT_CURRENCY: str = Field(
        default="INR",
        description=(
            "ISO-4217 currency assumed when the user doesn't specify one. "
            "Override per-message wins (e.g. '$40' or '40 USD')."
        ),
    )
    EXTRACTOR_CATEGORIES_FILE: str | None = Field(
        default=None,
        description=(
            "Optional path to a YAML file listing your expense categories + "
            "aliases. When unset, the bundled default lives in "
            "expense_tracker/extractor/data/categories.yaml. "
            "Step 4 will plug your real Google-Sheet column headers in here."
        ),
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

    # ─── Google Sheets (Step 4) ─────────────────────────────────────────
    GOOGLE_SERVICE_ACCOUNT_JSON: str | None = Field(
        default=None,
        description=(
            "Path to the service-account JSON downloaded from Google Cloud "
            "Console. Required for any Sheets operation. The file MUST stay "
            "outside git (see .gitignore — secrets/ is ignored)."
        ),
    )
    GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT: SecretStr | None = Field(
        default=None,
        description=(
            "Alternative to GOOGLE_SERVICE_ACCOUNT_JSON for hosted "
            "deployments where you can't ship a file (Hugging Face Spaces, "
            "Render, Koyeb, ...).  Paste the FULL contents of the "
            "service-account JSON here as a single env var.  On startup, "
            "the bot writes it to a temp file (chmod 600) and uses that "
            "path.  Wins over GOOGLE_SERVICE_ACCOUNT_JSON when both are "
            "set."
        ),
    )
    EXPENSE_SHEET_ID: str | None = Field(
        default=None,
        description=(
            "The Google Sheets ID — the long token in the URL between "
            "/spreadsheets/d/ and /edit. Required for any read/write."
        ),
    )
    SHEET_FORMAT_FILE: str | None = Field(
        default=None,
        description=(
            "Optional path to a YAML file that overrides the bundled sheet "
            "format (expense_tracker/sheets/data/sheet_format.yaml). Generate "
            "one with `expense --introspect-sheet \"<existing tab>\"` if you "
            "want to mirror an existing month's layout exactly."
        ),
    )
    SHEETS_TIMEOUT_S: float = Field(
        default=30.0,
        ge=1.0,
        description="Per-request timeout for Google Sheets API calls.",
    )

    # ─── Storage backend selection (Step 10b: Sheets vs Postgres) ──────
    STORAGE_BACKEND: StorageBackend = Field(
        default="sheets",
        description=(
            "Which ledger backend the bot writes / reads from:\n"
            "* 'sheets' — Google Sheets edition (Step 4-9).  Default.\n"
            "* 'nocodb' / 'postgres' — Postgres + NocoDB edition (Step 10b).\n"
            "Setting this to 'nocodb' requires DATABASE_URL pointing at a\n"
            "reachable Postgres (Supabase, local docker, RDS, ...).  Both\n"
            "editions live in the same codebase; flipping this var is the\n"
            "only change needed at runtime."
        ),
    )
    DATABASE_URL: SecretStr | None = Field(
        default=None,
        description=(
            "SQLAlchemy connection URL for the Postgres edition.  "
            "Examples:\n"
            "  postgresql+psycopg://user:pass@db.example.supabase.co:5432/postgres\n"
            "  postgresql+psycopg://expenses:expenses@localhost:5432/expenses\n"
            "Required only when STORAGE_BACKEND=nocodb."
        ),
    )
    NOCODB_BASE_URL: str | None = Field(
        default=None,
        description=(
            "Base URL of the NocoDB UI (for printing 'Open NocoDB' links "
            "from the CLI / Telegram replies).  Optional — purely a UX "
            "convenience.  Example: http://localhost:8080 ."
        ),
    )

    # ─── Telegram bot front-end (Step 7) ────────────────────────────────
    TELEGRAM_BOT_TOKEN: SecretStr | None = Field(
        default=None,
        description=(
            "Token from @BotFather. Required only when running "
            "`expense --telegram`. Keep it secret; treat it like a "
            "password (anyone with it can pretend to be your bot)."
        ),
    )
    TELEGRAM_ALLOWED_USERS: str | None = Field(
        default=None,
        description=(
            "Comma-separated list of Telegram user IDs allowed to talk to "
            "the bot — for a personal bot this is just your own ID. Anyone "
            "not on the list gets a 'not authorized' reply that includes "
            "their ID, so you can copy it into .env. Leave unset to refuse "
            "every message (safe default — explicit allow-list, no implicit "
            "open access)."
        ),
    )
    TELEGRAM_HEALTH_PORT: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description=(
            "When set, run a tiny HTTP health endpoint alongside the "
            "Telegram long-poll loop.  Required by hosts that demand a "
            "listening port (Hugging Face Spaces defaults to 7860, "
            "Render to 10000).  Endpoint serves GET / (200 'alive') so "
            "platform health checks + UptimeRobot / GitHub Actions cron "
            "keep-alive pings work.  Leave unset for laptop / Oracle VM "
            "deploys where no port is needed."
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
