"""ExpenseLogger unit tests.

Covers the chat → row writer in isolation:

* USD identity flow (no API call, no cache).
* Foreign-currency flow with a pre-seeded FX cache (still no network).
* Auto-vivification of the Transactions tab and the monthly summary tab.
* Trace-id propagation onto the appended row.
* Day-name + month-key derivation.
* SheetsError + CurrencyError both surface as ExpenseLogError.
* Multiple appends on the same day land as separate rows (no dedup).

Real network calls are blocked because :class:`FakeSheetsBackend` and a
manually-seeded ``_RateCache`` cover every code path the production
backend exercises.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from expense_tracker.extractor.categories import CategoryRegistry, get_registry
from expense_tracker.extractor.schemas import ExpenseEntry
from expense_tracker.ledger.sheets.backend import FakeSheetsBackend
from expense_tracker.ledger.sheets.currency import CurrencyConverter, CurrencyError
from expense_tracker.ledger.sheets.format import get_sheet_format
from expense_tracker.ledger.sheets.transactions import col_for as txn_col_for
from expense_tracker.pipeline.exceptions import ExpenseLogError
from expense_tracker.pipeline.logger import ExpenseLogger, LogResult

TZ = "America/Chicago"
FROZEN_NOW = datetime(2026, 4, 24, 14, 30, tzinfo=ZoneInfo(TZ))


def _frozen_now() -> datetime:
    return FROZEN_NOW


@pytest.fixture
def registry() -> CategoryRegistry:
    return get_registry()


@pytest.fixture
def sheet_format():
    return get_sheet_format()


@pytest.fixture
def usd_converter(tmp_path) -> CurrencyConverter:
    """USD-primary converter rooted in a tmp cache (offline)."""
    return CurrencyConverter(
        primary_currency="USD",
        cache_path=tmp_path / "fx_cache.json",
        timeout_s=0.001,  # makes accidental network calls fail loudly
    )


@pytest.fixture
def inr_seeded_converter(tmp_path) -> CurrencyConverter:
    """USD-primary converter with INR->USD pre-seeded in the cache."""
    converter = CurrencyConverter(
        primary_currency="USD",
        cache_path=tmp_path / "fx_cache.json",
        timeout_s=0.001,
    )
    # Seed both the entry's date and a nearby fallback date so any branch
    # of convert() can find a rate without touching the network.
    converter._cache.put(date(2026, 4, 24), "INR", "USD", 0.012)
    return converter


@pytest.fixture
def fake_backend() -> FakeSheetsBackend:
    return FakeSheetsBackend(
        title="Expense Tracker (test)",
        spreadsheet_id="sid_test",
    )


def _make_logger(
    *,
    backend,
    sheet_format,
    registry,
    converter,
    source: str = "chat",
) -> ExpenseLogger:
    return ExpenseLogger(
        backend=backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=converter,
        timezone=TZ,
        source=source,
        now=_frozen_now,
    )


# ─── Happy-path flows ───────────────────────────────────────────────────


def test_log_usd_entry_writes_one_row(
    fake_backend, sheet_format, registry, usd_converter
):
    logger = _make_logger(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    entry = ExpenseEntry(
        date=date(2026, 4, 24),
        category="Food",
        amount=12.50,
        currency="USD",
        vendor="Starbucks",
        note="latte",
    )

    result = logger.log(entry, trace_id="tr_abc12345")

    assert isinstance(result, LogResult)
    assert result.fx_source == "identity"
    assert result.row.amount == 12.50
    assert result.row.amount_usd == 12.50
    assert result.row.fx_rate == 1.0
    assert result.row.currency == "USD"
    assert result.row.day == "Fri"
    assert result.row.month == "April"
    assert result.row.year == 2026
    assert result.row.category == "Food"
    assert result.row.note == "latte"
    assert result.row.vendor == "Starbucks"
    assert result.row.source == "chat"
    assert result.row.trace_id == "tr_abc12345"

    # Row landed in Transactions. Header A1 is now "Date" (date is the
    # leftmost column in the new schema).
    txns = fake_backend.get_worksheet(sheet_format.transactions.sheet_name)
    assert txns.cell("A1") == "Date"
    assert txns.cell(f"{txn_col_for('date')}2") == "2026-04-24"
    assert txns.cell(f"{txn_col_for('month')}2") == "April"
    assert txns.cell(f"{txn_col_for('year')}2") == 2026
    assert txns.cell(f"{txn_col_for('amount')}2") == 12.50
    assert txns.cell(f"{txn_col_for('amount_usd')}2") == 12.50
    assert txns.cell(f"{txn_col_for('trace_id')}2") == "tr_abc12345"
    assert txns.cell(f"{txn_col_for('source')}2") == "chat"
    assert txns.cell(f"{txn_col_for('vendor')}2") == "Starbucks"
    assert txns.cell(f"{txn_col_for('note')}2") == "latte"
    # Timestamp lives at the rightmost column.
    assert txns.cell(f"{txn_col_for('timestamp')}2").startswith("2026-04-24T14:30")

    # Monthly tab was auto-vivified.
    assert result.monthly_tab_created is True
    assert fake_backend.has_worksheet(result.monthly_tab)
    assert "April 2026" in result.monthly_tab


def test_log_inr_entry_uses_cached_fx(
    fake_backend, sheet_format, registry, inr_seeded_converter
):
    logger = _make_logger(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=inr_seeded_converter,
    )

    entry = ExpenseEntry(
        date=date(2026, 4, 24),
        category="Digital",
        amount=499,
        currency="INR",
        vendor=None,
        note="netflix",
    )

    result = logger.log(entry, trace_id="tr_inr1")

    assert result.fx_source == "cache"
    assert result.row.amount == 499.0
    assert result.row.currency == "INR"
    assert result.row.fx_rate == pytest.approx(0.012)
    assert result.row.amount_usd == pytest.approx(499 * 0.012)


def test_second_log_in_same_month_does_not_recreate_tab(
    fake_backend, sheet_format, registry, usd_converter
):
    logger = _make_logger(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    e1 = ExpenseEntry(
        date=date(2026, 4, 24), category="Food", amount=10, currency="USD"
    )
    e2 = ExpenseEntry(
        date=date(2026, 4, 25), category="Food", amount=20, currency="USD"
    )

    r1 = logger.log(e1)
    r2 = logger.log(e2)

    assert r1.monthly_tab_created is True
    assert r2.monthly_tab_created is False
    assert r1.monthly_tab == r2.monthly_tab

    txns = fake_backend.get_worksheet(sheet_format.transactions.sheet_name)
    date_col = txn_col_for("date")
    assert txns.cell(f"{date_col}2") == "2026-04-24"
    assert txns.cell(f"{date_col}3") == "2026-04-25"
    # No third data row.
    assert txns.cell(f"{date_col}4") == ""


def test_log_in_different_month_creates_second_monthly_tab(
    fake_backend, sheet_format, registry, usd_converter
):
    logger = _make_logger(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    e_april = ExpenseEntry(
        date=date(2026, 4, 24), category="Food", amount=10, currency="USD"
    )
    e_may = ExpenseEntry(
        date=date(2026, 5, 1), category="Food", amount=20, currency="USD"
    )

    r_april = logger.log(e_april)
    r_may = logger.log(e_may)

    assert r_april.monthly_tab_created is True
    assert r_may.monthly_tab_created is True
    assert r_april.monthly_tab != r_may.monthly_tab
    assert "April 2026" in r_april.monthly_tab
    assert "May 2026" in r_may.monthly_tab


def test_log_resolves_alias_to_canonical(
    fake_backend, sheet_format, registry, usd_converter
):
    logger = _make_logger(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    # "coffee" is an alias of Food in the YAML.
    entry = ExpenseEntry(
        date=date(2026, 4, 24), category="coffee", amount=5, currency="USD"
    )
    result = logger.log(entry)

    assert result.row.category == "Food"


def test_log_unknown_category_falls_back(
    fake_backend, sheet_format, registry, usd_converter
):
    logger = _make_logger(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    entry = ExpenseEntry(
        date=date(2026, 4, 24),
        category="completely-unknown-thing",
        amount=5,
        currency="USD",
    )
    result = logger.log(entry)

    # Falls back to Miscellaneous (the user's chosen fallback).
    assert result.row.category == registry.fallback_category


def test_log_to_action_dict_shape(
    fake_backend, sheet_format, registry, usd_converter
):
    logger = _make_logger(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    entry = ExpenseEntry(
        date=date(2026, 4, 24), category="Food", amount=10, currency="USD"
    )
    result = logger.log(entry, trace_id="tr_xyz")

    action = result.to_action_dict()
    assert action["type"] == "sheets_append"
    assert action["status"] == "ok"
    assert action["category"] == "Food"
    assert action["amount"] == 10.0
    assert action["amount_usd"] == 10.0
    assert action["fx_rate"] == 1.0
    assert action["fx_source"] == "identity"
    assert action["trace_id"] == "tr_xyz"
    assert action["date"] == "2026-04-24"
    assert action["transactions_tab"] == sheet_format.transactions.sheet_name
    assert "April 2026" in action["monthly_tab"]


def test_custom_source_string(
    fake_backend, sheet_format, registry, usd_converter
):
    logger = _make_logger(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
        source="cli",
    )

    entry = ExpenseEntry(
        date=date(2026, 4, 24), category="Food", amount=10, currency="USD"
    )
    result = logger.log(entry)
    assert result.row.source == "cli"


# ─── Failure flows ──────────────────────────────────────────────────────


class _ExplodingConverter:
    """CurrencyConverter stand-in that always fails with CurrencyError."""

    primary_currency = "USD"

    def convert(self, amount, from_currency, *, to_currency=None, on_date=None):
        raise CurrencyError(f"boom: {from_currency}->USD")


def test_currency_error_wrapped_in_log_error(
    fake_backend, sheet_format, registry
):
    logger = _make_logger(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=_ExplodingConverter(),
    )

    entry = ExpenseEntry(
        date=date(2026, 4, 24), category="Food", amount=10, currency="INR"
    )

    with pytest.raises(ExpenseLogError) as exc_info:
        logger.log(entry)

    assert isinstance(exc_info.value.cause, CurrencyError)
    assert "INR" in str(exc_info.value)
    # Nothing was written.
    assert not fake_backend.has_worksheet(sheet_format.transactions.sheet_name)
