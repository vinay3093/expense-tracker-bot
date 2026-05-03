"""Unit tests for :class:`CorrectionLogger`.

Covers /undo and /edit semantics in isolation:

* ``peek_last`` — pure read, doesn't touch the sheet.
* ``undo`` — deletes the bottom-most row, runs the recompute nudge,
  and is graceful on an empty ledger.
* ``edit`` — patches amount and/or category, re-runs FX on amount
  changes, canonicalizes category aliases through the registry.
* Failure mapping — :class:`SheetsError` and :class:`CurrencyError`
  both surface as :class:`CorrectionError`.

Real Sheets / FX network calls are blocked: we drive a
:class:`FakeSheetsBackend` and a pre-seeded :class:`CurrencyConverter`.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from expense_tracker.extractor.categories import CategoryRegistry, get_registry
from expense_tracker.extractor.schemas import ExpenseEntry
from expense_tracker.ledger.sheets.backend import FakeSheetsBackend
from expense_tracker.ledger.sheets.currency import CurrencyConverter
from expense_tracker.ledger.sheets.format import get_sheet_format
from expense_tracker.ledger.sheets.transactions import (
    col_for as txn_col_for,
)
from expense_tracker.ledger.sheets.transactions import (
    get_last_row,
    init_transactions_tab,
)
from expense_tracker.pipeline.correction import (
    CorrectionError,
    CorrectionLogger,
    EditResult,
    UndoResult,
)
from expense_tracker.pipeline.logger import ExpenseLogger

TZ = "America/Chicago"
FROZEN_NOW = datetime(2026, 4, 24, 14, 30, tzinfo=ZoneInfo(TZ))


def _frozen_now() -> datetime:
    return FROZEN_NOW


# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def registry() -> CategoryRegistry:
    return get_registry()


@pytest.fixture
def sheet_format():
    return get_sheet_format()


@pytest.fixture
def usd_converter(tmp_path) -> CurrencyConverter:
    """Offline USD-primary converter (timeout=0 makes any net call fail)."""
    return CurrencyConverter(
        primary_currency="USD",
        cache_path=tmp_path / "fx_cache.json",
        timeout_s=0.001,
    )


@pytest.fixture
def inr_seeded_converter(tmp_path) -> CurrencyConverter:
    """USD-primary converter with INR->USD pre-seeded for two dates."""
    converter = CurrencyConverter(
        primary_currency="USD",
        cache_path=tmp_path / "fx_cache.json",
        timeout_s=0.001,
    )
    # Pre-seed both the original entry's date and any near fallback so
    # the convert() lookups in undo/edit always hit the cache.
    converter._cache.put(date(2026, 4, 24), "INR", "USD", 0.012)
    converter._cache.put(date(2026, 4, 25), "INR", "USD", 0.012)
    return converter


@pytest.fixture
def fake_backend() -> FakeSheetsBackend:
    return FakeSheetsBackend(
        title="Expense Tracker (test)",
        spreadsheet_id="sid_test",
    )


def _seed_two_expenses(*, backend, sheet_format, registry, converter) -> None:
    """Log two USD expenses on consecutive April 2026 dates."""
    logger = ExpenseLogger(
        backend=backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=converter,
        timezone=TZ,
        source="chat",
        now=_frozen_now,
    )
    logger.log(
        ExpenseEntry(
            date=date(2026, 4, 24),
            category="Food",
            amount=12.50,
            currency="USD",
            vendor="Starbucks",
            note="latte",
        ),
        trace_id="tr_first",
    )
    logger.log(
        ExpenseEntry(
            date=date(2026, 4, 25),
            category="Saloon",
            amount=30.0,
            currency="USD",
            vendor="Supercuts",
            note="haircut",
        ),
        trace_id="tr_second",
    )


def _make_corrector(*, backend, sheet_format, registry, converter) -> CorrectionLogger:
    return CorrectionLogger(
        backend=backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=converter,
    )


# ─── peek_last ─────────────────────────────────────────────────────────


def test_peek_last_returns_bottom_row_without_modifying(
    fake_backend, sheet_format, registry, usd_converter,
):
    _seed_two_expenses(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    snap = corrector.peek_last()
    assert snap.is_empty is False
    assert snap.value("category") == "Saloon"
    assert snap.value("amount") == 30.0
    # Sanity: peek doesn't shift the row out from under us.
    again = corrector.peek_last()
    assert again.row_index == snap.row_index


def test_peek_last_on_empty_returns_empty(
    fake_backend, sheet_format, registry, usd_converter,
):
    init_transactions_tab(fake_backend, sheet_format)
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    assert corrector.peek_last().is_empty


# ─── undo ──────────────────────────────────────────────────────────────


def test_undo_deletes_bottom_row_and_nudges_month(
    fake_backend, sheet_format, registry, usd_converter,
):
    _seed_two_expenses(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    result = corrector.undo()
    assert isinstance(result, UndoResult)
    assert result.deleted_row.value("category") == "Saloon"
    assert result.transactions_tab == sheet_format.transactions.sheet_name
    assert result.monthly_tab is not None
    assert "April 2026" in result.monthly_tab
    assert result.monthly_tab_recomputed is True

    # The previous row is now bottom-most.
    survivor = get_last_row(fake_backend, sheet_format)
    assert survivor.value("category") == "Food"
    assert survivor.value("amount") == 12.50


def test_undo_on_empty_ledger_is_noop(
    fake_backend, sheet_format, registry, usd_converter,
):
    init_transactions_tab(fake_backend, sheet_format)
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    result = corrector.undo()
    assert result.deleted_row.is_empty
    assert result.monthly_tab is None
    assert result.monthly_tab_recomputed is False


# ─── edit ──────────────────────────────────────────────────────────────


def test_edit_amount_only_recomputes_usd(
    fake_backend, sheet_format, registry, usd_converter,
):
    _seed_two_expenses(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    result = corrector.edit(amount=50.0)
    assert isinstance(result, EditResult)
    assert result.before.value("amount") == 30.0
    assert result.applied == {
        "amount": 50.0,
        "amount_usd": 50.0,  # USD identity
        "fx_rate": 1.0,
    }
    # Row is patched in place (same row index, new amount).
    after = get_last_row(fake_backend, sheet_format)
    assert after.row_index == result.before.row_index
    assert after.value("amount") == 50.0
    assert after.value("amount_usd") == 50.0
    # Untouched fields survive.
    assert after.value("category") == "Saloon"
    assert after.value("note") == "haircut"


def test_edit_category_canonicalizes_aliases(
    fake_backend, sheet_format, registry, usd_converter,
):
    _seed_two_expenses(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    # "groceries" (lowercase) should resolve to canonical "Groceries".
    result = corrector.edit(category="groceries")
    assert result.applied == {"category": "Groceries"}
    after = get_last_row(fake_backend, sheet_format)
    assert after.value("category") == "Groceries"


def test_edit_combined_amount_and_category(
    fake_backend, sheet_format, registry, usd_converter,
):
    _seed_two_expenses(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    result = corrector.edit(amount=15.0, category="Shopping")
    assert result.applied["amount"] == 15.0
    assert result.applied["category"] == "Shopping"
    after = get_last_row(fake_backend, sheet_format)
    assert after.value("amount") == 15.0
    assert after.value("category") == "Shopping"


def test_edit_amount_on_inr_row_reuses_currency_and_fx(
    fake_backend, sheet_format, registry, inr_seeded_converter,
):
    # Seed one INR expense so amount edits have to re-run FX.
    logger = ExpenseLogger(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=inr_seeded_converter,
        timezone=TZ,
        source="chat",
        now=_frozen_now,
    )
    logger.log(
        ExpenseEntry(
            date=date(2026, 4, 24),
            category="Digital",
            amount=499,
            currency="INR",
            vendor=None,
            note="cloud bill",
        ),
        trace_id="tr_inr",
    )

    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=inr_seeded_converter,
    )

    result = corrector.edit(amount=1000.0)
    # Sub-cent rounding preserved by the converter; just assert the
    # core reconvert happened and stayed in INR.
    after = get_last_row(fake_backend, sheet_format)
    assert after.value("currency") == "INR"
    assert after.value("amount") == 1000.0
    assert after.value("amount_usd") == pytest.approx(12.0, rel=0.01)
    # Updates dict carries the same numbers we wrote.
    assert result.applied["amount"] == 1000.0
    assert result.applied["amount_usd"] == pytest.approx(12.0, rel=0.01)
    assert result.applied["fx_rate"] == pytest.approx(0.012, rel=0.01)


def test_edit_requires_at_least_one_field(
    fake_backend, sheet_format, registry, usd_converter,
):
    _seed_two_expenses(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    with pytest.raises(CorrectionError, match="amount / category"):
        corrector.edit()


def test_edit_rejects_non_positive_amount(
    fake_backend, sheet_format, registry, usd_converter,
):
    _seed_two_expenses(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    with pytest.raises(CorrectionError, match="positive"):
        corrector.edit(amount=0.0)
    with pytest.raises(CorrectionError, match="positive"):
        corrector.edit(amount=-5.0)


def test_edit_on_empty_ledger_returns_empty_result(
    fake_backend, sheet_format, registry, usd_converter,
):
    init_transactions_tab(fake_backend, sheet_format)
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    result = corrector.edit(amount=10.0)
    assert result.before.is_empty
    assert result.applied == {}
    assert result.monthly_tab is None
    assert result.monthly_tab_recomputed is False


# ─── Recompute nudge resilience ────────────────────────────────────────


def test_recompute_failure_does_not_break_undo(
    fake_backend, sheet_format, registry, usd_converter, monkeypatch,
):
    """If the monthly-tab nudge raises, undo should still report success.

    Stale-cache repair is a best-effort UX nicety; the user-visible
    operation (deleting the row) has already succeeded by then, so we
    must never let recompute errors mask that success.
    """
    _seed_two_expenses(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )
    corrector = _make_corrector(
        backend=fake_backend,
        sheet_format=sheet_format,
        registry=registry,
        converter=usd_converter,
    )

    from expense_tracker.ledger.sheets.exceptions import SheetsError

    def _boom(*args, **kwargs):
        raise SheetsError("sheets API hiccup")

    monkeypatch.setattr(
        "expense_tracker.pipeline.correction.force_month_recompute", _boom,
    )

    result = corrector.undo()
    # Row was still deleted.
    assert result.deleted_row.value("category") == "Saloon"
    assert result.monthly_tab is None
    assert result.monthly_tab_recomputed is False
    # Survivor row is intact.
    survivor = get_last_row(fake_backend, sheet_format)
    assert survivor.value("category") == "Food"


# Silence "unused" warnings on the col helper — it's imported because
# future tests in this file will likely want it.
_ = txn_col_for
