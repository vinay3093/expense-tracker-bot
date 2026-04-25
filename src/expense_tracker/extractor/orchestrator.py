"""High-level extractor: chat text → :class:`ExtractionResult`.

Glues the three components from this package:

1. :class:`IntentClassifier`       — what does the user want?
2. :class:`ExpenseExtractor`       — for ``LOG_EXPENSE``
3. :class:`RetrievalExtractor`     — for the four query intents

…and writes one :class:`~expense_tracker.storage.ConversationTurn` per
call to the chat store, cross-linked to LLM call traces by
``session_id``.

This is the layer the chat-bot router (Step 5) calls into.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from ..llm import LLMBadResponseError, LLMClient, get_llm_client
from ..llm._traced import TracedLLMClient
from ..storage import ChatStore, ConversationTurn, get_chat_store
from .categories import CategoryRegistry, get_registry
from .expense_extractor import ExpenseExtractor
from .intent_classifier import IntentClassifier
from .retrieval_extractor import RetrievalExtractor
from .schemas import ExtractionResult, Intent, is_query_intent

_log = logging.getLogger(__name__)


def _new_session_id() -> str:
    return f"x_{uuid.uuid4().hex[:10]}"


class Orchestrator:
    """Run the full extractor pipeline for one user turn."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        store: ChatStore,
        registry: CategoryRegistry,
        timezone: str,
        default_currency: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._llm = llm
        self._store = store
        self._registry = registry
        self._tz = ZoneInfo(timezone)
        self._default_currency = default_currency.upper()
        self._now = now or (lambda: datetime.now(tz=self._tz))

        self._intent_clf = IntentClassifier(llm=llm)
        self._expense_ext = ExpenseExtractor(
            llm=llm, registry=registry, default_currency=self._default_currency
        )
        self._retrieval_ext = RetrievalExtractor(llm=llm, registry=registry)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Orchestrator:
        """Convenience constructor — wires defaults from :mod:`config`."""
        cfg = settings or get_settings()
        return cls(
            llm=get_llm_client(cfg),
            store=get_chat_store(cfg),
            registry=get_registry(),
            timezone=cfg.TIMEZONE,
            default_currency=cfg.DEFAULT_CURRENCY,
        )

    # ─── Public API ─────────────────────────────────────────────────────
    def extract(self, user_text: str) -> ExtractionResult:
        """Classify *user_text* and run the matching stage-2 extractor."""
        if not user_text or not user_text.strip():
            return self._unactionable(
                user_text=user_text,
                intent=Intent.UNCLEAR,
                confidence=1.0,
                reasoning="Empty message.",
                session_id=_new_session_id(),
                trace_ids=[],
            )

        session_id = _new_session_id()
        scoped_llm = self._scope_llm(session_id)
        # Rebuild stage objects with the scoped client so every record
        # this turn produces inherits the same session_id.
        intent_clf = IntentClassifier(llm=scoped_llm)
        expense_ext = ExpenseExtractor(
            llm=scoped_llm,
            registry=self._registry,
            default_currency=self._default_currency,
        )
        retrieval_ext = RetrievalExtractor(llm=scoped_llm, registry=self._registry)

        today = self._today()
        trace_ids: list[str] = []

        # Stage 1
        try:
            stage1 = intent_clf.classify(user_text)
        except LLMBadResponseError as exc:
            _log.warning("Intent classification failed: %s", exc)
            return self._fail(
                user_text=user_text,
                session_id=session_id,
                trace_ids=trace_ids,
                error=f"Intent classification failed: {exc}",
            )
        trace_ids.append(stage1.response.request_id)
        cls_ = stage1.classification

        # Smalltalk / unclear short-circuit — no stage 2 call.
        if cls_.intent in (Intent.SMALLTALK, Intent.UNCLEAR):
            return self._finish(
                ExtractionResult(
                    intent=cls_.intent,
                    confidence=cls_.confidence,
                    reasoning=cls_.reasoning,
                    user_text=user_text,
                    session_id=session_id,
                    trace_ids=trace_ids,
                )
            )

        # Stage 2 — dispatch on intent
        try:
            if cls_.intent == Intent.LOG_EXPENSE:
                stage2 = expense_ext.extract(user_text, today=today)
                trace_ids.append(stage2.response.request_id)
                return self._finish(
                    ExtractionResult(
                        intent=cls_.intent,
                        confidence=cls_.confidence,
                        reasoning=cls_.reasoning,
                        user_text=user_text,
                        expense=stage2.expense,
                        session_id=session_id,
                        trace_ids=trace_ids,
                    )
                )

            if is_query_intent(cls_.intent):
                stage2_q = retrieval_ext.extract(
                    user_text, intent=cls_.intent, today=today
                )
                trace_ids.append(stage2_q.response.request_id)
                return self._finish(
                    ExtractionResult(
                        intent=cls_.intent,
                        confidence=cls_.confidence,
                        reasoning=cls_.reasoning,
                        user_text=user_text,
                        query=stage2_q.query,
                        session_id=session_id,
                        trace_ids=trace_ids,
                    )
                )

        except LLMBadResponseError as exc:
            _log.warning("Stage-2 extraction failed: %s", exc)
            return self._finish(
                ExtractionResult(
                    intent=cls_.intent,
                    confidence=cls_.confidence,
                    reasoning=cls_.reasoning,
                    user_text=user_text,
                    session_id=session_id,
                    trace_ids=trace_ids,
                    error=f"Stage-2 extraction failed: {exc}",
                )
            )

        # Unreachable: classifier returned an intent we don't handle.
        return self._finish(  # pragma: no cover
            ExtractionResult(
                intent=Intent.UNCLEAR,
                confidence=cls_.confidence,
                reasoning=f"Unhandled intent: {cls_.intent}",
                user_text=user_text,
                session_id=session_id,
                trace_ids=trace_ids,
            )
        )

    # ─── Helpers ────────────────────────────────────────────────────────
    def _today(self):
        return self._now().date()

    def _scope_llm(self, session_id: str) -> LLMClient:
        """Stamp this session_id on every LLM trace from this turn.

        Only ``TracedLLMClient`` knows what to do with a session_id; if
        the user disabled tracing (``LLM_TRACE=false``) the client is
        the raw provider and we just use it as-is.
        """
        if isinstance(self._llm, TracedLLMClient):
            return self._llm.with_session(session_id)
        return self._llm

    def _finish(self, result: ExtractionResult) -> ExtractionResult:
        """Persist the conversation turn and return *result*."""
        self._safe_append_turn(result)
        return result

    def _safe_append_turn(self, result: ExtractionResult) -> None:
        try:
            turn = ConversationTurn(
                session_id=result.session_id or _new_session_id(),
                user_text=result.user_text,
                intent=result.intent.value,
                extracted=result.to_turn_payload() if result.is_actionable() else None,
                action=None,  # filled by Step 5+ router
                bot_reply=None,
                trace_ids=list(result.trace_ids),
            )
            self._store.append_turn(turn)
        except Exception:
            # Persistence must never break extraction's return value.
            _log.warning(
                "Failed to persist ConversationTurn (non-fatal).", exc_info=True
            )

    def _unactionable(
        self,
        *,
        user_text: str,
        intent: Intent,
        confidence: float,
        reasoning: str,
        session_id: str,
        trace_ids: list[str],
    ) -> ExtractionResult:
        return self._finish(
            ExtractionResult(
                intent=intent,
                confidence=confidence,
                reasoning=reasoning,
                user_text=user_text,
                session_id=session_id,
                trace_ids=trace_ids,
            )
        )

    def _fail(
        self,
        *,
        user_text: str,
        session_id: str,
        trace_ids: list[str],
        error: str,
    ) -> ExtractionResult:
        return self._finish(
            ExtractionResult(
                intent=Intent.UNCLEAR,
                confidence=0.0,
                reasoning="extractor failure — see error",
                user_text=user_text,
                session_id=session_id,
                trace_ids=trace_ids,
                error=error,
            )
        )
