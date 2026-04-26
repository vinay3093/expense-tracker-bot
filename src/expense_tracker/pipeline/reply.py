"""Compose the user-facing chat reply for one pipeline turn.

Goal: short, deterministic, never-empty replies. The bot always says
*something* — never a silent success, never a stack trace.

Output shape is plain text, no markdown / no emoji, suitable for both
the CLI (where a one-liner is ideal) and Telegram (Step 7).

The function is pure: same inputs ⇒ same string. Tests pin every branch.
"""

from __future__ import annotations

from ..extractor.schemas import ExtractionResult, Intent
from .exceptions import ExpenseLogError
from .logger import LogResult

# ─── Public API ─────────────────────────────────────────────────────────

def format_reply(
    result: ExtractionResult,
    *,
    log_result: LogResult | None = None,
    log_error: ExpenseLogError | None = None,
) -> str:
    """Return the bot's user-facing reply for one extraction outcome.

    Args:
        result:     what the extractor produced.
        log_result: present iff a log_expense entry was successfully
                    written to Sheets.
        log_error:  present iff the chat pipeline tried to log an
                    expense but the Sheets / FX layer failed.
    """
    if result.error:
        return _reply_extractor_error(result.error)

    if log_error is not None:
        return _reply_log_failed(result, log_error)

    intent = result.intent

    if intent == Intent.LOG_EXPENSE:
        if log_result is None:
            # log_expense was classified but no row was written and no
            # error was reported — usually means the extractor produced
            # no payload (extraction failed silently). Treat as unclear.
            return _reply_unclear()
        return _reply_logged(result, log_result)

    if intent == Intent.SMALLTALK:
        return _reply_smalltalk()

    if intent == Intent.UNCLEAR:
        return _reply_unclear()

    if result.query is not None:
        return _reply_retrieval_stub(result, result.query)

    # Defensive default — shouldn't happen with the current intent set.
    return _reply_unclear()  # pragma: no cover


# ─── Per-branch builders ────────────────────────────────────────────────

def _reply_logged(result: ExtractionResult, log_result: LogResult) -> str:
    entry = result.expense
    row = log_result.row

    # "$40.00" or "499 INR -> $5.99 USD" if conversion happened.
    if row.currency == "USD" and abs(row.amount - row.amount_usd) < 1e-9:
        amount_str = f"${row.amount_usd:,.2f}"
    else:
        amount_str = (
            f"{row.amount:,.2f} {row.currency} -> ${row.amount_usd:,.2f} USD"
            f" (rate {row.fx_rate:.4f}, {log_result.fx_source})"
        )

    date_str = row.date.strftime("%a %d %b %Y")
    line = f"Logged {amount_str} to {row.category} on {date_str}"

    extras: list[str] = []
    note = (entry.note if entry is not None else None) or row.note
    if note:
        extras.append(f"note: {note}")
    vendor = (entry.vendor if entry is not None else None) or row.vendor
    if vendor:
        extras.append(f"vendor: {vendor}")
    if extras:
        line += " (" + ", ".join(extras) + ")"

    line += f". Tab: {log_result.monthly_tab}"
    if log_result.monthly_tab_created:
        line += " (newly created)"
    line += "."
    return line


def _reply_log_failed(result: ExtractionResult, error: ExpenseLogError) -> str:
    parts = [
        "I understood the expense but couldn't write it to the sheet.",
        f"Reason: {error}",
    ]
    if result.expense is not None:
        e = result.expense
        parts.append(
            f"Details: {e.amount} {e.currency} on {e.date.isoformat()} "
            f"as {e.category}."
        )
    parts.append("Try again in a moment, or run --whoami to check the connection.")
    return " ".join(parts)


def _reply_extractor_error(message: str) -> str:
    return (
        "Sorry, I couldn't parse that. "
        "Try something like: 'spent 40 on coffee yesterday' or "
        "'paid 146 to apartment for digital charge'. "
        f"(detail: {message})"
    )


def _reply_unclear() -> str:
    return (
        "I didn't catch that. Try: 'spent 40 on coffee yesterday', "
        "'paid 499 RS for netflix', or 'how much for food in April?'."
    )


def _reply_smalltalk() -> str:
    return "You're welcome - say 'spent X on Y' anytime to log an expense."


def _reply_retrieval_stub(
    result: ExtractionResult,
    query: object,  # RetrievalQuery — kept loose to avoid extra import noise
) -> str:
    """Step 6 will answer; for now we acknowledge with the parsed query."""
    label = getattr(getattr(query, "time_range", None), "label", "the requested period")
    category = getattr(query, "category", None)
    cat_str = f" in {category}" if category else ""
    return (
        f"Got the question (intent={result.intent.value}{cat_str} "
        f"for {label}). Retrieval lands in Step 6 - check back soon."
    )


__all__ = ["format_reply"]
