"""Tests for MessageProcessor — auth + pipeline orchestration in pure Python.

The Telegram SDK is *not* exercised here; we test the inner
:class:`MessageProcessor` directly with a stub pipeline. The SDK glue
(handlers, /start, /whoami) lives in ``test_telegram_handlers.py``.
"""

from __future__ import annotations

from datetime import date

import pytest

from expense_tracker.extractor.schemas import ExpenseEntry, ExtractionResult, Intent
from expense_tracker.pipeline.chat import ChatTurn
from expense_tracker.telegram_app.auth import Authorizer
from expense_tracker.telegram_app.bot import MessageProcessor

# ─── Test doubles ──────────────────────────────────────────────────────


class _StubPipeline:
    """Minimal :class:`ChatPipeline` substitute.

    We only need ``.chat(text) -> ChatTurn`` for the processor tests.
    The stub records calls so we can assert how many times the pipeline
    was hit (zero, for unauthorized users).
    """

    def __init__(self, *, reply: str = "ok!", raises: Exception | None = None) -> None:
        self._reply = reply
        self._raises = raises
        self.calls: list[str] = []

    def chat(self, text: str) -> ChatTurn:
        self.calls.append(text)
        if self._raises is not None:
            raise self._raises
        result = ExtractionResult(
            intent=Intent.SMALLTALK,
            confidence=0.9,
            reasoning="stub",
            user_text=text,
            session_id="sess-stub",
            trace_ids=["trace-stub"],
        )
        return ChatTurn(
            user_text=text,
            intent=Intent.SMALLTALK,
            extraction=result,
            log_result=None,
            log_error=None,
            retrieval_answer=None,
            retrieval_error=None,
            bot_reply=self._reply,
            session_id="sess-stub",
            trace_ids=["trace-stub"],
        )


def _processor(*, allowed_ids: set[int], reply: str = "ok!", raises=None) -> tuple[
    MessageProcessor, _StubPipeline
]:
    pipeline = _StubPipeline(reply=reply, raises=raises)
    auth = Authorizer(frozenset(allowed_ids))
    return MessageProcessor(authorizer=auth, pipeline=pipeline), pipeline


# ─── Auth gating ───────────────────────────────────────────────────────


def test_processor_rejects_unauthorized_user_without_calling_pipeline() -> None:
    """Unauthorized users must NOT trigger an LLM call (cost + safety)."""
    processor, pipeline = _processor(allowed_ids={42})

    out = processor.process(user_id=7, text="spent 40 on coffee")

    assert pipeline.calls == [], "pipeline should not be invoked for unauthorized user"
    assert out.chat_turn is None
    assert "not authorized" in out.reply_text.lower()
    assert "7" in out.reply_text, "denial should echo the caller's user id"


def test_processor_rejects_none_user_id() -> None:
    """Channel posts have no effective_user — must be denied gracefully."""
    processor, pipeline = _processor(allowed_ids={42})

    out = processor.process(user_id=None, text="hi")

    assert pipeline.calls == []
    assert out.chat_turn is None
    assert "authorized" in out.reply_text.lower()


def test_processor_rejects_everyone_when_allowlist_empty() -> None:
    """Empty allow-list = nobody allowed; even the bot owner has to set up."""
    processor, pipeline = _processor(allowed_ids=set())

    out = processor.process(user_id=42, text="hi")

    assert pipeline.calls == []
    assert "TELEGRAM_ALLOWED_USERS" in out.reply_text
    assert "42" in out.reply_text


# ─── Happy path ────────────────────────────────────────────────────────


def test_processor_routes_authorized_message_through_pipeline() -> None:
    processor, pipeline = _processor(
        allowed_ids={42},
        reply="Logged $40 on Food today.",
    )

    out = processor.process(user_id=42, text="spent 40 on food")

    assert pipeline.calls == ["spent 40 on food"]
    assert out.reply_text == "Logged $40 on Food today."
    assert out.chat_turn is not None
    assert out.chat_turn.intent is Intent.SMALLTALK


def test_processor_strips_whitespace_before_pipeline() -> None:
    """Telegram clients sometimes append trailing newlines on copy-paste."""
    processor, pipeline = _processor(allowed_ids={42}, reply="ok")

    processor.process(user_id=42, text="  spent 40  \n")

    assert pipeline.calls == ["spent 40"], "leading/trailing whitespace must be trimmed"


def test_processor_handles_blank_message_without_calling_pipeline() -> None:
    """Whitespace-only messages shouldn't waste an LLM round-trip."""
    processor, pipeline = _processor(allowed_ids={42}, reply="ok")

    out = processor.process(user_id=42, text="   ")

    assert pipeline.calls == []
    assert out.chat_turn is None
    assert "empty" in out.reply_text.lower()


# ─── Failure isolation ────────────────────────────────────────────────


def test_processor_recovers_from_pipeline_exception() -> None:
    """Any exception → friendly reply, never a stack trace to the user."""
    boom = RuntimeError("LLM timed out twice in a row")
    processor, pipeline = _processor(allowed_ids={42}, raises=boom)

    out = processor.process(user_id=42, text="spent 40 on coffee")

    assert pipeline.calls == ["spent 40 on coffee"]
    assert out.chat_turn is None
    assert "something went wrong" in out.reply_text.lower()
    assert "RuntimeError" not in out.reply_text, "internal error class must not leak"
    assert "LLM timed out" not in out.reply_text, "raw exception text must not leak"


# ─── Real-shape ChatTurn pass-through ─────────────────────────────────


def test_processor_propagates_chat_turn_for_log_expense() -> None:
    """When the pipeline produces a real log_expense ChatTurn, the processor
    surfaces ``chat_turn`` unchanged so callers can inspect/log it.
    """
    expense = ExpenseEntry(
        amount=12.5,
        currency="USD",
        category="Food",
        date=date(2026, 4, 26),
        note="coffee",
    )
    extraction = ExtractionResult(
        intent=Intent.LOG_EXPENSE,
        confidence=0.95,
        reasoning="clear log",
        user_text="spent 12.50 on coffee",
        session_id="sess-1",
        trace_ids=["t-classify", "t-extract"],
        expense=expense,
    )
    canned_turn = ChatTurn(
        user_text="spent 12.50 on coffee",
        intent=Intent.LOG_EXPENSE,
        extraction=extraction,
        log_result=None,
        log_error=None,
        retrieval_answer=None,
        retrieval_error=None,
        bot_reply="Logged $12.50 on Food.",
        session_id="sess-1",
        trace_ids=["t-classify", "t-extract"],
    )

    class _CannedPipeline:
        def chat(self, text: str) -> ChatTurn:
            return canned_turn

    processor = MessageProcessor(
        authorizer=Authorizer(frozenset({42})),
        pipeline=_CannedPipeline(),  # type: ignore[arg-type]
    )

    out = processor.process(user_id=42, text="spent 12.50 on coffee")

    assert out.reply_text == "Logged $12.50 on Food."
    assert out.chat_turn is canned_turn
    assert out.chat_turn.intent is Intent.LOG_EXPENSE


# ─── Sanity: the SDK-free pieces really are SDK-free ──────────────────


def test_message_processor_module_does_not_pull_telegram_at_import() -> None:
    """The processor + auth modules must be importable without the Telegram
    SDK installed. Asserting import-shape stops a future refactor from
    accidentally adding a top-level ``import telegram``.
    """
    import sys

    # We can't really uninstall the package mid-test, but we can at least
    # verify the auth/bot modules don't expose telegram symbols at module
    # scope — a regression test against accidental top-level imports.
    from expense_tracker.telegram_app import auth, bot

    auth_globals = {k for k in vars(auth) if not k.startswith("_")}
    bot_globals = {k for k in vars(bot) if not k.startswith("_")}

    # If somebody adds ``from telegram import Update`` at module level
    # (instead of TYPE_CHECKING), ``Update`` lands in module globals.
    forbidden = {"Update", "Bot", "Application", "ApplicationBuilder"}
    leaked = (auth_globals | bot_globals) & forbidden
    assert not leaked, f"Telegram SDK names leaked into module globals: {leaked}"
    # Reference ``sys`` so the import isn't ruff'd away.
    assert sys is not None


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, frozenset()),
        ("", frozenset()),
        ("42", frozenset({42})),
        ("1, 2 ,3", frozenset({1, 2, 3})),
    ],
)
def test_authorizer_constructed_from_parser_roundtrip(raw, expected) -> None:
    """Smoke test: parser + Authorizer compose cleanly."""
    from expense_tracker.telegram_app.auth import parse_allowed_users

    auth = Authorizer(parse_allowed_users(raw))
    assert auth.allowed_ids == expected
