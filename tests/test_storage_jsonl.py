"""Tests for :class:`JsonlChatStore`."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from expense_tracker.storage import (
    SCHEMA_VERSION,
    ChatStore,
    ConversationTurn,
    LLMCallRecord,
    get_chat_store,
)
from expense_tracker.storage.jsonl_store import JsonlChatStore


def _make_call_record(**overrides) -> LLMCallRecord:
    base = dict(
        provider="fake",
        model="fake-model",
        messages=[{"role": "user", "content": "hi"}],
        response="hello",
        latency_ms=12.3,
    )
    base.update(overrides)
    return LLMCallRecord(**base)


def _make_turn(**overrides) -> ConversationTurn:
    base = dict(user_text="spent 40 on food")
    base.update(overrides)
    return ConversationTurn(**base)


def test_jsonl_store_satisfies_protocol(tmp_path) -> None:
    store = JsonlChatStore(log_dir=tmp_path)
    assert isinstance(store, ChatStore)
    assert store.schema_version == SCHEMA_VERSION


def test_creates_log_dir_on_init(tmp_path) -> None:
    target = tmp_path / "deep" / "nested" / "logs"
    JsonlChatStore(log_dir=target)
    assert target.is_dir()


def test_append_and_iter_round_trip_for_llm_calls(tmp_path) -> None:
    store = JsonlChatStore(log_dir=tmp_path)
    rec_a = _make_call_record(latency_ms=10.0)
    rec_b = _make_call_record(latency_ms=20.0)
    store.append_llm_call(rec_a)
    store.append_llm_call(rec_b)

    out = list(store.iter_llm_calls())
    assert len(out) == 2
    assert out[0].latency_ms == 10.0
    assert out[1].latency_ms == 20.0
    # Every record carries the schema version even if writer didn't set it.
    assert all(r.schema_version == SCHEMA_VERSION for r in out)


def test_append_and_iter_round_trip_for_turns(tmp_path) -> None:
    store = JsonlChatStore(log_dir=tmp_path)
    turn = _make_turn(
        intent="log_expense",
        extracted={"category": "Food", "amount": 40},
        bot_reply="Logged 40 to Food.",
        trace_ids=["tr_abc", "tr_def"],
    )
    store.append_turn(turn)

    out = list(store.iter_turns())
    assert len(out) == 1
    assert out[0].intent == "log_expense"
    assert out[0].extracted == {"category": "Food", "amount": 40}
    assert out[0].trace_ids == ["tr_abc", "tr_def"]


def test_iter_handles_missing_file(tmp_path) -> None:
    """Reading from a freshly-built store (no writes yet) yields nothing,
    not an error."""
    store = JsonlChatStore(log_dir=tmp_path)
    assert list(store.iter_llm_calls()) == []
    assert list(store.iter_turns()) == []


def test_iter_filters_by_time_window(tmp_path) -> None:
    store = JsonlChatStore(log_dir=tmp_path)
    now = datetime.now(tz=timezone.utc)
    earlier = _make_call_record(ts=now - timedelta(hours=2))
    middle = _make_call_record(ts=now - timedelta(hours=1))
    later = _make_call_record(ts=now)
    for r in (earlier, middle, later):
        store.append_llm_call(r)

    out = list(
        store.iter_llm_calls(
            since=now - timedelta(hours=1, minutes=30),
            until=now - timedelta(minutes=30),
        )
    )
    assert len(out) == 1
    assert out[0].ts == middle.ts


def test_streams_are_independent(tmp_path) -> None:
    """LLM calls and conversation turns must NOT bleed into each other's file."""
    store = JsonlChatStore(log_dir=tmp_path)
    store.append_llm_call(_make_call_record())
    store.append_turn(_make_turn())

    assert len(list(store.iter_llm_calls())) == 1
    assert len(list(store.iter_turns())) == 1

    # And their files are physically separate.
    assert store.llm_calls_path != store.conversations_path


def test_concurrent_appends_dont_corrupt(tmp_path) -> None:
    """Hammer the lock with 8 threads x 25 appends each. Every line must
    be valid JSON and the total count must match."""
    store = JsonlChatStore(log_dir=tmp_path)

    n_threads = 8
    per_thread = 25

    def writer(idx: int) -> None:
        for i in range(per_thread):
            store.append_llm_call(_make_call_record(latency_ms=float(idx * 1000 + i)))

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = list(store.iter_llm_calls())
    assert len(out) == n_threads * per_thread


def test_factory_builds_jsonl_store(isolated_env, tmp_path) -> None:
    isolated_env(LOG_DIR=str(tmp_path / "logs"))
    store = get_chat_store()
    assert isinstance(store, JsonlChatStore)
    assert (tmp_path / "logs").is_dir()


def test_unknown_backend_rejected(isolated_env) -> None:
    """Settings already rejects unknown backends at parse time."""
    from pydantic import ValidationError

    isolated_env(CHAT_STORE_BACKEND="postgres")
    with pytest.raises(ValidationError):
        get_chat_store()
