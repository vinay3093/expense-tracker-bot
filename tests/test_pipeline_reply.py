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
* retrieval — period total, category total, day detail, recent
* retrieval — empty window says "no expenses found"
* retrieval — query parsed but no engine wired (older tests path)
* retrieval — read failure surfaces gracefully
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
from expense_tracker.ledger.sheets.transactions import TransactionRow
from expense_tracker.pipeline.exceptions import ExpenseLogError
from expense_tracker.pipeline.logger import LogResult
from expense_tracker.pipeline.reply import format_reply
from expense_tracker.pipeline.retrieval import (
    LedgerRow,
    RetrievalAnswer,
    RetrievalError,
)


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


# ─── Retrieval replies ─────────────────────────────────────────────────


def _ledger_row(
    *,
    date_=date(2026, 4, 24),
    category="Food",
    amount_usd=12.5,
    amount=12.5,
    currency="USD",
    note=None,
    vendor=None,
    row_index=2,
) -> LedgerRow:
    import calendar as _cal
    return LedgerRow(
        row_index=row_index,
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
        fx_rate=1.0,
        source="chat",
        trace_id=None,
        timestamp=None,
    )


def _april_query(
    intent: Intent, *, category: str | None = None, limit: int | None = None,
) -> RetrievalQuery:
    return RetrievalQuery(
        intent=intent,
        time_range=TimeRange(
            start=date(2026, 4, 1),
            end=date(2026, 4, 30),
            label="April 2026",
        ),
        category=category,
        limit=limit,
    )


def test_retrieval_period_total_lists_top_categories():
    query = _april_query(Intent.QUERY_PERIOD_TOTAL)
    rows = [
        _ledger_row(category="Groceries", amount_usd=450.0, row_index=2),
        _ledger_row(category="Food",      amount_usd=300.0, row_index=3),
        _ledger_row(category="Tesla Car", amount_usd=240.0, row_index=4,
                    vendor="Tesla", note="charging"),
    ]
    answer = RetrievalAnswer(
        intent=Intent.QUERY_PERIOD_TOTAL,
        query=query,
        matched_rows=rows,
        total_usd=990.0,
        transaction_count=3,
        by_category={"Groceries": 450.0, "Food": 300.0, "Tesla Car": 240.0},
        by_day={date(2026, 4, 24): 990.0},
        largest=rows[0],
    )
    result = _result(Intent.QUERY_PERIOD_TOTAL, query=query)
    msg = format_reply(result, retrieval_answer=answer)

    assert "April 2026" in msg
    assert "$990.00" in msg
    assert "3 transactions" in msg
    # Top categories are mentioned in spend order.
    assert msg.find("Groceries") < msg.find("Food") < msg.find("Tesla Car")
    assert "$450.00" in msg


def test_retrieval_category_total_mentions_largest():
    query = _april_query(Intent.QUERY_CATEGORY_TOTAL, category="Food")
    biggest = _ledger_row(
        category="Food", amount_usd=45.0, vendor="Chipotle",
        date_=date(2026, 4, 18), note="lunch",
    )
    answer = RetrievalAnswer(
        intent=Intent.QUERY_CATEGORY_TOTAL,
        query=query,
        matched_rows=[biggest, _ledger_row(category="Food", amount_usd=15.0)],
        total_usd=60.0,
        transaction_count=2,
        by_category={"Food": 60.0},
        by_day={date(2026, 4, 18): 45.0, date(2026, 4, 24): 15.0},
        largest=biggest,
    )
    result = _result(Intent.QUERY_CATEGORY_TOTAL, query=query)
    msg = format_reply(result, retrieval_answer=answer)

    assert "April 2026 / Food" in msg
    assert "$60.00" in msg
    assert "2 transactions" in msg
    assert "Largest: $45.00" in msg
    assert "Sat 18 Apr" in msg
    assert "Chipotle" in msg
    assert "lunch" in msg


def test_retrieval_day_detail_lists_each_row():
    query = RetrievalQuery(
        intent=Intent.QUERY_DAY,
        time_range=TimeRange(
            start=date(2026, 4, 25),
            end=date(2026, 4, 25),
            label="Sat 25 Apr 2026",
        ),
    )
    rows = [
        _ledger_row(category="Food", amount_usd=40.0, vendor="Starbucks",
                    note="coffee", row_index=2),
        _ledger_row(category="Groceries", amount_usd=35.0, vendor="Costco",
                    row_index=3),
        _ledger_row(category="Saloon", amount_usd=12.5, note="haircut",
                    row_index=4),
    ]
    answer = RetrievalAnswer(
        intent=Intent.QUERY_DAY,
        query=query,
        matched_rows=rows,
        total_usd=87.5,
        transaction_count=3,
        by_category={"Food": 40.0, "Groceries": 35.0, "Saloon": 12.5},
        by_day={date(2026, 4, 25): 87.5},
        largest=rows[0],
    )
    result = _result(Intent.QUERY_DAY, query=query)
    msg = format_reply(result, retrieval_answer=answer)

    assert "Sat 25 Apr 2026" in msg
    assert "3 transactions" in msg
    assert "$87.50" in msg
    # Largest first in the breakdown
    assert "Food $40.00" in msg
    assert "Starbucks" in msg
    assert "Saloon $12.50" in msg


def test_retrieval_recent_says_last_n_of_window():
    query = _april_query(Intent.QUERY_RECENT, limit=3)
    rows = [
        _ledger_row(category="Food", amount_usd=12.5, vendor="Cafe",
                    date_=date(2026, 4, 26), row_index=20),
        _ledger_row(category="Groceries", amount_usd=88.0,
                    date_=date(2026, 4, 25), row_index=19),
        _ledger_row(category="Digital", amount_usd=5.0,
                    date_=date(2026, 4, 24), row_index=18),
    ]
    answer = RetrievalAnswer(
        intent=Intent.QUERY_RECENT,
        query=query,
        matched_rows=rows,
        total_usd=320.0,
        transaction_count=18,
        by_category={"Food": 100.0, "Groceries": 200.0, "Digital": 20.0},
        by_day={r.date: r.amount_usd for r in rows},
        largest=rows[1],
    )
    result = _result(Intent.QUERY_RECENT, query=query)
    msg = format_reply(result, retrieval_answer=answer)

    assert "Last 3" in msg
    assert "of 18 in April 2026" in msg
    assert "Food $12.50" in msg
    assert "Cafe" in msg


def test_retrieval_empty_window_yields_no_matches_reply():
    query = _april_query(Intent.QUERY_PERIOD_TOTAL)
    answer = RetrievalAnswer(
        intent=Intent.QUERY_PERIOD_TOTAL,
        query=query,
        matched_rows=[],
        total_usd=0.0,
        transaction_count=0,
        by_category={},
        by_day={},
        largest=None,
    )
    result = _result(Intent.QUERY_PERIOD_TOTAL, query=query)
    msg = format_reply(result, retrieval_answer=answer)

    assert "No expenses found" in msg
    assert "April 2026" in msg
    assert "$0.00" not in msg, "shouldn't render a misleading zero total"


def test_retrieval_query_parsed_but_no_engine_falls_back_to_explanation():
    # Reachable when ChatPipeline is constructed without a
    # RetrievalEngine (older tests). Production wires one in.
    query = _april_query(Intent.QUERY_CATEGORY_TOTAL, category="Food")
    result = _result(Intent.QUERY_CATEGORY_TOTAL, query=query)
    msg = format_reply(result)

    assert "April 2026" in msg
    assert "Food" in msg
    assert "engine" in msg.lower()


def test_retrieval_failure_surfaces_friendly_error():
    query = _april_query(Intent.QUERY_PERIOD_TOTAL)
    err = RetrievalError("read timeout from gspread")
    result = _result(Intent.QUERY_PERIOD_TOTAL, query=query)
    msg = format_reply(result, retrieval_error=err)

    assert "couldn't read the ledger" in msg
    assert "read timeout" in msg
    assert "April 2026" in msg


@pytest.mark.parametrize(
    "intent",
    [
        Intent.QUERY_PERIOD_TOTAL,
        Intent.QUERY_CATEGORY_TOTAL,
        Intent.QUERY_DAY,
        Intent.QUERY_RECENT,
    ],
)
def test_retrieval_each_intent_dispatches_through_format_reply(intent):
    query = RetrievalQuery(
        intent=intent,
        time_range=TimeRange(
            start=date(2026, 4, 1),
            end=date(2026, 4, 30),
            label="April 2026",
        ),
        category="Food" if intent == Intent.QUERY_CATEGORY_TOTAL else None,
        limit=3 if intent == Intent.QUERY_RECENT else None,
    )
    answer = RetrievalAnswer(
        intent=intent,
        query=query,
        matched_rows=[_ledger_row()],
        total_usd=12.5,
        transaction_count=1,
        by_category={"Food": 12.5},
        by_day={date(2026, 4, 24): 12.5},
        largest=_ledger_row(),
    )
    result = _result(intent, query=query)
    msg = format_reply(result, retrieval_answer=answer)
    assert "April 2026" in msg
    assert "$12.50" in msg or "12.50" in msg


# ─── Pluralization (regression: "1 transactions" → "1 transaction") ─────


@pytest.mark.parametrize(
    ("count", "expected_phrase"),
    [
        (0, "0 transactions"),
        (1, "1 transaction"),
        (2, "2 transactions"),
        (42, "42 transactions"),
    ],
)
def test_period_total_pluralizes_transaction_count(count, expected_phrase):
    query = _april_query(Intent.QUERY_PERIOD_TOTAL)
    rows = [_ledger_row() for _ in range(count)] if count else []
    answer = RetrievalAnswer(
        intent=Intent.QUERY_PERIOD_TOTAL,
        query=query,
        matched_rows=rows,
        total_usd=12.5 * count,
        transaction_count=count,
        by_category={"Food": 12.5 * count} if count else {},
        by_day={date(2026, 4, 24): 12.5 * count} if count else {},
        largest=rows[0] if rows else None,
    )
    result = _result(Intent.QUERY_PERIOD_TOTAL, query=query)
    msg = format_reply(result, retrieval_answer=answer)

    if count == 0:
        assert "No expenses found" in msg
    else:
        assert expected_phrase in msg
        assert "1 transactions" not in msg


def test_category_total_pluralizes_singular():
    query = _april_query(Intent.QUERY_CATEGORY_TOTAL, category="Food")
    answer = RetrievalAnswer(
        intent=Intent.QUERY_CATEGORY_TOTAL,
        query=query,
        matched_rows=[_ledger_row(category="Food", amount_usd=1.0)],
        total_usd=1.0,
        transaction_count=1,
        by_category={"Food": 1.0},
        by_day={date(2026, 4, 24): 1.0},
        largest=_ledger_row(category="Food", amount_usd=1.0),
    )
    result = _result(Intent.QUERY_CATEGORY_TOTAL, query=query)
    msg = format_reply(result, retrieval_answer=answer)

    assert "1 transaction." in msg or "1 transaction " in msg
    assert "1 transactions" not in msg


def test_recent_pluralizes_expense_noun():
    query = _april_query(Intent.QUERY_RECENT, limit=5)
    one_row = [_ledger_row(category="Food", amount_usd=1.0)]
    answer = RetrievalAnswer(
        intent=Intent.QUERY_RECENT,
        query=query,
        matched_rows=one_row,
        total_usd=1.0,
        transaction_count=1,
        by_category={"Food": 1.0},
        by_day={date(2026, 4, 24): 1.0},
        largest=one_row[0],
    )
    result = _result(Intent.QUERY_RECENT, query=query)
    msg = format_reply(result, retrieval_answer=answer)

    assert "Last 1 expense " in msg
    assert "Last 1 expenses" not in msg
