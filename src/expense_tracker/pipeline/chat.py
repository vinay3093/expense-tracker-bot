"""End-to-end chat turn: text in, action + reply out, one stored turn.

This is the layer the CLI's ``--chat`` and the Telegram bot (Step 7)
call into. It composes four pieces:

1. :class:`Orchestrator` — classifies + extracts (Step 3).
2. :class:`ExpenseLogger` — writes to Sheets when intent is
   ``log_expense`` (Step 5).
3. :class:`RetrievalEngine` — reads the ledger when intent is any
   ``query_*`` (Step 6, this module's wiring).
4. :func:`format_reply` — builds the user-facing reply string.

Persistence model
-----------------
We tell the orchestrator ``persist=False`` so it does NOT write a stage-1
turn to ``logs/conversations.jsonl``. Once we know the action outcome
and the reply, we call :meth:`Orchestrator.persist_turn` ourselves with
the *complete* shape — one user message yields one ConversationTurn row.

LLM call traces (``logs/llm_calls.jsonl``) are still written by
:class:`TracedLLMClient` inside extraction; those are 1-to-many to a
turn and stay independent.

Failure isolation
-----------------
* Sheets / FX errors during ``log_expense`` are caught here, attached to
  the persisted ``action`` dict, and surfaced as a friendly reply. The
  user sees an explanation, never a stack trace.
* Sheets read failures during retrieval are caught the same way and
  produce a graceful "couldn't read ledger" reply.
* Persistence errors are swallowed by ``Orchestrator.persist_turn``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..extractor.orchestrator import Orchestrator
from ..extractor.schemas import ExtractionResult, Intent, is_query_intent
from .correction import CorrectionLogger
from .exceptions import ExpenseLogError, InconsistentExtractionError, PipelineError
from .logger import ExpenseLogger, LogResult
from .reply import format_reply
from .retrieval import RetrievalAnswer, RetrievalEngine, RetrievalError

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatTurn:
    """Public result returned to the chat front-end.

    Carries everything a UI might want: the original text, the typed
    extraction, the typed log / retrieval result (when applicable), the
    rendered reply, and the trace IDs so the UI can deep-link into
    ``logs/llm_calls.jsonl`` for debugging.
    """

    user_text: str
    intent: Intent
    extraction: ExtractionResult
    log_result: LogResult | None
    log_error: ExpenseLogError | None
    retrieval_answer: RetrievalAnswer | None
    retrieval_error: RetrievalError | None
    bot_reply: str
    session_id: str | None
    trace_ids: list[str]

    @property
    def ok(self) -> bool:
        """True when the turn produced no error.

        SMALLTALK / UNCLEAR are still ``ok`` — they're valid outcomes,
        just not actionable. The flag flips False only when an error
        was raised by the extractor, the log path, or the retrieval
        path.
        """
        return (
            self.log_error is None
            and self.retrieval_error is None
            and self.extraction.error is None
        )


class ChatPipeline:
    """Orchestrate one full user turn.

    Construct via :func:`get_chat_pipeline` (the factory) for normal use,
    or directly here for tests that want to inject fakes.
    """

    def __init__(
        self,
        *,
        orchestrator: Orchestrator,
        expense_logger: ExpenseLogger,
        retrieval_engine: RetrievalEngine | None = None,
        correction_logger: CorrectionLogger | None = None,
    ) -> None:
        self._orch = orchestrator
        self._logger = expense_logger
        self._retriever = retrieval_engine
        self._corrector = correction_logger

    @property
    def corrector(self) -> CorrectionLogger | None:
        """Optional :class:`CorrectionLogger` for ``/undo`` / ``/edit``.

        ``None`` only when the pipeline was constructed without one
        (older tests). Production code paths always wire one in via
        :func:`get_chat_pipeline`.
        """
        return self._corrector

    @property
    def retriever(self) -> RetrievalEngine | None:
        """Optional :class:`RetrievalEngine` for ``query_*`` intents.

        ``None`` only when the pipeline was constructed without one
        (older tests). Production code paths always wire one in via
        :func:`get_chat_pipeline`.
        """
        return self._retriever

    def chat(self, user_text: str) -> ChatTurn:
        """Run one turn end-to-end and return a :class:`ChatTurn`."""
        result = self._orch.extract(user_text, persist=False)

        log_result: LogResult | None = None
        log_error: ExpenseLogError | None = None
        retrieval_answer: RetrievalAnswer | None = None
        retrieval_error: RetrievalError | None = None
        action: dict[str, Any] | None = None

        if result.intent == Intent.LOG_EXPENSE:
            log_result, log_error = self._maybe_log(result)
            if log_result is not None:
                action = log_result.to_action_dict()
            elif log_error is not None:
                action = {
                    "type": "sheets_append",
                    "status": "error",
                    "error": str(log_error),
                    "error_type": type(log_error).__name__,
                }

        elif is_query_intent(result.intent):
            retrieval_answer, retrieval_error = self._maybe_retrieve(result)
            if retrieval_answer is not None:
                action = retrieval_answer.to_action_dict()
            elif retrieval_error is not None:
                action = {
                    "type": "sheets_query",
                    "status": "error",
                    "error": str(retrieval_error),
                    "error_type": type(retrieval_error).__name__,
                }

        bot_reply = format_reply(
            result,
            log_result=log_result,
            log_error=log_error,
            retrieval_answer=retrieval_answer,
            retrieval_error=retrieval_error,
        )

        # One complete turn, written exactly once.
        self._orch.persist_turn(result, action=action, bot_reply=bot_reply)

        return ChatTurn(
            user_text=user_text,
            intent=result.intent,
            extraction=result,
            log_result=log_result,
            log_error=log_error,
            retrieval_answer=retrieval_answer,
            retrieval_error=retrieval_error,
            bot_reply=bot_reply,
            session_id=result.session_id,
            trace_ids=list(result.trace_ids),
        )

    # ─── Helpers ────────────────────────────────────────────────────────

    def _maybe_log(
        self, result: ExtractionResult
    ) -> tuple[LogResult | None, ExpenseLogError | None]:
        """Try to write the extracted expense to Sheets.

        Returns ``(log_result, log_error)`` — at most one is non-None.
        Both are None when the extractor classified ``log_expense`` but
        produced no payload (rare; handled as ``InconsistentExtraction``
        and surfaced through ``log_error``).
        """
        if result.expense is None:
            err = InconsistentExtractionError(
                "stage-1 said log_expense but stage-2 returned no ExpenseEntry; "
                "likely a malformed LLM response. Treating as unclear."
            )
            log_err = ExpenseLogError(str(err), cause=err)
            _log.warning("Inconsistent extraction: %s", err)
            return None, log_err

        # Pick the most recent trace id (stage-2's call) so the row is
        # auditable straight back to the prompt that produced it.
        trace_id = result.trace_ids[-1] if result.trace_ids else None

        try:
            return self._logger.log(result.expense, trace_id=trace_id), None
        except ExpenseLogError as exc:
            _log.warning("Sheet write failed: %s", exc)
            return None, exc
        except PipelineError as exc:
            _log.warning("Pipeline error: %s", exc)
            return None, ExpenseLogError(str(exc), cause=exc)

    def _maybe_retrieve(
        self, result: ExtractionResult,
    ) -> tuple[RetrievalAnswer | None, RetrievalError | None]:
        """Run a retrieval query against the ledger.

        Returns ``(answer, error)`` — at most one is non-None.

        * ``(None, None)`` when no engine is wired in (older tests) or
          the extractor produced no query payload — the reply layer
          falls back to the "unanswered" message.
        * Any :class:`RetrievalError` from the engine flows through as
          ``error`` so the chat reply can frame it gracefully.
        """
        if self._retriever is None or result.query is None:
            return None, None
        try:
            return self._retriever.answer(result.query), None
        except RetrievalError as exc:
            _log.warning("Retrieval failed: %s", exc)
            return None, exc
        except PipelineError as exc:
            _log.warning("Pipeline error during retrieval: %s", exc)
            return None, RetrievalError(str(exc), cause=exc)


__all__ = [
    "ChatPipeline",
    "ChatTurn",
]
