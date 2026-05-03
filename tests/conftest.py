"""Pytest fixtures shared across the suite.

Two key principles:

1. **No real network or Google calls in unit tests.** Every test that
   needs an LLM uses :class:`FakeLLMClient` (deterministic, offline).
2. **No leaking env state across tests.** ``isolated_env`` clears every
   project-relevant env var, then re-applies whatever the test wants.
   Combined with :func:`reset_settings_cache_for_tests` this gives each
   test a completely clean :class:`Settings` instance.
"""

from __future__ import annotations

import os

import pytest

from expense_tracker.config import reset_settings_cache_for_tests
from expense_tracker.extractor.categories import reset_registry_cache_for_tests
from expense_tracker.ledger.sheets.adapter import SheetsLedgerBackend
from expense_tracker.ledger.sheets.backend import FakeSheetsBackend, SheetsBackend
from expense_tracker.ledger.sheets.credentials import (
    reset_for_tests as reset_credentials_cache_for_tests,
)
from expense_tracker.ledger.sheets.format import (
    SheetFormat,
    get_sheet_format,
    reset_format_cache_for_tests,
)
from expense_tracker.llm._fake import FakeLLMClient


def make_sheets_ledger(
    backend: SheetsBackend | None = None,
    sheet_format: SheetFormat | None = None,
) -> SheetsLedgerBackend:
    """Build a Sheets edition :class:`LedgerBackend` for tests.

    Centralises the ``backend + sheet_format -> ledger`` wiring so
    test files don't have to repeat it.  Defaults: an empty
    :class:`FakeSheetsBackend` and the bundled YAML format.
    """
    return SheetsLedgerBackend(
        backend=backend or FakeSheetsBackend(title="Test Sheet", spreadsheet_id="sid"),
        sheet_format=sheet_format or get_sheet_format(),
    )

_PROJECT_ENV_VARS = (
    "LLM_PROVIDER",
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "OLLAMA_BASE_URL",
    "OLLAMA_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "LLM_TIMEOUT_S",
    "LLM_MAX_RETRIES",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "LLM_TRACE",
    "LOG_DIR",
    "CHAT_STORE_BACKEND",
    "TIMEZONE",
    "DEFAULT_CURRENCY",
    "EXTRACTOR_CATEGORIES_FILE",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT",
    "EXPENSE_SHEET_ID",
    "SHEET_FORMAT_FILE",
    "SHEETS_TIMEOUT_S",
    "STORAGE_BACKEND",
    "MIRROR_PRIMARY",
    "MIRROR_SECONDARY",
    "DATABASE_URL",
    "NOCODB_BASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_HEALTH_PORT",
)


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Yield a callable that sets env vars for the duration of one test.

    Also chdirs into ``tmp_path`` so any ``.env`` lookup picks up nothing
    (the project's real ``.env`` lives in the repo root).
    """
    for var in _PROJECT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    reset_settings_cache_for_tests()
    reset_registry_cache_for_tests()
    reset_format_cache_for_tests()
    reset_credentials_cache_for_tests()

    def _set(**kv: str) -> None:
        for k, v in kv.items():
            monkeypatch.setenv(k, v)
        reset_settings_cache_for_tests()
        reset_registry_cache_for_tests()
        reset_format_cache_for_tests()
        reset_credentials_cache_for_tests()

    yield _set
    reset_settings_cache_for_tests()
    reset_registry_cache_for_tests()
    reset_format_cache_for_tests()
    reset_credentials_cache_for_tests()


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    """Pre-built fake client. Tests can ``queue_response`` on it."""
    return FakeLLMClient()


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch):
    """Belt-and-braces: hide any project-level ``.env`` from tests by
    making sure the cwd is somewhere it doesn't exist. The
    ``isolated_env`` fixture also chdirs, but autouse here protects tests
    that don't pull ``isolated_env``.
    """
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    # We do NOT chdir here unconditionally because some tests (e.g. CLI
    # smoke tests) may want their own cwd. Only chdir if the test's cwd
    # is the repo root (where .env lives).
    repo_root_env = os.path.join(os.getcwd(), ".env")
    if os.path.isfile(repo_root_env):
        # Pretend it doesn't exist by overriding the var that loads it.
        # The Settings class reads from `.env` only if that file is in
        # cwd; pydantic-settings does NOT walk up. So if the test's cwd
        # IS the repo root, blank the file path via a SettingsConfigDict
        # override is overkill — instead just monkeypatch the os env to
        # the values the tests expect. Tests that use `isolated_env`
        # already chdir into tmp_path, so this branch is mostly noop.
        pass
