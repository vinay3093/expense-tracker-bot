"""Unit tests for the YAML-driven sheet format loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from expense_tracker.ledger.sheets.exceptions import SheetFormatError
from expense_tracker.ledger.sheets.format import (
    SheetFormat,
    get_sheet_format,
)

# ─── from_dict / from_yaml happy path ──────────────────────────────────

def test_default_yaml_loads_via_get_sheet_format(isolated_env):
    isolated_env()  # nothing set → bundled default
    fmt = get_sheet_format()
    assert fmt.primary_currency == "USD"
    assert fmt.secondary_currency == "INR"
    assert fmt.transactions.sheet_name == "Transactions"
    assert "{month_name}" in fmt.monthly.sheet_name_pattern or \
           "{month_short}" in fmt.monthly.sheet_name_pattern or \
           "{month_num}" in fmt.monthly.sheet_name_pattern
    assert "{year}" in fmt.monthly.sheet_name_pattern
    assert "{year}" in fmt.ytd.sheet_name_pattern


def test_from_dict_minimal_uses_defaults():
    fmt = SheetFormat.from_dict({})
    assert fmt.primary_currency == "USD"
    assert fmt.secondary_currency == "INR"
    assert fmt.transactions.sheet_name == "Transactions"
    # Defaults should validate cleanly.
    assert fmt.monthly.sheet_name_pattern == "{month_name} {year}"
    assert fmt.ytd.sheet_name_pattern == "YTD {year}"


def test_currency_codes_uppercased():
    fmt = SheetFormat.from_dict({"primary_currency": "usd", "secondary_currency": "inr"})
    assert fmt.primary_currency == "USD"
    assert fmt.secondary_currency == "INR"


def test_secondary_currency_can_be_null():
    fmt = SheetFormat.from_dict({"secondary_currency": None})
    assert fmt.secondary_currency is None


def test_invalid_secondary_currency_raises():
    with pytest.raises(SheetFormatError):
        SheetFormat.from_dict({"secondary_currency": "EU"})


# ─── Pattern validators ────────────────────────────────────────────────

def test_monthly_pattern_must_include_year_token():
    with pytest.raises(SheetFormatError):
        SheetFormat.from_dict({"monthly": {"sheet_name_pattern": "{month_name}"}})


def test_monthly_pattern_must_include_month_token():
    with pytest.raises(SheetFormatError):
        SheetFormat.from_dict({"monthly": {"sheet_name_pattern": "{year}"}})


def test_ytd_pattern_must_include_year_token():
    with pytest.raises(SheetFormatError):
        SheetFormat.from_dict({"ytd": {"sheet_name_pattern": "Year"}})


def test_monthly_pattern_with_short_token_accepted():
    fmt = SheetFormat.from_dict({"monthly": {"sheet_name_pattern": "{month_short}-{year}"}})
    name = fmt.monthly_sheet_name(
        month_name="April", month_short="Apr", month_num=4, year=2026,
    )
    assert name == "Apr-2026"


def test_monthly_pattern_with_num_token_accepted():
    fmt = SheetFormat.from_dict({"monthly": {"sheet_name_pattern": "{year}-{month_num}"}})
    name = fmt.monthly_sheet_name(
        month_name="April", month_short="Apr", month_num=4, year=2026,
    )
    assert name == "2026-04"


# ─── Sheet name / title formatters ─────────────────────────────────────

def test_monthly_sheet_name_default_pattern():
    fmt = SheetFormat.from_dict({})
    assert fmt.monthly_sheet_name(
        month_name="April", month_short="Apr", month_num=4, year=2026,
    ) == "April 2026"
    assert fmt.monthly_title(
        month_name="April", month_short="Apr", month_num=4, year=2026,
    ) == "April 2026 Expenses"


def test_ytd_sheet_name_and_title():
    fmt = SheetFormat.from_dict({})
    assert fmt.ytd_sheet_name(year=2026) == "YTD 2026"
    assert fmt.ytd_title(year=2026) == "Year to Date — 2026"


# ─── Error wrapping ────────────────────────────────────────────────────

def test_unknown_top_level_key_rejected():
    with pytest.raises(SheetFormatError):
        SheetFormat.from_dict({"this_is_not_a_field": True})


def test_from_yaml_missing_file_raises():
    with pytest.raises(SheetFormatError):
        SheetFormat.from_yaml("/nonexistent/sheet_format.yaml")


def test_from_yaml_invalid_yaml_raises(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text(":\n  - this is not\n    valid: yaml: garbage", encoding="utf-8")
    with pytest.raises(SheetFormatError):
        SheetFormat.from_yaml(p)


def test_from_yaml_non_mapping_top_level_raises(tmp_path: Path):
    p = tmp_path / "list.yaml"
    p.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(SheetFormatError):
        SheetFormat.from_yaml(p)


# ─── Override via env ──────────────────────────────────────────────────

def test_env_override_picks_up_custom_yaml(isolated_env, tmp_path: Path):
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        "schema_version: 2\n"
        "primary_currency: EUR\n"
        "transactions:\n"
        "  sheet_name: Ledger\n",
        encoding="utf-8",
    )
    isolated_env(SHEET_FORMAT_FILE=str(custom))
    fmt = get_sheet_format()
    assert fmt.primary_currency == "EUR"
    assert fmt.transactions.sheet_name == "Ledger"
