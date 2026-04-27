"""Compose the user-facing chat reply for one pipeline turn.

Goal: short, deterministic, never-empty replies. The bot always says
*something* — never a silent success, never a stack trace.

Output shape is plain text, no markdown / no emoji, suitable for both
the CLI (where a one-liner is ideal) and Telegram (Step 7).

The function is pure: same inputs ⇒ same string. Tests pin every branch.
"""

from __future__ import annotations

from ..extractor.schemas import ExtractionResult, Intent, RetrievalQuery
from .exceptions import ExpenseLogError
from .logger import LogResult
from .retrieval import LedgerRow, RetrievalAnswer, RetrievalError

# ─── Public API ─────────────────────────────────────────────────────────

def format_reply(
    result: ExtractionResult,
    *,
    log_result: LogResult | None = None,
    log_error: ExpenseLogError | None = None,
    retrieval_answer: RetrievalAnswer | None = None,
    retrieval_error: RetrievalError | None = None,
) -> str:
    """Return the bot's user-facing reply for one extraction outcome.

    Args:
        result:            what the extractor produced.
        log_result:        present iff a log_expense entry was
                           successfully written to Sheets.
        log_error:         present iff the chat pipeline tried to log
                           an expense but the Sheets / FX layer failed.
        retrieval_answer:  present iff the chat pipeline answered a
                           retrieval query against the ledger.
        retrieval_error:   present iff the retrieval engine failed
                           reading / aggregating the ledger.
    """
    if result.error:
        return _reply_extractor_error(result.error)

    if log_error is not None:
        return _reply_log_failed(result, log_error)

    if retrieval_error is not None:
        return _reply_retrieval_failed(result, retrieval_error)

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
        if retrieval_answer is not None:
            return _reply_retrieval(result.query, retrieval_answer)
        # Extractor produced a query but the engine wasn't called
        # (older callers / tests without a wired engine). Echo the
        # parsed query so the user sees we understood, just couldn't
        # answer.
        return _reply_retrieval_unanswered(result, result.query)

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


# ─── Retrieval replies ──────────────────────────────────────────────────


def _reply_retrieval(query: RetrievalQuery, answer: RetrievalAnswer) -> str:
    """Dispatch to the per-intent retrieval formatter.

    All four query intents share the same :class:`RetrievalAnswer`
    shape; each formatter picks which fields are most useful for that
    framing. Empty windows always go through :func:`_reply_no_matches`
    so the user never sees a misleading "$0.00" without context.
    """
    if answer.transaction_count == 0:
        return _reply_no_matches(query)

    intent = answer.intent
    if intent == Intent.QUERY_PERIOD_TOTAL:
        return _reply_period_total(query, answer)
    if intent == Intent.QUERY_CATEGORY_TOTAL:
        return _reply_category_total(query, answer)
    if intent == Intent.QUERY_DAY:
        return _reply_day_detail(query, answer)
    if intent == Intent.QUERY_RECENT:
        return _reply_recent(query, answer)
    return _reply_period_total(query, answer)  # pragma: no cover


def _reply_period_total(query: RetrievalQuery, answer: RetrievalAnswer) -> str:
    """e.g. "April 2026: $1,234.56 across 42 transactions. Top:
    Groceries $450, Food $300, Tesla Car $240."""
    label = query.time_range.label
    head = (
        f"{label}: ${answer.total_usd:,.2f} across "
        f"{answer.transaction_count} transactions."
    )
    top = _top_categories(answer, n=3)
    if top:
        head += " Top: " + ", ".join(f"{c} ${v:,.2f}" for c, v in top) + "."
    if answer.largest is not None:
        head += " " + _largest_clause(answer.largest)
    return head


def _reply_category_total(query: RetrievalQuery, answer: RetrievalAnswer) -> str:
    """e.g. "April 2026 / Food: $300.00 across 12 transactions.
    Largest: $45 on Sat 18 Apr at Chipotle."""
    label = query.time_range.label
    cat = query.category or "all categories"
    head = (
        f"{label} / {cat}: ${answer.total_usd:,.2f} across "
        f"{answer.transaction_count} transactions."
    )
    if answer.largest is not None:
        head += " " + _largest_clause(answer.largest)
    return head


def _reply_day_detail(query: RetrievalQuery, answer: RetrievalAnswer) -> str:
    """e.g. "Sat 25 Apr 2026: 3 transactions totaling $87.50.
    Food $40 (Starbucks: coffee), Groceries $35 (Costco), Saloon
    $12.50 (haircut)."""
    label = query.time_range.label
    head = (
        f"{label}: {answer.transaction_count} "
        f"transaction{'s' if answer.transaction_count != 1 else ''} "
        f"totaling ${answer.total_usd:,.2f}."
    )
    rows = sorted(answer.matched_rows, key=lambda r: -r.amount_usd)
    parts = [_row_clause(r) for r in rows[:5]]
    if parts:
        head += " " + "; ".join(parts) + "."
    if len(rows) > 5:
        head += f" (+{len(rows) - 5} more)"
    return head


def _reply_recent(query: RetrievalQuery, answer: RetrievalAnswer) -> str:
    """e.g. "Last 5 (of 18 in April 2026): Sat 25 Apr Food $40
    (Starbucks); Fri 24 Apr Groceries $35 (Costco); …"""
    label = query.time_range.label
    shown = len(answer.matched_rows)
    total_in_window = answer.transaction_count
    head = f"Last {shown}"
    if total_in_window != shown:
        head += f" (of {total_in_window} in {label})"
    else:
        head += f" in {label}"
    head += ":"
    parts: list[str] = []
    for r in answer.matched_rows:
        parts.append(
            f"{r.date.strftime('%a %d %b')} "
            f"{r.category} ${r.amount_usd:,.2f}"
            + (f" ({r.vendor})" if r.vendor else "")
        )
    if parts:
        head += " " + "; ".join(parts) + "."
    return head


def _reply_no_matches(query: RetrievalQuery) -> str:
    """No rows matched the window + filters."""
    label = query.time_range.label
    cat = f" / {query.category}" if query.category else ""
    return (
        f"No expenses found for {label}{cat}. "
        "Try a wider window, or log some first with 'spent X on Y'."
    )


def _reply_retrieval_unanswered(
    result: ExtractionResult, query: RetrievalQuery,
) -> str:
    """We parsed the query but no engine was wired in to answer.

    Only reachable when ``ChatPipeline`` is constructed without a
    :class:`RetrievalEngine` (older tests). In production
    :func:`get_chat_pipeline` always provides one.
    """
    cat = f" / {query.category}" if query.category else ""
    return (
        f"Got the question (intent={result.intent.value}{cat} for "
        f"{query.time_range.label}). The retrieval engine isn't wired "
        "in for this caller — use the CLI '--chat' or the Telegram bot."
    )


def _reply_retrieval_failed(
    result: ExtractionResult, error: RetrievalError,
) -> str:
    parts = [
        "I understood your question but couldn't read the ledger.",
        f"Reason: {error}",
    ]
    if result.query is not None:
        parts.append(
            f"(query: {result.intent.value} for {result.query.time_range.label})"
        )
    parts.append("Try again in a moment, or run --whoami to check the connection.")
    return " ".join(parts)


# ─── Small render helpers ───────────────────────────────────────────────


def _top_categories(
    answer: RetrievalAnswer, *, n: int = 3,
) -> list[tuple[str, float]]:
    """Top ``n`` (category, total_usd) by spend, descending."""
    items = sorted(answer.by_category.items(), key=lambda kv: -kv[1])
    return items[:n]


def _largest_clause(row: LedgerRow) -> str:
    """e.g. "Largest: $45.00 on Sat 18 Apr (Chipotle: lunch)." """
    base = f"Largest: ${row.amount_usd:,.2f} on {row.date.strftime('%a %d %b')}"
    extras: list[str] = []
    if row.vendor:
        extras.append(row.vendor)
    if row.note:
        extras.append(row.note)
    if extras:
        base += " (" + ": ".join(extras) + ")"
    base += "."
    return base


def _row_clause(row: LedgerRow) -> str:
    """Compact "Category $X (Vendor: note)" used in day-detail replies."""
    base = f"{row.category} ${row.amount_usd:,.2f}"
    extras: list[str] = []
    if row.vendor:
        extras.append(row.vendor)
    if row.note:
        extras.append(row.note)
    if extras:
        base += " (" + ": ".join(extras) + ")"
    return base


__all__ = ["format_reply"]
