"""format_reply unit tests.

The reply formatter is pure: same inputs ⇒ same string. We pin every
branch so changes to user-facing wording stay deliberate.

Branches covered:

* successful USD log
* successful INR log (FX path mentioned)
* log with note + vendor
* log on a newly-created monthly tab
* log_expense intent but no payload (treated as unclear)
* extractor error
* sheets / FX failure (ExpenseLogError supplied)
* smalltalk
* unclear
* retrieval-stub for each query intent
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from expense_tracker.extractor.schemas import (
    ExpenseEntry,
    ExtractionResult,
    Intent,
    RetrievalQuery,
    TimeRange,
)
from expense_tracker.pipeline.exceptions import ExpenseLogError
from expense_tracker.pipeline.logger import LogResult
from expense_tracker.pipeline.reply import format_reply
from expense_tracker.sheets.transactions import TransactionRow


def _row(
    *,
    date_=date(2026, 4, 24),
    category="Food",
    note=None,
    vendor=None,
    amount=10.0,
    currency="USD",
    amount_usd=10.0,
    fx_rate=1.0,
    trace_id="tr_test",
) -> TransactionRow:
    import calendar as _cal
    return TransactionRow(
        date=date_,
        day=date_.strftime("%a"),
        month=_cal.month_name[date_.month],
        year=date_.year,
        category=category,
        note=note,
        vendor=vendor,
        amount=amount,
        currency=currency,
        amount_usd=amount_usd,
        fx_rate=fx_rate,
        source="chat",
        trace_id=trace_id,
        timestamp=datetime(2026, 4, 24, 14, 30),
    )


def _log_result(row, **overrides) -> LogResult:
    defaults = dict(
        transactions_tab="Transactions",
        monthly_tab="April 2026",
        row=row,
        fx_source="identity",
        monthly_tab_created=False,
    )
    defaults.update(overrides)
    return LogResult(**defaults)


def _result(
    intent: Intent,
    *,
    expense: ExpenseEntry | None = None,
    query: RetrievalQuery | None = None,
    error: str | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        intent=intent,
        confidence=0.9,
        reasoning="test",
        user_text="(unused)",
        expense=expense,
        query=query,
        trace_ids=["tr_test"],
        session_id="x_test",
        error=error,
    )


# ─── log_expense replies ────────────────────────────────────────────────


def test_logged_usd_basic():
    row = _row(amount=12.5, amount_usd=12.5, currency="USD")
    result = _result(
        Intent.LOG_EXPENSE,
        expense=ExpenseEntry(
            date=row.date, category="Food", amount=12.5, currency="USD"
        ),
    )
    msg = format_reply(result, log_result=_log_result(row))
    assert "Logged $12.50 to Food" in msg
    assert "Fri 24 Apr 2026" in msg
    assert "Tab: April 2026" in msg


def test_logged_with_note_and_vendor():
    row = _row(note="latte", vendor="Starbucks")
    result = _result(
        Intent.LOG_EXPENSE,
        expense=ExpenseEntry(
            date=row.date, category="Food", amount=10, currency="USD",
            note="latte", vendor="Starbucks",
        ),
    )
    msg = format_reply(result, log_result=_log_result(row))
    assert "note: latte" in msg
    assert "vendor: Starbucks" in msg


def test_logged_inr_shows_fx_path():
    row = _row(
        amount=499, currency="INR", amount_usd=5.99, fx_rate=0.012,
    )
    result = _result(
        Intent.LOG_EXPENSE,
        expense=ExpenseEntry(
            date=row.date, category="Digital", amount=499, currency="INR",
            note="netflix",
        ),
    )
    msg = format_reply(
        result,
        log_result=_log_result(row, fx_source="api"),
    )
    assert "499.00 INR" in msg
    assert "$5.99 USD" in msg
    assert "rate 0.0120" in msg
    assert "api" in msg  # fx_source mentioned


def test_logged_mentions_newly_created_monthly_tab():
    row = _row(date_=date(2026, 5, 1))
    result = _result(
        Intent.LOG_EXPENSE,
        expense=ExpenseEntry(
            date=row.date, category="Food", amount=10, currency="USD",
        ),
    )
    msg = format_reply(
        result,
        log_result=_log_result(row, monthly_tab="May 2026", monthly_tab_created=True),
    )
    assert "Tab: May 2026" in msg
    assert "newly created" in msg


def test_log_expense_without_payload_falls_back_to_unclear():
    # Stage-1 said log_expense but stage-2 produced nothing AND no
    # log_error was provided — treat as unclear.
    result = _result(Intent.LOG_EXPENSE, expense=None)
    msg = format_reply(result)
    assert "didn't catch" in msg.lower() or "unclear" in msg.lower()


def test_log_error_branch_uses_friendly_wording():
    err = ExpenseLogError("FX API down and no cached rate")
    result = _result(
        Intent.LOG_EXPENSE,
        expense=ExpenseEntry(
            date=date(2026, 4, 24), category="Food", amount=499, currency="INR",
        ),
    )
    msg = format_reply(result, log_error=err)
    assert "couldn't write" in msg
    assert "FX API down" in msg
    assert "499" in msg
    assert "INR" in msg


# ─── Non-log replies ────────────────────────────────────────────────────


def test_smalltalk_reply():
    result = _result(Intent.SMALLTALK)
    msg = format_reply(result)
    assert msg
    assert "spent" in msg.lower() or "log" in msg.lower()


def test_unclear_reply_includes_examples():
    result = _result(Intent.UNCLEAR)
    msg = format_reply(result)
    assert "spent 40" in msg
    assert "april" in msg.lower()


def test_extractor_error_branch():
    result = _result(Intent.LOG_EXPENSE, error="bad json from llm")
    msg = format_reply(result)
    assert "couldn't parse" in msg
    assert "bad json from llm" in msg


@pytest.mark.parametrize(
    "intent",
    [
        Intent.QUERY_PERIOD_TOTAL,
        Intent.QUERY_CATEGORY_TOTAL,
        Intent.QUERY_DAY,
        Intent.QUERY_RECENT,
    ],
)
def test_retrieval_stub_mentions_step_6(intent):
    query = RetrievalQuery(
        intent=intent,
        time_range=TimeRange(
            start=date(2026, 4, 1),
            end=date(2026, 4, 30),
            label="April 2026",
        ),
        category="Food" if intent != Intent.QUERY_RECENT else None,
    )
    result = _result(intent, query=query)
    msg = format_reply(result)
    assert "Step 6" in msg
    assert "April 2026" in msg
    if intent == Intent.QUERY_CATEGORY_TOTAL:
        assert "Food" in msg
