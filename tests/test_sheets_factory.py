"""Unit tests for the Sheets backend factory."""

from __future__ import annotations

import pytest

from expense_tracker.config import get_settings
from expense_tracker.ledger.sheets import (
    FakeSheetsBackend,
    SheetsConfigError,
    get_sheets_backend,
)


def test_fake_returns_fake_backend(isolated_env):
    isolated_env()
    b = get_sheets_backend(fake=True)
    assert isinstance(b, FakeSheetsBackend)
    # Listing on a fresh fake should yield no tabs.
    assert b.list_worksheets() == []


def test_real_backend_requires_service_account(isolated_env):
    isolated_env(EXPENSE_SHEET_ID="abc123")  # no GOOGLE_SERVICE_ACCOUNT_JSON
    with pytest.raises(SheetsConfigError):
        get_sheets_backend(get_settings())


def test_real_backend_requires_sheet_id(isolated_env, tmp_path):
    sa = tmp_path / "sa.json"
    sa.write_text("{}", encoding="utf-8")
    isolated_env(GOOGLE_SERVICE_ACCOUNT_JSON=str(sa))
    with pytest.raises(SheetsConfigError):
        get_sheets_backend(get_settings())


def test_settings_default_to_empty(isolated_env):
    isolated_env()
    cfg = get_settings()
    assert cfg.GOOGLE_SERVICE_ACCOUNT_JSON in (None, "")
    assert cfg.EXPENSE_SHEET_ID in (None, "")
