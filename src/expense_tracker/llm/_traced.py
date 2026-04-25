"""Transparent tracing wrapper for any :class:`LLMClient`.

Wraps an underlying provider client and records one
:class:`LLMCallRecord` per call to the configured
:class:`~expense_tracker.storage.ChatStore`. The wrapped client is
indistinguishable from the inner one to callers — same protocol, same
return values, same exceptions.

Why this is a decorator (not built into each client):

* Keeps the provider clients single-responsibility.
* Enables / disables tracing per environment with a single env var.
* Tests can drop the wrapper and inspect the inner client directly.

Caveat: tracing failures must NEVER break the user's request. If the
store fails (disk full, permission denied), we swallow that error after
emitting a warning — the user's chat still completes.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from ..storage import ChatStore, LLMCallRecord
from .base import LLMClient, LLMResponse, Message, T
from .exceptions import LLMError

_log = logging.getLogger(__name__)


class TracedLLMClient:
    """Decorator client that records every call to a :class:`ChatStore`.

    Implements :class:`~expense_tracker.llm.base.LLMClient` itself so
    callers can't tell it's not a real provider.
    """

    # ``provider_name`` and ``model`` mirror the inner client. We expose
    # them as instance attributes (Protocols accept either ClassVar or
    # instance attributes for these declarations).
    provider_name: ClassVar[str] = "traced"

    def __init__(
        self,
        *,
        inner: LLMClient,
        store: ChatStore,
        session_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._store = store
        self._session_id = session_id
        # Mirror inner identity so callers see the underlying provider.
        self.provider_name = inner.provider_name  # type: ignore[misc]
        self.model = inner.model

    @property
    def inner(self) -> LLMClient:
        """Escape hatch for tests / debug introspection."""
        return self._inner

    # ─── Protocol methods ────────────────────────────────────────────────
    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        try:
            resp = self._inner.complete(
                messages, temperature=temperature, max_tokens=max_tokens
            )
        except LLMError as exc:
            self._record_error(messages, exc, json_mode=False, schema_name=None)
            raise

        self._record_ok(messages, resp, json_mode=False, schema_name=None)
        return resp

    def complete_json(
        self,
        messages: list[Message],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[T, LLMResponse]:
        try:
            parsed, resp = self._inner.complete_json(
                messages,
                schema,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except LLMError as exc:
            self._record_error(
                messages, exc, json_mode=True, schema_name=schema.__name__
            )
            raise

        self._record_ok(messages, resp, json_mode=True, schema_name=schema.__name__)
        return parsed, resp

    # ─── Internal recording ──────────────────────────────────────────────
    def _record_ok(
        self,
        messages: list[Message],
        resp: LLMResponse,
        *,
        json_mode: bool,
        schema_name: str | None,
    ) -> None:
        rec = LLMCallRecord(
            # Use the response's request_id as the trace_id so callers
            # who hold an LLMResponse can correlate it back to the
            # persisted record without scanning the file.
            trace_id=resp.request_id,
            session_id=self._session_id,
            provider=resp.provider,
            model=resp.model,
            json_mode=json_mode,
            schema_name=schema_name,
            messages=[m.model_dump() for m in messages],
            response=resp.content,
            prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
            total_tokens=resp.total_tokens,
            latency_ms=resp.latency_ms,
            outcome="ok",
        )
        self._safe_append(rec)

    def with_session(self, session_id: str) -> TracedLLMClient:
        """Return a sibling tracer that stamps ``session_id`` on every record.

        Lets a caller (e.g. the extractor orchestrator) group all LLM
        calls produced by one user turn behind a single id, so
        ``iter_llm_calls`` filters cleanly and ConversationTurn rows can
        cross-link via their ``trace_ids`` list.
        """
        return TracedLLMClient(
            inner=self._inner, store=self._store, session_id=session_id
        )

    def _record_error(
        self,
        messages: list[Message],
        exc: LLMError,
        *,
        json_mode: bool,
        schema_name: str | None,
    ) -> None:
        rec = LLMCallRecord(
            session_id=self._session_id,
            provider=self._inner.provider_name,
            model=self._inner.model,
            json_mode=json_mode,
            schema_name=schema_name,
            messages=[m.model_dump() for m in messages],
            response="",
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            latency_ms=0.0,
            outcome="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        self._safe_append(rec)

    def _safe_append(self, rec: LLMCallRecord) -> None:
        """Persist a trace record without ever propagating store failures.

        The user's chat must keep working even if the disk is full or
        the log directory was removed. We log the failure once and move
        on — the LLMResponse the caller got back is unchanged.
        """
        try:
            self._store.append_llm_call(rec)
        except Exception:
            # Observability must never crash callers — swallow & warn.
            _log.warning(
                "Failed to persist LLM trace record (non-fatal).", exc_info=True
            )
