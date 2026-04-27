"""Async tests for the Telegram-SDK glue.

We don't talk to telegram.org. Instead we hand-craft minimal ``Update``
shapes (just the attributes our handlers touch) and a stub
:class:`MessageProcessor` so the tests stay fully offline yet still
exercise the real ``async def`` handlers from
``expense_tracker.telegram_app.bot``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from expense_tracker.telegram_app.auth import Authorizer
from expense_tracker.telegram_app.bot import (
    MessageProcessor,
    ProcessedMessage,
    make_start_handler,
    make_text_handler,
    make_whoami_handler,
)

# ─── Update / message fakes ───────────────────────────────────────────


def _make_update(*, text: str | None, user_id: int | None) -> SimpleNamespace:
    """Hand-rolled stand-in for ``telegram.Update``.

    ``reply_text`` and ``send_action`` are async mocks so we can assert
    on what the handler tried to send back. We don't touch the real
    ``telegram.Bot`` object at all.
    """
    reply_mock = AsyncMock(name="reply_text")
    action_mock = AsyncMock(name="send_action")

    message = SimpleNamespace(
        text=text,
        reply_text=reply_mock,
    )
    chat = SimpleNamespace(send_action=action_mock)
    user = None if user_id is None else SimpleNamespace(id=user_id)

    return SimpleNamespace(
        effective_message=message,
        effective_chat=chat,
        effective_user=user,
        _reply_mock=reply_mock,
        _action_mock=action_mock,
    )


class _StubProcessor:
    """Stand-in for :class:`MessageProcessor` with a recorded reply."""

    def __init__(self, reply: str = "echoed") -> None:
        self._reply = reply
        self.calls: list[tuple[int | None, str]] = []

    def process(self, *, user_id: int | None, text: str) -> ProcessedMessage:
        self.calls.append((user_id, text))
        return ProcessedMessage(reply_text=self._reply, chat_turn=None)


# ─── Text handler ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_handler_sends_typing_then_replies() -> None:
    """Authorized text → typing indicator + reply with processor's output."""
    processor = _StubProcessor(reply="logged it")
    handler = make_text_handler(processor)  # type: ignore[arg-type]
    update = _make_update(text="spent 40 on coffee", user_id=42)

    await handler(update, context=None)  # type: ignore[arg-type]

    update._action_mock.assert_awaited_once_with("typing")
    update._reply_mock.assert_awaited_once()
    args, kwargs = update._reply_mock.await_args
    assert args[0] == "logged it"
    assert kwargs.get("disable_web_page_preview") is True
    assert processor.calls == [(42, "spent 40 on coffee")]


@pytest.mark.asyncio
async def test_text_handler_ignores_non_text_updates() -> None:
    """Stickers / photos / system events → no processor call, no reply."""
    processor = _StubProcessor()
    handler = make_text_handler(processor)  # type: ignore[arg-type]
    update = _make_update(text=None, user_id=42)

    await handler(update, context=None)  # type: ignore[arg-type]

    assert processor.calls == []
    update._reply_mock.assert_not_awaited()
    update._action_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_handler_routes_user_id_correctly() -> None:
    """The handler must hand the caller's user_id to the processor so the
    auth gate can see it. Trivial-looking but easy to break in a refactor.
    """
    processor = _StubProcessor()
    handler = make_text_handler(processor)  # type: ignore[arg-type]
    update = _make_update(text="hi", user_id=12345)

    await handler(update, context=None)  # type: ignore[arg-type]

    assert processor.calls == [(12345, "hi")]


@pytest.mark.asyncio
async def test_text_handler_handles_no_user() -> None:
    """Channel posts / anonymous senders → user_id is None, processor decides."""
    processor = _StubProcessor(reply="not allowed")
    handler = make_text_handler(processor)  # type: ignore[arg-type]
    update = _make_update(text="hi", user_id=None)

    await handler(update, context=None)  # type: ignore[arg-type]

    assert processor.calls == [(None, "hi")]
    update._reply_mock.assert_awaited_once()


# ─── /start ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_handler_replies_with_welcome_text() -> None:
    handler = make_start_handler()
    update = _make_update(text="/start", user_id=42)

    await handler(update, context=None)  # type: ignore[arg-type]

    update._reply_mock.assert_awaited_once()
    sent = update._reply_mock.await_args.args[0]
    assert "expense tracker bot" in sent.lower()
    assert "spent 40 on coffee" in sent.lower()


# ─── /whoami ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whoami_replies_with_user_id() -> None:
    """Unauthenticated bootstrap helper — must work even with empty allow-list."""
    handler = make_whoami_handler()
    update = _make_update(text="/whoami", user_id=987654321)

    await handler(update, context=None)  # type: ignore[arg-type]

    update._reply_mock.assert_awaited_once()
    sent = update._reply_mock.await_args.args[0]
    assert "987654321" in sent
    assert "TELEGRAM_ALLOWED_USERS" in sent


@pytest.mark.asyncio
async def test_whoami_handles_missing_user() -> None:
    handler = make_whoami_handler()
    update = _make_update(text="/whoami", user_id=None)

    await handler(update, context=None)  # type: ignore[arg-type]

    update._reply_mock.assert_awaited_once()
    sent = update._reply_mock.await_args.args[0]
    assert "couldn't determine" in sent.lower()


# ─── Real MessageProcessor sanity ─────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_with_real_processor_denies_unauthorized() -> None:
    """End-to-end: real Authorizer + a tiny pipeline stub.

    Confirms the SDK glue + auth + processor wire together. We use a
    single-method stand-in for ChatPipeline rather than spinning up
    the full extractor stack.
    """

    class _NeverCalledPipeline:
        def chat(self, text):
            raise AssertionError("pipeline must not run for unauthorized users")

    processor = MessageProcessor(
        authorizer=Authorizer(frozenset({42})),
        pipeline=_NeverCalledPipeline(),  # type: ignore[arg-type]
    )
    handler = make_text_handler(processor)
    update = _make_update(text="spent 40 on coffee", user_id=7)

    await handler(update, context=None)  # type: ignore[arg-type]

    update._reply_mock.assert_awaited_once()
    sent = update._reply_mock.await_args.args[0]
    assert "not authorized" in sent.lower()
    assert "7" in sent


# ─── Application factory ──────────────────────────────────────────────


def test_build_application_registers_expected_handlers() -> None:
    """The wired :class:`Application` must have /start, /help, /whoami
    handlers + a text-message handler — and nothing else.
    """
    pytest.importorskip("telegram", reason="python-telegram-bot not installed")

    from telegram.ext import CommandHandler, MessageHandler

    from expense_tracker.config import Settings
    from expense_tracker.telegram_app.factory import build_application

    cfg = Settings(
        TELEGRAM_BOT_TOKEN="123:fake-test-token",  # type: ignore[arg-type]
        TELEGRAM_ALLOWED_USERS="42",
        # Avoid touching real services for the remaining wiring.
        LLM_PROVIDER="fake",
    )

    class _StubChatPipeline:
        # Mirror the real ChatPipeline contract just enough for
        # build_application: a .chat() method we never invoke and a
        # .corrector attribute the correction processor pulls.
        corrector = None

        def chat(self, text):
            raise AssertionError("not invoked in this test")

    app = build_application(cfg, pipeline=_StubChatPipeline())  # type: ignore[arg-type]

    handlers = [h for group in app.handlers.values() for h in group]
    command_names: set[str] = set()
    for h in handlers:
        if isinstance(h, CommandHandler) and h.commands:
            command_names.update(h.commands)
    assert command_names == {"start", "help", "whoami", "last", "undo", "edit"}

    message_handlers = [h for h in handlers if isinstance(h, MessageHandler)]
    assert len(message_handlers) == 1, (
        "expected exactly one MessageHandler (text), got "
        f"{len(message_handlers)}"
    )


def test_build_application_rejects_missing_token() -> None:
    """Missing TELEGRAM_BOT_TOKEN should be a clear, early error."""
    pytest.importorskip("telegram", reason="python-telegram-bot not installed")

    from expense_tracker.config import Settings
    from expense_tracker.telegram_app.factory import (
        TelegramConfigError,
        build_application,
    )

    cfg = Settings(TELEGRAM_BOT_TOKEN=None, TELEGRAM_ALLOWED_USERS="42")

    with pytest.raises(TelegramConfigError, match="TELEGRAM_BOT_TOKEN"):
        build_application(cfg)
