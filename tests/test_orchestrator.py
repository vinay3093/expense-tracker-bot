"""End-to-end tests for the extractor :class:`Orchestrator`.

Uses a :class:`FakeLLMClient` queueing one response per stage. Verifies:

* Smalltalk / unclear short-circuit (one LLM call total).
* log_expense → ExpenseEntry payload.
* query_* → RetrievalQuery payload.
* ConversationTurn is persisted to the chat store.
* Trace IDs are collected from each stage and exposed on the result.
* session_id propagates from the orchestrator into the trace records.
* Tracing wrapper integration (when LLM_TRACE=true).
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from expense_tracker.extractor.categories import get_registry
from expense_tracker.extractor.orchestrator import Orchestrator
from expense_tracker.extractor.schemas import Intent
from expense_tracker.llm._fake import FakeLLMClient
from expense_tracker.llm._traced import TracedLLMClient
from expense_tracker.storage.jsonl_store import JsonlChatStore

TZ = "America/Chicago"
FROZEN_NOW = datetime(2026, 4, 24, 12, 0, tzinfo=ZoneInfo(TZ))


def _frozen_now() -> datetime:
    return FROZEN_NOW


def _build_orch(
    fake_llm: FakeLLMClient,
    *,
    log_dir,
    wrap_trace: bool = False,
) -> tuple[Orchestrator, JsonlChatStore]:
    store = JsonlChatStore(log_dir=log_dir)
    llm = TracedLLMClient(inner=fake_llm, store=store) if wrap_trace else fake_llm
    orch = Orchestrator(
        llm=llm,
        store=store,
        registry=get_registry(),
        timezone=TZ,
        default_currency="INR",
        now=_frozen_now,
    )
    return orch, store


def _queue(fake_llm: FakeLLMClient, *responses: dict) -> None:
    for r in responses:
        fake_llm.queue_response(json.dumps(r))


# ─── Happy paths ────────────────────────────────────────────────────────

def test_log_expense_full_pipeline(fake_llm, tmp_path):
    _queue(
        fake_llm,
        {"intent": "log_expense", "confidence": 0.95, "reasoning": "spending verb"},
        {
            "date": "2026-04-24",
            "category": "Food",
            "amount": 40,
            "currency": "INR",
            "vendor": None,
            "note": None,
        },
    )
    orch, store = _build_orch(fake_llm, log_dir=tmp_path)

    result = orch.extract("spent 40 on food today")

    assert result.intent == Intent.LOG_EXPENSE
    assert result.is_actionable()
    assert result.expense is not None
    assert result.expense.amount == 40.0
    assert result.expense.category == "Food"
    assert result.error is None
    assert len(result.trace_ids) == 2  # stage 1 + stage 2
    assert result.session_id is not None

    # ConversationTurn was persisted.
    turns = list(store.iter_turns())
    assert len(turns) == 1
    turn = turns[0]
    assert turn.intent == "log_expense"
    assert turn.user_text == "spent 40 on food today"
    assert turn.extracted is not None
    assert turn.extracted["type"] == "expense"
    assert turn.extracted["amount"] == 40.0


def test_query_period_total_full_pipeline(fake_llm, tmp_path):
    _queue(
        fake_llm,
        {"intent": "query_period_total", "confidence": 0.9, "reasoning": "asks how much"},
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
    orch, store = _build_orch(fake_llm, log_dir=tmp_path)

    result = orch.extract("how much did I spend in April")

    assert result.intent == Intent.QUERY_PERIOD_TOTAL
    assert result.query is not None
    assert result.query.time_range.label == "April 2026"
    assert len(result.trace_ids) == 2

    turn = next(iter(store.iter_turns()))
    assert turn.extracted["type"] == "query"
    assert turn.intent == "query_period_total"


def test_smalltalk_short_circuits(fake_llm, tmp_path):
    """Smalltalk skips stage 2 entirely — only one LLM call."""
    _queue(
        fake_llm,
        {"intent": "smalltalk", "confidence": 0.99, "reasoning": "greeting"},
    )
    orch, store = _build_orch(fake_llm, log_dir=tmp_path)

    result = orch.extract("thanks!")

    assert result.intent == Intent.SMALLTALK
    assert not result.is_actionable()
    assert len(result.trace_ids) == 1
    assert len(fake_llm.calls) == 1

    turn = next(iter(store.iter_turns()))
    assert turn.extracted is None


def test_unclear_short_circuits(fake_llm, tmp_path):
    _queue(
        fake_llm,
        {"intent": "unclear", "confidence": 0.3, "reasoning": "ambiguous"},
    )
    orch, _ = _build_orch(fake_llm, log_dir=tmp_path)

    result = orch.extract("hmm")

    assert result.intent == Intent.UNCLEAR
    assert not result.is_actionable()
    assert len(fake_llm.calls) == 1


# ─── Failure paths ──────────────────────────────────────────────────────

def test_empty_message_returns_unclear_without_calling_llm(fake_llm, tmp_path):
    orch, _ = _build_orch(fake_llm, log_dir=tmp_path)

    result = orch.extract("   ")

    assert result.intent == Intent.UNCLEAR
    assert len(fake_llm.calls) == 0
    assert result.trace_ids == []


def test_stage1_bad_response_yields_error_result(fake_llm, tmp_path):
    fake_llm.queue_response("not json at all")
    orch, store = _build_orch(fake_llm, log_dir=tmp_path)

    result = orch.extract("spent 40 on food")

    assert result.intent == Intent.UNCLEAR
    assert result.error is not None
    assert "Intent classification failed" in result.error
    # Turn still persisted.
    assert len(list(store.iter_turns())) == 1


def test_stage2_bad_response_keeps_intent_but_reports_error(fake_llm, tmp_path):
    _queue(
        fake_llm,
        {"intent": "log_expense", "confidence": 0.9, "reasoning": "spending"},
    )
    fake_llm.queue_response("definitely not JSON")
    orch, _ = _build_orch(fake_llm, log_dir=tmp_path)

    result = orch.extract("spent 40 on food")

    assert result.intent == Intent.LOG_EXPENSE
    assert result.expense is None
    assert result.error is not None
    assert "Stage-2 extraction failed" in result.error
    assert len(result.trace_ids) == 1  # only stage 1 made it


# ─── Tracing integration ────────────────────────────────────────────────

def test_session_id_links_traces_when_wrapped(fake_llm, tmp_path):
    _queue(
        fake_llm,
        {"intent": "log_expense", "confidence": 0.95, "reasoning": "spending"},
        {
            "date": "2026-04-24",
            "category": "Food",
            "amount": 40,
            "currency": "INR",
        },
    )
    orch, store = _build_orch(fake_llm, log_dir=tmp_path, wrap_trace=True)

    result = orch.extract("spent 40 on food")

    records = list(store.iter_llm_calls())
    assert len(records) == 2
    # Both LLM call records carry the same session_id as the result.
    assert {r.session_id for r in records} == {result.session_id}
    # And the trace_ids on the result match the request_ids on the records.
    assert {r.trace_id for r in records} == set(result.trace_ids)


def test_from_settings_wires_defaults(isolated_env, tmp_path):
    """Smoke test: ``Orchestrator.from_settings`` works for the fake provider."""
    isolated_env(
        LLM_PROVIDER="fake",
        LOG_DIR=str(tmp_path),
        LLM_TRACE="false",
        TIMEZONE="UTC",
        DEFAULT_CURRENCY="INR",
    )
    from expense_tracker.config import get_settings

    cfg = get_settings()
    orch = Orchestrator.from_settings(cfg)
    # We don't actually call extract() — we'd need to queue responses on
    # an internal FakeLLMClient we don't hold. Just confirm it built.
    assert orch is not None


# ─── Reasoning passthrough ──────────────────────────────────────────────

def test_reasoning_is_preserved(fake_llm, tmp_path):
    _queue(
        fake_llm,
        {
            "intent": "log_expense",
            "confidence": 0.92,
            "reasoning": "explicit spending verb 'spent' + amount + category",
        },
        {
            "date": "2026-04-24",
            "category": "Food",
            "amount": 40,
            "currency": "INR",
        },
    )
    orch, _ = _build_orch(fake_llm, log_dir=tmp_path)

    result = orch.extract("spent 40 on food")

    assert "spending verb" in result.reasoning


# ─── Today injection ────────────────────────────────────────────────────

def test_today_uses_injected_now(fake_llm, tmp_path):
    """The ``now`` callable should reach the stage-2 prompt."""
    _queue(
        fake_llm,
        {"intent": "log_expense", "confidence": 0.95, "reasoning": "spending"},
        {
            "date": "2026-04-24",
            "category": "Food",
            "amount": 40,
            "currency": "INR",
        },
    )
    orch, _ = _build_orch(fake_llm, log_dir=tmp_path)

    orch.extract("spent 40 on food")

    # Stage 2 (calls[1]) should show the frozen date in its system prompt.
    stage2_messages = fake_llm.calls[1]
    rendered = "\n".join(m.content for m in stage2_messages)
    assert "2026-04-24" in rendered


@pytest.mark.parametrize(
    ("intent_value", "intent_enum"),
    [
        ("query_period_total", Intent.QUERY_PERIOD_TOTAL),
        ("query_category_total", Intent.QUERY_CATEGORY_TOTAL),
        ("query_day", Intent.QUERY_DAY),
        ("query_recent", Intent.QUERY_RECENT),
    ],
)
def test_all_query_intents_dispatch_to_retrieval_extractor(
    fake_llm, tmp_path, intent_value, intent_enum
):
    _queue(
        fake_llm,
        {"intent": intent_value, "confidence": 0.9, "reasoning": "test"},
        {
            "intent": intent_value,
            "time_range": {
                "start": "2026-04-24",
                "end": "2026-04-24",
                "label": "today",
            },
            "category": None,
            "vendor": None,
            "limit": 5 if intent_value == "query_recent" else None,
        },
    )
    orch, _ = _build_orch(fake_llm, log_dir=tmp_path)

    result = orch.extract("some query text")

    assert result.intent == intent_enum
    assert result.query is not None
    assert result.expense is None
