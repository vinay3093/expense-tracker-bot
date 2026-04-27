"""Telegram message handlers.

This is the seam between the Telegram SDK (``python-telegram-bot``) and
our existing :class:`~expense_tracker.pipeline.chat.ChatPipeline`.

Design goals
------------
* **Thin handlers** — each Telegram callback is a few lines that pull
  the text out of the update, hand it to a :class:`MessageProcessor`,
  and reply. All real work is in :class:`MessageProcessor`, which is
  pure Python and trivial to unit-test.
* **Async-friendly** — ``ChatPipeline.chat()`` is sync (and may take a
  couple of seconds while the LLM thinks), so we run it on a worker
  thread via :func:`asyncio.to_thread`. The bot stays responsive to
  other updates and Telegram's poll loop never blocks.
* **Fail-safe replies** — any unhandled exception in the pipeline is
  caught and surfaced as a generic apology, while the full traceback
  is logged. The user never sees a stack trace; we never silently drop
  messages.
* **Auth first** — every text update is gated through
  :class:`~.auth.Authorizer` before any LLM call. Unauthorized users
  get a one-shot reply telling them their numeric ID so the operator
  can add it to ``TELEGRAM_ALLOWED_USERS``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..pipeline.chat import ChatPipeline, ChatTurn
from .auth import AuthDecision, Authorizer

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

_log = logging.getLogger(__name__)


# Shown for /start and /help — kept identical so users don't have to
# remember which one to type. Plain text on purpose; markdown escape
# rules are surprisingly fiddly and a couple of lines don't need it.
_WELCOME_TEXT = (
    "Hi! I'm your expense tracker bot.\n\n"
    "Just tell me what you spent, in plain English:\n"
    "  • spent 40 on coffee today\n"
    "  • 1500 INR groceries yesterday at Costco\n"
    "  • bought a tesla supercharge for $12.50\n\n"
    "I'll log it into your Google Sheet and confirm. "
    "I can also chat back if you ask normal questions."
)


@dataclass(frozen=True)
class ProcessedMessage:
    """Result of processing one inbound text message.

    Carries everything the SDK-facing layer needs to send back without
    knowing anything about :class:`ChatTurn` internals — so tests can
    assert on shape without instantiating Telegram objects.
    """

    reply_text: str
    chat_turn: ChatTurn | None  # None for auth-rejected / non-text updates


class MessageProcessor:
    """Pure-Python orchestrator for one Telegram text turn.

    Holds references to the auth check + pipeline and exposes a single
    ``process()`` method. No Telegram SDK imports here — the handler
    layer (below) does the SDK glue.
    """

    def __init__(
        self,
        *,
        authorizer: Authorizer,
        pipeline: ChatPipeline,
    ) -> None:
        self._auth = authorizer
        self._pipeline = pipeline

    def process(self, *, user_id: int | None, text: str) -> ProcessedMessage:
        """Authorize, run the chat turn, and build a reply string."""
        decision = self._auth.check(user_id)
        if not decision.allowed:
            return ProcessedMessage(
                reply_text=_format_auth_denied(decision),
                chat_turn=None,
            )

        cleaned = text.strip()
        if not cleaned:
            return ProcessedMessage(
                reply_text="(empty message — nothing to log)",
                chat_turn=None,
            )

        try:
            turn = self._pipeline.chat(cleaned)
        except Exception:
            # Caught broadly on purpose: a Telegram handler MUST always
            # reply with something. Logging includes the trace so we can
            # debug from logs/.
            _log.exception("Chat pipeline blew up on text: %r", cleaned)
            return ProcessedMessage(
                reply_text=(
                    "Sorry — something went wrong on my side while "
                    "processing that. Please try again in a moment."
                ),
                chat_turn=None,
            )

        return ProcessedMessage(reply_text=turn.bot_reply, chat_turn=turn)


# ─── Telegram-SDK glue ──────────────────────────────────────────────────

# Below this line we touch the SDK. Kept small and at the bottom so the
# pure-logic part above stays trivial to import in tests that don't have
# (or don't want) python-telegram-bot installed.


async def _reply_safely(update: Update, text: str) -> None:
    """Reply to the message that triggered the update, if possible."""
    msg = update.effective_message
    if msg is None:  # pragma: no cover — defensive; shouldn't fire for text
        _log.warning("Update %s had no effective_message; cannot reply", update)
        return
    await msg.reply_text(text, disable_web_page_preview=True)


async def _send_typing(update: Update) -> None:
    """Best-effort 'typing…' indicator while the LLM thinks.

    Errors here are non-fatal — if Telegram refuses we just skip the
    indicator and continue with the real reply.
    """
    chat = update.effective_chat
    if chat is None:
        return
    try:
        await chat.send_action("typing")
    except Exception:  # pragma: no cover — purely cosmetic
        _log.debug("send_action(typing) failed", exc_info=True)


def make_text_handler(processor: MessageProcessor):
    """Build the ``async`` handler the Telegram Application expects.

    Returned closure has the ``async def(update, context)`` signature
    PTB requires. We keep the SDK contact surface to one function so
    unit tests can drive ``processor`` directly.
    """

    async def handle_text(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        # ``context`` is required by python-telegram-bot's signature,
        # but we don't use it — all state lives in the processor.
        del context
        msg = update.effective_message
        if msg is None or msg.text is None:
            return  # ignore non-text updates (stickers, edits, joins, …)

        user = update.effective_user
        user_id = user.id if user is not None else None

        await _send_typing(update)
        result = await asyncio.to_thread(
            processor.process,
            user_id=user_id,
            text=msg.text,
        )
        await _reply_safely(update, result.reply_text)

    return handle_text


def make_start_handler():
    """``/start`` and ``/help`` both reply with the welcome text."""

    async def handle_start(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context  # required by PTB signature, unused by us
        await _reply_safely(update, _WELCOME_TEXT)

    return handle_start


def make_whoami_handler():
    """``/whoami`` — reply with the caller's Telegram user ID.

    Useful while bootstrapping the allow-list. Works for everyone
    (allowed or not), since you literally need this command to add
    yourself.
    """

    async def handle_whoami(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context  # required by PTB signature, unused by us
        user = update.effective_user
        if user is None:
            await _reply_safely(update, "Couldn't determine your Telegram user ID.")
            return
        await _reply_safely(
            update,
            (
                f"Your Telegram user ID is `{user.id}`.\n"
                "Add it to TELEGRAM_ALLOWED_USERS in .env "
                "to log expenses with this bot."
            ),
        )

    return handle_whoami


def _format_auth_denied(decision: AuthDecision) -> str:
    """User-facing message for an auth-rejected update."""
    if decision.user_id is None:
        return (
            "I can only respond to direct messages from authorized users. "
            "If you're the owner, message me from your personal account."
        )
    return (
        "Sorry, you're not authorized to use this expense tracker.\n"
        f"Your Telegram user ID is `{decision.user_id}`. If this is your "
        "bot, add that ID to TELEGRAM_ALLOWED_USERS in .env and restart."
    )


__all__ = [
    "MessageProcessor",
    "ProcessedMessage",
    "make_start_handler",
    "make_text_handler",
    "make_whoami_handler",
]
