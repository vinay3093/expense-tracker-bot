"""End-to-end ChatPipeline tests.

The pipeline is the layer the CLI / Telegram bot calls. We exercise it
with:

* a programmable :class:`FakeLLMClient` for both extraction stages,
* a :class:`FakeSheetsBackend` for the destination spreadsheet,
* a USD-primary :class:`CurrencyConverter` rooted in tmp_path so no
  network is touched.

What we assert end-to-end:

* log_expense → row is written, reply is friendly, ConversationTurn is
  persisted *exactly once* with action + bot_reply populated.
* smalltalk → no row, helpful reply, one turn persisted.
* unclear → no row, hint reply, one turn persisted.
* retrieval (any of the 4 query intents) → routed through
  :class:`RetrievalEngine`, ledger is read + aggregated, reply quotes
  totals, one turn persisted with a ``sheets_query`` action.
* sheets failure (FX exploding) → ChatTurn carries the error, action
  status is "error", bot_reply tells the user what happened, one turn
  persisted, ``ok == False``.
* extractor stage-1 failure → graceful "couldn't parse" reply.
* persist_turn writes one and only one turn per chat() call.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from expense_tracker.extractor.categories import get_registry
from expense_tracker.extractor.orchestrator import Orchestrator
from expense_tracker.ledger.sheets.backend import FakeSheetsBackend
from expense_tracker.ledger.sheets.currency import CurrencyConverter, CurrencyError
from expense_tracker.ledger.sheets.format import get_sheet_format
from expense_tracker.ledger.sheets.transactions import col_for as txn_col_for
from expense_tracker.llm._fake import FakeLLMClient
from expense_tracker.pipeline.chat import ChatPipeline, ChatTurn
from expense_tracker.pipeline.exceptions import ExpenseLogError
from expense_tracker.pipeline.logger import ExpenseLogger
from expense_tracker.pipeline.retrieval import RetrievalEngine
from expense_tracker.storage.jsonl_store import JsonlChatStore

TZ = "America/Chicago"
FROZEN_NOW = datetime(2026, 4, 24, 14, 30, tzinfo=ZoneInfo(TZ))


def _frozen_now() -> datetime:
    return FROZEN_NOW


def _queue_json(fake_llm: FakeLLMClient, *responses: dict) -> None:
    for r in responses:
        fake_llm.queue_response(json.dumps(r))


def _build_pipeline(
    fake_llm: FakeLLMClient,
    *,
    log_dir,
    backend=None,
    converter=None,
):
    store = JsonlChatStore(log_dir=log_dir)
    fmt = get_sheet_format()
    registry = get_registry()
    backend = backend or FakeSheetsBackend(
        title="Expense Tracker (test)", spreadsheet_id="sid_test"
    )
    converter = converter or CurrencyConverter(
        primary_currency="USD",
        cache_path=log_dir / "fx_cache.json",
        timeout_s=0.001,
    )
    orch = Orchestrator(
        llm=fake_llm,
        store=store,
        registry=registry,
        timezone=TZ,
        default_currency="USD",
        now=_frozen_now,
    )
    expense_logger = ExpenseLogger(
        backend=backend,
        sheet_format=fmt,
        registry=registry,
        converter=converter,
        timezone=TZ,
        now=_frozen_now,
    )
    retrieval_engine = RetrievalEngine(
        backend=backend,
        sheet_format=fmt,
        registry=registry,
    )
    pipeline = ChatPipeline(
        orchestrator=orch,
        expense_logger=expense_logger,
        retrieval_engine=retrieval_engine,
    )
    return pipeline, store, backend


# ─── log_expense ────────────────────────────────────────────────────────


def test_log_expense_full_chat_writes_row_and_persists_one_turn(
    fake_llm, tmp_path,
):
    _queue_json(
        fake_llm,
        {"intent": "log_expense", "confidence": 0.95, "reasoning": "spending verb"},
        {
            "date": "2026-04-24",
            "category": "Food",
            "amount": 12.5,
            "currency": "USD",
            "vendor": "Starbucks",
            "note": "latte",
        },
    )
    pipeline, store, backend = _build_pipeline(fake_llm, log_dir=tmp_path)
    fmt = get_sheet_format()

    turn = pipeline.chat("spent 12.50 on coffee at starbucks")

    # Public ChatTurn shape.
    assert isinstance(turn, ChatTurn)
    assert turn.ok is True
    assert turn.intent.value == "log_expense"
    assert turn.log_result is not None
    assert turn.log_error is None
    assert "Logged $12.50 to Food" in turn.bot_reply
    assert turn.session_id is not None
    assert len(turn.trace_ids) == 2

    # Sheet was written.
    txns = backend.get_worksheet(fmt.transactions.sheet_name)
    assert txns.cell(f"{txn_col_for('amount_usd')}2") == 12.5
    assert txns.cell(f"{txn_col_for('note')}2") == "latte"

    # Conversation persisted exactly once with the resolved action.
    turns = list(store.iter_turns())
    assert len(turns) == 1
    persisted = turns[0]
    assert persisted.intent == "log_expense"
    assert persisted.bot_reply == turn.bot_reply
    assert persisted.action is not None
    assert persisted.action["status"] == "ok"
    assert persisted.action["category"] == "Food"
    assert persisted.action["amount_usd"] == 12.5
    assert persisted.session_id == turn.session_id


def test_log_expense_with_inr_uses_cached_fx(fake_llm, tmp_path):
    _queue_json(
        fake_llm,
        {"intent": "log_expense", "confidence": 0.9, "reasoning": "INR spend"},
        {
            "date": "2026-04-24",
            "category": "Digital",
            "amount": 499,
            "currency": "INR",
            "vendor": None,
            "note": "netflix",
        },
    )
    converter = CurrencyConverter(
        primary_currency="USD",
        cache_path=tmp_path / "fx_cache.json",
        timeout_s=0.001,
    )
    converter._cache.put(FROZEN_NOW.date(), "INR", "USD", 0.012)
    pipeline, _store, _backend = _build_pipeline(
        fake_llm, log_dir=tmp_path, converter=converter,
    )

    turn = pipeline.chat("paid 499 RS for netflix")

    assert turn.ok is True
    assert turn.log_result is not None
    assert turn.log_result.row.fx_rate == pytest.approx(0.012)
    assert "499.00 INR" in turn.bot_reply
    assert "$5.99 USD" in turn.bot_reply


def test_log_expense_creates_monthly_tab_lazily(fake_llm, tmp_path):
    _queue_json(
        fake_llm,
        {"intent": "log_expense", "confidence": 0.9, "reasoning": "spend"},
        {
            "date": "2026-05-03",
            "category": "Food",
            "amount": 8,
            "currency": "USD",
        },
    )
    pipeline, _store, backend = _build_pipeline(fake_llm, log_dir=tmp_path)

    turn = pipeline.chat("spent 8 on lunch on may 3")

    assert turn.ok is True
    assert turn.log_result.monthly_tab_created is True
    assert backend.has_worksheet(turn.log_result.monthly_tab)
    assert "May 2026" in turn.log_result.monthly_tab


# ─── Non-log intents ────────────────────────────────────────────────────


def test_smalltalk_short_circuits(fake_llm, tmp_path):
    _queue_json(
        fake_llm,
        {"intent": "smalltalk", "confidence": 0.99, "reasoning": "thanks"},
    )
    pipeline, store, backend = _build_pipeline(fake_llm, log_dir=tmp_path)
    fmt = get_sheet_format()

    turn = pipeline.chat("thanks!")

    assert turn.ok is True
    assert turn.intent.value == "smalltalk"
    assert turn.log_result is None
    assert turn.log_error is None
    assert turn.bot_reply
    # No row, no transactions tab.
    assert not backend.has_worksheet(fmt.transactions.sheet_name)
    # One turn persisted.
    turns = list(store.iter_turns())
    assert len(turns) == 1
    assert turns[0].intent == "smalltalk"
    assert turns[0].action is None


def test_unclear_short_circuits(fake_llm, tmp_path):
    _queue_json(
        fake_llm,
        {"intent": "unclear", "confidence": 0.0, "reasoning": "?"},
    )
    pipeline, store, _backend = _build_pipeline(fake_llm, log_dir=tmp_path)

    turn = pipeline.chat("xyzzy")

    assert turn.ok is True
    assert turn.log_result is None
    assert turn.intent.value == "unclear"
    assert "didn't catch" in turn.bot_reply.lower()
    assert len(list(store.iter_turns())) == 1


def _seed_transactions(backend, fmt, *, rows):
    """Populate the FakeSheetsBackend's Transactions tab for retrieval.

    Goes through the real :func:`init_transactions_tab` so the schema
    matches what the engine will read — header row, column types, and
    column ordering.
    """
    from expense_tracker.ledger.sheets.transactions import (
        TransactionRow,
        append_transactions,
        init_transactions_tab,
    )
    init_transactions_tab(backend, fmt)
    txn_rows = []
    for r in rows:
        txn_rows.append(
            TransactionRow(
                date=r["date"],
                day=r["date"].strftime("%a"),
                month=r["date"].strftime("%B"),
                year=r["date"].year,
                category=r["category"],
                note=r.get("note"),
                vendor=r.get("vendor"),
                amount=r["amount"],
                currency=r.get("currency", "USD"),
                amount_usd=r.get("amount_usd", r["amount"]),
                fx_rate=r.get("fx_rate", 1.0),
                source="chat",
                trace_id=r.get("trace_id"),
                timestamp=None,
            )
        )
    append_transactions(backend, fmt, txn_rows)


def test_retrieval_query_category_total_routes_through_engine(
    fake_llm, tmp_path,
):
    """End-to-end: 'how much on food in april?' → engine reads the
    Transactions tab, sums the matching rows, and the bot reply quotes
    the total + count."""
    from datetime import date as date_cls

    _queue_json(
        fake_llm,
        {
            "intent": "query_category_total",
            "confidence": 0.9,
            "reasoning": "asking for category total",
        },
        {
            "intent": "query_category_total",
            "time_range": {
                "start": "2026-04-01",
                "end": "2026-04-30",
                "label": "April 2026",
            },
            "category": "Food",
            "vendor": None,
            "limit": None,
        },
    )
    pipeline, store, backend = _build_pipeline(fake_llm, log_dir=tmp_path)
    fmt = get_sheet_format()

    _seed_transactions(backend, fmt, rows=[
        {"date": date_cls(2026, 4, 5),  "category": "Food",
         "amount": 12.50, "vendor": "Starbucks", "note": "coffee"},
        {"date": date_cls(2026, 4, 18), "category": "Food",
         "amount": 45.00, "vendor": "Chipotle", "note": "lunch"},
        # Out of category — must be excluded.
        {"date": date_cls(2026, 4, 18), "category": "Groceries",
         "amount": 88.00, "vendor": "Costco"},
        # Out of window — must be excluded.
        {"date": date_cls(2026, 3, 30), "category": "Food",
         "amount": 100.00, "vendor": "Capital Grille"},
    ])

    turn = pipeline.chat("how much for food in april?")

    assert turn.ok is True
    assert turn.log_result is None
    assert turn.retrieval_error is None
    assert turn.retrieval_answer is not None
    assert turn.intent.value == "query_category_total"

    answer = turn.retrieval_answer
    assert answer.transaction_count == 2
    assert answer.total_usd == pytest.approx(57.50)
    assert answer.by_category == {"Food": 57.50}
    assert answer.largest is not None
    assert answer.largest.amount_usd == pytest.approx(45.0)
    assert answer.largest.vendor == "Chipotle"

    # Friendly reply quotes the total + window + largest.
    assert "April 2026 / Food" in turn.bot_reply
    assert "$57.50" in turn.bot_reply
    assert "2 transactions" in turn.bot_reply
    assert "Chipotle" in turn.bot_reply

    # Conversation turn persisted with the structured action shape.
    persisted = next(iter(store.iter_turns()))
    assert persisted.intent == "query_category_total"
    assert persisted.action is not None
    assert persisted.action["type"] == "sheets_query"
    assert persisted.action["status"] == "ok"
    assert persisted.action["total_usd"] == pytest.approx(57.50)
    assert persisted.action["transaction_count"] == 2
    assert persisted.extracted is not None
    assert persisted.extracted["type"] == "query"


def test_retrieval_query_period_total_aggregates_full_window(
    fake_llm, tmp_path,
):
    """A period-total query sums *every* category and lists the top."""
    from datetime import date as date_cls

    _queue_json(
        fake_llm,
        {"intent": "query_period_total", "confidence": 0.9, "reasoning": "month total"},
        {
            "intent": "query_period_total",
            "time_range": {
                "start": "2026-04-01",
                "end": "2026-04-30",
                "label": "April 2026",
            },
            "category": None,
            "vendor": None,
            "limit": None,
        },
    )
    pipeline, _store, backend = _build_pipeline(fake_llm, log_dir=tmp_path)
    fmt = get_sheet_format()

    _seed_transactions(backend, fmt, rows=[
        {"date": date_cls(2026, 4, 5),  "category": "Food",      "amount": 12.50},
        {"date": date_cls(2026, 4, 8),  "category": "Groceries", "amount": 88.00},
        {"date": date_cls(2026, 4, 18), "category": "Food",      "amount": 45.00},
        {"date": date_cls(2026, 4, 22), "category": "Tesla Car", "amount": 240.00},
    ])

    turn = pipeline.chat("how much in april?")

    assert turn.ok is True
    answer = turn.retrieval_answer
    assert answer is not None
    assert answer.transaction_count == 4
    assert answer.total_usd == pytest.approx(385.50)
    assert answer.by_category == {
        "Food": 57.50, "Groceries": 88.00, "Tesla Car": 240.00,
    }
    # Reply lists top categories with totals.
    assert "$385.50" in turn.bot_reply
    assert "Tesla Car $240.00" in turn.bot_reply


def test_retrieval_query_recent_returns_top_n_only(fake_llm, tmp_path):
    """`query_recent` slices to the last N rows but counts the full window."""
    from datetime import date as date_cls

    _queue_json(
        fake_llm,
        {"intent": "query_recent", "confidence": 0.9, "reasoning": "show recent"},
        {
            "intent": "query_recent",
            "time_range": {
                "start": "2026-04-01",
                "end": "2026-04-30",
                "label": "April 2026",
            },
            "category": None,
            "vendor": None,
            "limit": 3,
        },
    )
    pipeline, _store, backend = _build_pipeline(fake_llm, log_dir=tmp_path)
    fmt = get_sheet_format()

    _seed_transactions(backend, fmt, rows=[
        {"date": date_cls(2026, 4, d), "category": "Food",
         "amount": 10.0 + d, "vendor": f"V{d}"}
        for d in (5, 8, 12, 18, 24, 26)
    ])

    turn = pipeline.chat("show last 3 transactions")

    assert turn.ok is True
    answer = turn.retrieval_answer
    assert answer is not None
    assert answer.transaction_count == 6  # full window
    assert len(answer.matched_rows) == 3  # truncated to limit
    # Sorted newest-first.
    assert [r.date.day for r in answer.matched_rows] == [26, 24, 18]
    assert "Last 3" in turn.bot_reply
    assert "of 6 in April 2026" in turn.bot_reply


def test_retrieval_empty_window_says_no_matches(fake_llm, tmp_path):
    """Query with no matching rows yields a 'no expenses found' reply."""
    _queue_json(
        fake_llm,
        {"intent": "query_period_total", "confidence": 0.9, "reasoning": "month total"},
        {
            "intent": "query_period_total",
            "time_range": {
                "start": "2026-04-01",
                "end": "2026-04-30",
                "label": "April 2026",
            },
            "category": None,
            "vendor": None,
            "limit": None,
        },
    )
    pipeline, _store, _backend = _build_pipeline(fake_llm, log_dir=tmp_path)

    turn = pipeline.chat("how much in april?")

    assert turn.ok is True
    answer = turn.retrieval_answer
    assert answer is not None
    assert answer.transaction_count == 0
    assert answer.total_usd == 0.0
    assert "No expenses found" in turn.bot_reply
    assert "April 2026" in turn.bot_reply


# ─── Failure paths ──────────────────────────────────────────────────────


class _ExplodingConverter:
    primary_currency = "USD"

    def convert(self, amount, from_currency, *, to_currency=None, on_date=None):
        raise CurrencyError(f"boom: {from_currency}->USD")


def test_sheets_failure_surfaces_in_chat_turn_and_persists_error_action(
    fake_llm, tmp_path,
):
    _queue_json(
        fake_llm,
        {"intent": "log_expense", "confidence": 0.9, "reasoning": "spend"},
        {
            "date": "2026-04-24",
            "category": "Digital",
            "amount": 499,
            "currency": "INR",
        },
    )
    pipeline, store, backend = _build_pipeline(
        fake_llm,
        log_dir=tmp_path,
        converter=_ExplodingConverter(),
    )

    turn = pipeline.chat("paid 499 RS for netflix")

    assert turn.ok is False
    assert turn.log_result is None
    assert isinstance(turn.log_error, ExpenseLogError)
    assert "couldn't write" in turn.bot_reply
    assert "499" in turn.bot_reply

    # No row — the Transactions tab was never even created (FX failed first).
    fmt = get_sheet_format()
    assert not backend.has_worksheet(fmt.transactions.sheet_name)

    persisted = next(iter(store.iter_turns()))
    assert persisted.action is not None
    assert persisted.action["status"] == "error"
    assert "boom" in persisted.action["error"]


def test_extractor_stage1_failure_recovers(fake_llm, tmp_path):
    # Queue something that won't parse as IntentClassification.
    fake_llm.queue_response("not even json")

    pipeline, store, _backend = _build_pipeline(fake_llm, log_dir=tmp_path)

    turn = pipeline.chat("spent 40 on food")

    # Pipeline didn't crash — graceful fallback.
    assert turn.bot_reply
    assert "couldn't parse" in turn.bot_reply or "didn't catch" in turn.bot_reply
    assert len(list(store.iter_turns())) == 1


# ─── Persistence guarantees ─────────────────────────────────────────────


def test_each_chat_call_persists_exactly_one_turn(fake_llm, tmp_path):
    _queue_json(
        fake_llm,
        {"intent": "smalltalk", "confidence": 0.99, "reasoning": "hi"},
        {"intent": "smalltalk", "confidence": 0.99, "reasoning": "bye"},
    )
    pipeline, store, _backend = _build_pipeline(fake_llm, log_dir=tmp_path)

    pipeline.chat("hi")
    pipeline.chat("bye")

    turns = list(store.iter_turns())
    assert len(turns) == 2
