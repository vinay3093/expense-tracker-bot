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
* retrieval (any of the 4 query intents) → no row, Step-6 stub reply,
  one turn persisted with the parsed query echoed back.
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
from expense_tracker.llm._fake import FakeLLMClient
from expense_tracker.pipeline.chat import ChatPipeline, ChatTurn
from expense_tracker.pipeline.exceptions import ExpenseLogError
from expense_tracker.pipeline.logger import ExpenseLogger
from expense_tracker.sheets.backend import FakeSheetsBackend
from expense_tracker.sheets.currency import CurrencyConverter, CurrencyError
from expense_tracker.sheets.format import get_sheet_format
from expense_tracker.sheets.transactions import col_for as txn_col_for
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
    pipeline = ChatPipeline(orchestrator=orch, expense_logger=expense_logger)
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


def test_retrieval_stub_acknowledges_query(fake_llm, tmp_path):
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
    pipeline, store, _backend = _build_pipeline(fake_llm, log_dir=tmp_path)

    turn = pipeline.chat("how much for food in april?")

    assert turn.ok is True
    assert turn.log_result is None
    assert turn.intent.value == "query_category_total"
    assert "Step 6" in turn.bot_reply
    assert "April 2026" in turn.bot_reply
    assert "Food" in turn.bot_reply

    persisted = next(iter(store.iter_turns()))
    assert persisted.action is None
    assert persisted.extracted is not None
    assert persisted.extracted["type"] == "query"


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
