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
from ..pipeline.correction import (
    CorrectionError,
    CorrectionLogger,
    EditResult,
    UndoResult,
)
from ..pipeline.retrieval import RetrievalError
from ..pipeline.summary import (
    Summary,
    SummaryEngine,
    SummaryScope,
    format_summary,
)
from ..sheets.transactions import LastRow
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
    "I'll log it into your Google Sheet and confirm.\n\n"
    "Ask me anything about your spending:\n"
    "  • how much did I spend in April?\n"
    "  • how much for food this month?\n"
    "  • what did I spend on 24 April?\n"
    "  • show last 5 transactions\n\n"
    "Fix the last entry:\n"
    "  /undo                  delete the last logged expense\n"
    "  /edit amount 50        change the amount to 50\n"
    "  /edit category Food    change the category (aliases OK)\n"
    "  /last                  show the last entry without changing it\n\n"
    "See how you're tracking:\n"
    "  /summary               last 7 days vs the 7 before that\n"
    "  /summary month         this month so far vs last month thru same day\n"
    "  /summary year          year-to-date vs last year thru same date"
)


# User-facing reply for /undo / /edit when the ledger is empty. Phrased
# the same way both commands hit so the muscle memory is "I'll just
# tell you nothing's there" rather than two slightly-different errors.
_EMPTY_LEDGER_REPLY = (
    "Nothing to fix — your Transactions tab is empty (or hasn't been "
    "created yet)."
)


# Generic "couldn't fix it" reply we surface when the underlying Sheets
# / FX layer raises. Stack traces stay in the logs; the user sees a
# short apology + a hint at what to try.
_FIX_FAILED_TEMPLATE = (
    "Sorry — couldn't apply that change.\n"
    "Reason: {reason}\n"
    "Try again in a moment, or run `expense --whoami` from the laptop "
    "to check the connection."
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


# ─── Correction processor (/last, /undo, /edit) ────────────────────────

# Same pattern as :class:`MessageProcessor` — pure-Python wrapper around
# :class:`CorrectionLogger`. Telegram-side handlers (below) just turn
# Telegram updates into method calls and stringify the result.


def _format_last_row_pretty(snap: LastRow) -> str:
    """Multi-line summary of a :class:`LastRow` for chat replies.

    Defensive: rows written under older schemas may have shorter
    ``values`` lists, in which case ``snap.value()`` returns ``None``
    and we just print "?" rather than crashing.
    """
    if snap.is_empty:
        return "(empty)"
    lines = [
        f"  Date     : {snap.value('date') or '?'}",
        f"  Day      : {snap.value('day') or '?'}",
        f"  Category : {snap.value('category') or '?'}",
        (
            f"  Amount   : {snap.value('amount') or '?'} "
            f"{snap.value('currency') or '?'}"
        ),
        f"  USD      : {snap.value('amount_usd') or '?'}",
    ]
    note = snap.value("note")
    if note:
        lines.append(f"  Note     : {note}")
    return "\n".join(lines)


class CorrectionProcessor:
    """Pure-Python orchestrator for /last, /undo, /edit Telegram turns.

    Constructed with an :class:`Authorizer` and an optional
    :class:`CorrectionLogger` (it's optional because tests + dev modes
    that don't need the corrector can still build a bot). When the
    logger is missing every command replies with a friendly "feature
    not configured" message instead of crashing.
    """

    def __init__(
        self,
        *,
        authorizer: Authorizer,
        corrector: CorrectionLogger | None,
    ) -> None:
        self._auth = authorizer
        self._corrector = corrector

    # ─── /last ────────────────────────────────────────────────────────

    def process_last(self, *, user_id: int | None) -> str:
        denied = self._maybe_deny(user_id)
        if denied is not None:
            return denied
        if self._corrector is None:
            return self._unconfigured_reply()
        try:
            snap = self._corrector.peek_last()
        except CorrectionError as exc:
            _log.warning("/last failed: %s", exc)
            return _FIX_FAILED_TEMPLATE.format(reason=str(exc))
        if snap.is_empty:
            return _EMPTY_LEDGER_REPLY
        return f"Last logged expense:\n{_format_last_row_pretty(snap)}"

    # ─── /undo ────────────────────────────────────────────────────────

    def process_undo(self, *, user_id: int | None) -> str:
        denied = self._maybe_deny(user_id)
        if denied is not None:
            return denied
        if self._corrector is None:
            return self._unconfigured_reply()
        try:
            result = self._corrector.undo()
        except CorrectionError as exc:
            _log.warning("/undo failed: %s", exc)
            return _FIX_FAILED_TEMPLATE.format(reason=str(exc))
        if result.deleted_row.is_empty:
            return _EMPTY_LEDGER_REPLY
        return self._format_undo_reply(result)

    # ─── /edit ────────────────────────────────────────────────────────

    def process_edit(self, *, user_id: int | None, args_text: str) -> str:
        denied = self._maybe_deny(user_id)
        if denied is not None:
            return denied
        if self._corrector is None:
            return self._unconfigured_reply()

        parsed = _parse_edit_args(args_text)
        if isinstance(parsed, str):
            return parsed  # parse error message
        amount, category = parsed

        try:
            result = self._corrector.edit(amount=amount, category=category)
        except CorrectionError as exc:
            _log.warning("/edit failed: %s", exc)
            return _FIX_FAILED_TEMPLATE.format(reason=str(exc))
        if result.before.is_empty:
            return _EMPTY_LEDGER_REPLY
        return self._format_edit_reply(result)

    # ─── Helpers ──────────────────────────────────────────────────────

    def _maybe_deny(self, user_id: int | None) -> str | None:
        decision = self._auth.check(user_id)
        if not decision.allowed:
            return _format_auth_denied(decision)
        return None

    @staticmethod
    def _unconfigured_reply() -> str:
        # Belt-and-braces: users should never hit this path because
        # build_application() always wires a corrector, but keep the
        # message friendly in case someone constructs a processor
        # directly in tests.
        return (
            "Correction commands aren't configured for this bot. "
            "Re-run `expense --telegram` from the laptop."
        )

    @staticmethod
    def _format_undo_reply(result: UndoResult) -> str:
        deleted = result.deleted_row
        date_v = deleted.value("date") or "?"
        cat_v = deleted.value("category") or "?"
        amt_v = deleted.value("amount") or "?"
        cur_v = deleted.value("currency") or "?"
        lines = [
            "Deleted last expense:",
            f"  {date_v} | {cat_v} | {amt_v} {cur_v}",
        ]
        if result.monthly_tab and result.monthly_tab_recomputed:
            lines.append(f"Refreshed `{result.monthly_tab}` so totals stay in sync.")
        return "\n".join(lines)

    @staticmethod
    def _format_edit_reply(result: EditResult) -> str:
        before = result.before
        applied = result.applied
        lines = ["Updated last expense:"]
        if "amount" in applied:
            lines.append(
                f"  Amount   : {before.value('amount') or '?'} "
                f"{before.value('currency') or '?'} -> "
                f"{applied['amount']} "
                f"{before.value('currency') or '?'}"
            )
            usd = applied.get("amount_usd")
            if usd is not None:
                lines.append(f"  USD      : -> ${usd:.2f}")
        if "category" in applied:
            lines.append(
                f"  Category : {before.value('category') or '?'} -> "
                f"{applied['category']}"
            )
        if result.monthly_tab and result.monthly_tab_recomputed:
            lines.append(f"Refreshed `{result.monthly_tab}` so totals stay in sync.")
        return "\n".join(lines)


def _parse_edit_args(args_text: str) -> tuple[float | None, str | None] | str:
    """Parse a ``/edit ...`` payload into ``(amount, category)``.

    Returns either the parsed pair or a user-facing error string. Two
    forms supported, matching what feels natural to type on a phone::

        /edit amount 50
        /edit amount 50 INR
        /edit category Groceries
        /edit category India Expense

    Note: currency in ``amount`` form is currently a parse-friendly
    stub — we always reuse the row's original currency, since editing
    the currency too is rare and ambiguous (did you mean to convert?).
    Telling the user about that intent in plain English keeps the
    surface tiny without surprising them.
    """
    cleaned = args_text.strip()
    if not cleaned:
        return _EDIT_USAGE_REPLY

    parts = cleaned.split(maxsplit=1)
    head = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if head == "amount":
        if not rest:
            return "Usage: /edit amount 50  (positive number)"
        # First whitespace-separated token is the number; anything
        # after is currently ignored with a heads-up below.
        num_str = rest.split()[0]
        try:
            amount = float(num_str)
        except ValueError:
            return f"Couldn't parse `{num_str}` as a number."
        if amount <= 0:
            return f"Amount must be positive — got {amount}."
        return amount, None

    if head == "category":
        if not rest:
            return "Usage: /edit category Groceries"
        return None, rest

    return _EDIT_USAGE_REPLY


_EDIT_USAGE_REPLY = (
    "Usage:\n"
    "  /edit amount 50\n"
    "  /edit category Groceries"
)


# ─── Summary processor (/summary [week|month|year]) ─────────────────────

# Mirrors the :class:`MessageProcessor` / :class:`CorrectionProcessor`
# pattern: pure-Python wrapper that takes raw Telegram args and returns
# a string. The Telegram-SDK glue below stays trivially testable.

_SUMMARY_USAGE_REPLY = (
    "Usage:\n"
    "  /summary             default — last 7 days vs the 7 before\n"
    "  /summary week        same as default\n"
    "  /summary month       this month so far vs last month thru same day\n"
    "  /summary year        year-to-date vs same period last year"
)

_SUMMARY_FAILED_TEMPLATE = (
    "Sorry — couldn't read the ledger to build that summary.\n"
    "Reason: {reason}\n"
    "Try again in a moment, or run `expense --whoami` from the laptop "
    "to check the connection."
)


class SummaryProcessor:
    """Pure-Python orchestrator for /summary Telegram turns.

    Constructed with an :class:`Authorizer` and an optional
    :class:`SummaryEngine` (optional so tests / dev modes that don't
    wire the engine still get a friendly "feature not configured"
    reply rather than a crash).
    """

    def __init__(
        self,
        *,
        authorizer: Authorizer,
        engine: SummaryEngine | None,
    ) -> None:
        self._auth = authorizer
        self._engine = engine

    def process(self, *, user_id: int | None, args_text: str) -> str:
        denied = self._maybe_deny(user_id)
        if denied is not None:
            return denied
        if self._engine is None:
            return self._unconfigured_reply()

        scope_or_err = _parse_summary_args(args_text)
        # Order matters: ``SummaryScope`` extends ``str`` so the
        # ``isinstance(..., str)`` form would match the success case
        # too. Check the enum first.
        if not isinstance(scope_or_err, SummaryScope):
            return scope_or_err  # parse error string
        scope = scope_or_err

        try:
            summary = self._engine.summarize(scope)
        except RetrievalError as exc:
            _log.warning("/summary failed: %s", exc)
            return _SUMMARY_FAILED_TEMPLATE.format(reason=str(exc))

        return _format_summary_for_telegram(summary)

    def _maybe_deny(self, user_id: int | None) -> str | None:
        decision = self._auth.check(user_id)
        if not decision.allowed:
            return _format_auth_denied(decision)
        return None

    @staticmethod
    def _unconfigured_reply() -> str:
        return (
            "Summaries aren't configured for this bot. "
            "Re-run `expense --telegram` from the laptop."
        )


def _parse_summary_args(args_text: str) -> SummaryScope | str:
    """Parse ``/summary ...`` payload into a :class:`SummaryScope`.

    Empty payload defaults to ``WEEK`` (the most useful weekly check-in).
    Returns a usage string for unknown scope words so the user learns
    the surface without needing to read the help.
    """
    cleaned = args_text.strip().lower()
    if not cleaned:
        return SummaryScope.WEEK
    head = cleaned.split()[0]
    if head in ("week", "weekly", "w", "7d"):
        return SummaryScope.WEEK
    if head in ("month", "monthly", "m"):
        return SummaryScope.MONTH
    if head in ("year", "yearly", "ytd", "y"):
        return SummaryScope.YEAR
    return f"Couldn't parse `{head}` as a scope.\n\n{_SUMMARY_USAGE_REPLY}"


def _format_summary_for_telegram(summary: Summary) -> str:
    """Two-line phone-friendly summary built atop :func:`format_summary`.

    The ``compact=True`` form fits on one screen; we preserve the
    multi-line CLI form for the laptop. Same data either way.
    """
    return format_summary(summary, compact=True)


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


def make_last_handler(processor: CorrectionProcessor):
    """``/last`` — show the bottom-most Transactions row, no changes."""

    async def handle_last(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        user = update.effective_user
        user_id = user.id if user is not None else None
        await _send_typing(update)
        reply = await asyncio.to_thread(processor.process_last, user_id=user_id)
        await _reply_safely(update, reply)

    return handle_last


def make_undo_handler(processor: CorrectionProcessor):
    """``/undo`` — delete the bottom-most Transactions row."""

    async def handle_undo(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        user = update.effective_user
        user_id = user.id if user is not None else None
        await _send_typing(update)
        reply = await asyncio.to_thread(processor.process_undo, user_id=user_id)
        await _reply_safely(update, reply)

    return handle_undo


def make_edit_handler(processor: CorrectionProcessor):
    """``/edit ...`` — patch the bottom-most Transactions row.

    Args after the command get parsed by
    :func:`_parse_edit_args`; usage replies live there.
    """

    async def handle_edit(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        user = update.effective_user
        user_id = user.id if user is not None else None
        msg = update.effective_message
        if msg is None or msg.text is None:
            return
        # Strip the leading "/edit" (and optional "@bot_username").
        # Whatever remains is the args payload the processor parses.
        text = msg.text.strip()
        space_idx = text.find(" ")
        args_text = text[space_idx + 1 :] if space_idx >= 0 else ""

        await _send_typing(update)
        reply = await asyncio.to_thread(
            processor.process_edit,
            user_id=user_id,
            args_text=args_text,
        )
        await _reply_safely(update, reply)

    return handle_edit


def make_summary_handler(processor: SummaryProcessor):
    """``/summary [week|month|year]`` — period rollup with comparison.

    Same args-parsing dance as :func:`make_edit_handler`: strip the
    leading ``/summary`` (and optional ``@bot_username``) before
    handing the rest to the processor.
    """

    async def handle_summary(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        user = update.effective_user
        user_id = user.id if user is not None else None
        msg = update.effective_message
        if msg is None or msg.text is None:
            return
        text = msg.text.strip()
        space_idx = text.find(" ")
        args_text = text[space_idx + 1 :] if space_idx >= 0 else ""

        await _send_typing(update)
        reply = await asyncio.to_thread(
            processor.process,
            user_id=user_id,
            args_text=args_text,
        )
        await _reply_safely(update, reply)

    return handle_summary


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
    "CorrectionProcessor",
    "MessageProcessor",
    "ProcessedMessage",
    "SummaryProcessor",
    "make_edit_handler",
    "make_last_handler",
    "make_start_handler",
    "make_summary_handler",
    "make_text_handler",
    "make_undo_handler",
    "make_whoami_handler",
]
