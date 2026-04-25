"""Direct tests on the schemas in :mod:`expense_tracker.extractor.schemas`."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from expense_tracker.extractor.schemas import (
    ExpenseEntry,
    ExtractionResult,
    Intent,
    RetrievalQuery,
    TimeRange,
    is_query_intent,
)

# ─── Intent helpers ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "intent",
    [
        Intent.QUERY_PERIOD_TOTAL,
        Intent.QUERY_CATEGORY_TOTAL,
        Intent.QUERY_DAY,
        Intent.QUERY_RECENT,
    ],
)
def test_is_query_intent_true(intent):
    assert is_query_intent(intent) is True


@pytest.mark.parametrize(
    "intent",
    [Intent.LOG_EXPENSE, Intent.SMALLTALK, Intent.UNCLEAR],
)
def test_is_query_intent_false(intent):
    assert is_query_intent(intent) is False


# ─── ExpenseEntry validation ────────────────────────────────────────────

def test_expense_entry_minimal():
    e = ExpenseEntry(date=date(2026, 4, 24), category="Food", amount=40)
    assert e.currency == "INR"
    assert e.vendor is None
    assert e.note is None


def test_expense_entry_negative_amount_rejected():
    with pytest.raises(ValidationError):
        ExpenseEntry(date=date(2026, 4, 24), category="Food", amount=-1)


def test_expense_entry_zero_amount_rejected():
    with pytest.raises(ValidationError):
        ExpenseEntry(date=date(2026, 4, 24), category="Food", amount=0)


def test_expense_entry_uppercases_currency():
    e = ExpenseEntry(date=date(2026, 4, 24), category="Food", amount=40, currency="usd")
    assert e.currency == "USD"


def test_expense_entry_strips_category():
    e = ExpenseEntry(
        date=date(2026, 4, 24), category="  Food  ", amount=40, currency="INR"
    )
    assert e.category == "Food"


# ─── TimeRange validation ───────────────────────────────────────────────

def test_time_range_end_before_start_rejected():
    with pytest.raises(ValidationError):
        TimeRange(start=date(2026, 4, 24), end=date(2026, 4, 1), label="bad")


def test_time_range_end_equals_start_ok():
    tr = TimeRange(start=date(2026, 4, 24), end=date(2026, 4, 24), label="today")
    assert tr.start == tr.end


def test_time_range_label_required():
    with pytest.raises(ValidationError):
        TimeRange(start=date(2026, 4, 1), end=date(2026, 4, 30), label="")


# ─── RetrievalQuery ─────────────────────────────────────────────────────

def test_retrieval_query_limit_bounds():
    tr = TimeRange(start=date(2026, 4, 1), end=date(2026, 4, 30), label="April")
    with pytest.raises(ValidationError):
        RetrievalQuery(intent=Intent.QUERY_RECENT, time_range=tr, limit=0)
    with pytest.raises(ValidationError):
        RetrievalQuery(intent=Intent.QUERY_RECENT, time_range=tr, limit=101)
    rq = RetrievalQuery(intent=Intent.QUERY_RECENT, time_range=tr, limit=10)
    assert rq.limit == 10


# ─── ExtractionResult helpers ───────────────────────────────────────────

def test_extraction_result_is_actionable_for_expense():
    expense = ExpenseEntry(date=date(2026, 4, 24), category="Food", amount=40)
    r = ExtractionResult(
        intent=Intent.LOG_EXPENSE,
        confidence=0.9,
        reasoning="",
        user_text="x",
        expense=expense,
    )
    assert r.is_actionable() is True
    payload = r.to_turn_payload()
    assert payload["type"] == "expense"
    assert payload["amount"] == 40.0


def test_extraction_result_is_actionable_for_query():
    tr = TimeRange(start=date(2026, 4, 1), end=date(2026, 4, 30), label="April")
    q = RetrievalQuery(intent=Intent.QUERY_PERIOD_TOTAL, time_range=tr)
    r = ExtractionResult(
        intent=Intent.QUERY_PERIOD_TOTAL,
        confidence=0.9,
        reasoning="",
        user_text="x",
        query=q,
    )
    assert r.is_actionable() is True
    payload = r.to_turn_payload()
    assert payload["type"] == "query"


def test_extraction_result_unactionable_for_smalltalk():
    r = ExtractionResult(
        intent=Intent.SMALLTALK,
        confidence=0.99,
        reasoning="hi",
        user_text="hi",
    )
    assert r.is_actionable() is False
    assert r.to_turn_payload() == {"type": "smalltalk"}
