"""Tests for :class:`FakeLLMClient` — both as a tool and as a protocol check."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from expense_tracker.llm import LLMBadResponseError, Message
from expense_tracker.llm._fake import FakeLLMClient
from expense_tracker.llm.base import LLMClient


class _DummyExpense(BaseModel):
    category: str
    amount: float


def test_fake_satisfies_protocol() -> None:
    """If we accidentally break the FakeLLMClient signature, this fails fast."""
    client = FakeLLMClient()
    assert isinstance(client, LLMClient)


def test_fake_returns_default_when_queue_is_empty() -> None:
    client = FakeLLMClient(default_text='{"hello": "world"}')
    resp = client.complete([Message.user("anything")])
    assert resp.content == '{"hello": "world"}'
    assert resp.provider == "fake"


def test_fake_returns_queued_responses_in_order() -> None:
    client = FakeLLMClient()
    client.queue_response("first")
    client.queue_response("second")
    assert client.complete([Message.user("a")]).content == "first"
    assert client.complete([Message.user("b")]).content == "second"
    assert client.complete([Message.user("c")]).content == '{"ok": true}'  # default


def test_fake_records_calls_for_assertions() -> None:
    client = FakeLLMClient()
    client.complete([Message.system("sys"), Message.user("hi")])
    client.complete([Message.user("hello again")])
    calls = client.calls
    assert len(calls) == 2
    assert calls[0][0].role == "system"
    assert calls[1][0].content == "hello again"


def test_fake_complete_json_parses_pydantic_model() -> None:
    client = FakeLLMClient()
    client.queue_response('{"category": "Food", "amount": 40.0}')
    parsed, raw = client.complete_json([Message.user("...")], schema=_DummyExpense)
    assert parsed.category == "Food"
    assert parsed.amount == 40.0
    assert raw.content == '{"category": "Food", "amount": 40.0}'


def test_fake_complete_json_raises_on_garbage() -> None:
    client = FakeLLMClient()
    client.queue_response("definitely not json")
    with pytest.raises(LLMBadResponseError):
        client.complete_json([Message.user("...")], schema=_DummyExpense)


def test_fake_complete_json_raises_on_schema_mismatch() -> None:
    client = FakeLLMClient()
    client.queue_response('{"category": "Food"}')  # missing amount
    with pytest.raises(LLMBadResponseError):
        client.complete_json([Message.user("...")], schema=_DummyExpense)


def test_fake_reset_clears_state() -> None:
    client = FakeLLMClient()
    client.queue_response("first")
    client.complete([Message.user("a")])
    client.reset()
    assert client.calls == []
    # Default text after reset:
    assert client.complete([Message.user("b")]).content == '{"ok": true}'
