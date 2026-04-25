"""Storage layer interface — chat history & LLM call traces.

Single, swappable abstraction so the rest of the app never knows whether
records live in a JSONL file, SQLite, DuckDB, or a vector store. Today
the only concrete implementation is
:class:`~expense_tracker.storage.jsonl_store.JsonlChatStore`. Tomorrow
we add SQLite/DuckDB if scale demands.

Two record types — they live in **separate** physical streams:

* :class:`LLMCallRecord` — one per LLM round-trip. Used for prompt
  debugging, cost tracking, regression replay. Written automatically by
  :class:`~expense_tracker.llm._traced.TracedLLMClient`.
* :class:`ConversationTurn` — one per user message + bot response pair.
  Used for audit trail, retrieval ("what did I tell the bot last
  Tuesday?"), few-shot personalization. Written by application code
  (Step 3+) — not by the LLM client.

A single user turn typically produces ONE :class:`ConversationTurn` and
ONE-OR-MORE :class:`LLMCallRecord`s; they're linked by ``trace_ids``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

#: Bumped whenever the on-disk JSON shape changes incompatibly.
#: Readers should refuse / migrate any record whose ``schema_version``
#: doesn't match the writer's expectation.
SCHEMA_VERSION: int = 1


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_trace_id() -> str:
    return f"tr_{uuid.uuid4().hex[:12]}"


def _new_session_id() -> str:
    return f"s_{uuid.uuid4().hex[:10]}"


# ─── Records ────────────────────────────────────────────────────────────

class LLMCallRecord(BaseModel):
    """One LLM round-trip captured for replay and observability.

    ``messages`` is the exact prompt list sent to the provider — this is
    the most expensive field but also the most useful one when debugging
    a wrong answer ("show me what I actually asked"). For a personal
    bot the volume is tiny; for a multi-tenant service we'd redact /
    truncate here.
    """

    schema_version: int = SCHEMA_VERSION
    ts: datetime = Field(default_factory=_now_utc)
    trace_id: str = Field(default_factory=_new_trace_id)
    session_id: str | None = None

    provider: str
    model: str
    json_mode: bool = False
    schema_name: str | None = None

    messages: list[dict[str, str]]
    response: str

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: float

    outcome: Literal["ok", "error"] = "ok"
    error_type: str | None = None
    error_message: str | None = None


class ConversationTurn(BaseModel):
    """One user message and the bot's resolved response.

    ``extracted`` and ``action`` are deliberately ``dict[str, Any]``
    rather than typed shapes — schemas at this layer change as we add
    intents (log_expense, query_total, query_category, ...). Pydantic
    typing happens UPSTREAM at the extractor; by the time we're storing
    we just want a stable serialisable record.

    ``trace_ids`` ties this turn back to whichever
    :class:`LLMCallRecord`s it produced. One ``jq`` away from "show me
    every prompt the bot saw to answer this user message".
    """

    schema_version: int = SCHEMA_VERSION
    ts: datetime = Field(default_factory=_now_utc)
    session_id: str = Field(default_factory=_new_session_id)

    user_text: str
    intent: str | None = None
    extracted: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    bot_reply: str | None = None

    trace_ids: list[str] = Field(default_factory=list)


# ─── Storage protocol ───────────────────────────────────────────────────

@runtime_checkable
class ChatStore(Protocol):
    """Append-only storage for both record types.

    Concrete implementations MUST:

    * Make appends durable before returning (fsync or equivalent), so a
      crash mid-call doesn't lose the record we just wrote.
    * Be safe under concurrent writes from a single process — multiple
      threads inside the same bot instance must not corrupt the file.
    * Round-trip records exactly: a value written via ``append_*`` must
      be returned bit-identical by the matching ``iter_*``.

    Implementations are NOT required to be safe under concurrent writes
    across processes — the bot is single-tenant by design.
    """

    schema_version: int

    def append_llm_call(self, record: LLMCallRecord) -> None:
        """Persist one LLM round-trip."""
        ...

    def append_turn(self, turn: ConversationTurn) -> None:
        """Persist one user-bot turn."""
        ...

    def iter_llm_calls(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Iterator[LLMCallRecord]:
        """Yield records in write order, optionally clipped by timestamp."""
        ...

    def iter_turns(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Iterator[ConversationTurn]:
        """Yield turns in write order, optionally clipped by timestamp."""
        ...
