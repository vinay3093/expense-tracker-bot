"""Tests for :class:`TracedLLMClient` and the factory's auto-wrap behaviour."""

from __future__ import annotations

import logging

from pydantic import BaseModel

from expense_tracker.llm import LLMBadResponseError, Message, get_llm_client
from expense_tracker.llm._fake import FakeLLMClient
from expense_tracker.llm._traced import TracedLLMClient
from expense_tracker.llm.base import LLMClient
from expense_tracker.storage.jsonl_store import JsonlChatStore


class _Expense(BaseModel):
    category: str
    amount: float


def _make_traced(tmp_path) -> tuple[TracedLLMClient, FakeLLMClient, JsonlChatStore]:
    inner = FakeLLMClient()
    store = JsonlChatStore(log_dir=tmp_path)
    return TracedLLMClient(inner=inner, store=store), inner, store


# ─── Identity & protocol conformance ────────────────────────────────────

def test_traced_satisfies_protocol(tmp_path) -> None:
    traced, _, _ = _make_traced(tmp_path)
    assert isinstance(traced, LLMClient)


def test_traced_mirrors_inner_identity(tmp_path) -> None:
    traced, inner, _ = _make_traced(tmp_path)
    assert traced.provider_name == inner.provider_name
    assert traced.model == inner.model
    assert traced.inner is inner


# ─── Successful calls produce trace records ─────────────────────────────

def test_complete_records_one_trace_per_call(tmp_path) -> None:
    traced, _, store = _make_traced(tmp_path)
    traced.complete([Message.user("hi")])
    traced.complete([Message.user("again")])

    records = list(store.iter_llm_calls())
    assert len(records) == 2
    assert all(r.outcome == "ok" for r in records)
    assert all(r.json_mode is False for r in records)
    assert records[0].messages == [{"role": "user", "content": "hi"}]


def test_complete_json_records_schema_name_and_json_mode(tmp_path) -> None:
    traced, inner, store = _make_traced(tmp_path)
    inner.queue_response('{"category": "Food", "amount": 40}')
    traced.complete_json([Message.user("...")], schema=_Expense)

    [rec] = list(store.iter_llm_calls())
    assert rec.json_mode is True
    assert rec.schema_name == "_Expense"
    assert rec.outcome == "ok"
    assert "Food" in rec.response


def test_recorded_metadata_matches_response(tmp_path) -> None:
    traced, _, store = _make_traced(tmp_path)
    resp = traced.complete([Message.user("ping")])

    [rec] = list(store.iter_llm_calls())
    assert rec.provider == resp.provider
    assert rec.model == resp.model
    assert rec.latency_ms == resp.latency_ms
    assert rec.response == resp.content


# ─── Error path also produces a trace record ───────────────────────────

def test_complete_json_error_is_traced_and_re_raised(tmp_path) -> None:
    traced, inner, store = _make_traced(tmp_path)
    inner.queue_response("definitely not json")  # → LLMBadResponseError

    try:
        traced.complete_json([Message.user("...")], schema=_Expense)
    except LLMBadResponseError:
        pass
    else:
        raise AssertionError("Expected LLMBadResponseError to propagate")

    [rec] = list(store.iter_llm_calls())
    assert rec.outcome == "error"
    assert rec.error_type == "LLMBadResponseError"
    assert rec.error_message  # non-empty


# ─── Tracing failures must NEVER break the user's call ─────────────────

class _BrokenStore:
    """Store that throws on every append. Used to verify graceful degradation."""

    schema_version = 1

    def append_llm_call(self, record):
        del record
        raise OSError("disk on fire")

    def append_turn(self, turn):
        del turn
        raise OSError("disk on fire")

    def iter_llm_calls(self, **_):
        return iter([])

    def iter_turns(self, **_):
        return iter([])


def test_store_failures_are_swallowed_with_warning(tmp_path, caplog) -> None:
    inner = FakeLLMClient()
    traced = TracedLLMClient(inner=inner, store=_BrokenStore())

    with caplog.at_level(logging.WARNING):
        resp = traced.complete([Message.user("still works?")])

    assert resp.content  # the user's call succeeded
    assert any("Failed to persist LLM trace" in r.message for r in caplog.records)


# ─── Factory auto-wraps when LLM_TRACE=true (the default) ──────────────

def test_factory_wraps_with_tracer_by_default(isolated_env, tmp_path) -> None:
    isolated_env(LLM_PROVIDER="fake", LOG_DIR=str(tmp_path / "logs"))
    client = get_llm_client()
    assert isinstance(client, TracedLLMClient)
    # Make a call, then check the JSONL exists and has one line.
    client.complete([Message.user("hi")])
    with open(tmp_path / "logs" / "llm_calls.jsonl", encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]
    assert len(lines) == 1


def test_factory_skips_tracer_when_disabled(isolated_env) -> None:
    isolated_env(LLM_PROVIDER="fake", LLM_TRACE="false")
    client = get_llm_client()
    assert isinstance(client, FakeLLMClient)
