"""Retrieval engine: ``RetrievalQuery`` → typed answer from the ledger.

This is the **read counterpart** to :class:`ExpenseLogger`. The chat
pipeline routes ``query_*`` intents here; the engine reads the master
``Transactions`` ledger, filters in Python, and aggregates.

Why read from ``Transactions`` rather than from the monthly / YTD tabs:

1. ``Transactions`` is the single source of truth — every row written by
   the bot (or pasted in by hand) lands there with explicit
   ``Year`` / ``Month`` / ``Date`` / ``Category`` / ``Amount (USD)``
   columns. The monthly + YTD tabs are derived views.
2. Reading once and filtering in Python beats reading 13 SUMIFS results
   per category x monthly tab, and avoids the stale-formula-cache class
   of bug we already had to chase down on the *write* side (Step 7.1).
3. For personal scale (years of data ≈ a few thousand rows) the
   round-trip is fast and predictable.

The engine is intentionally **stateless** across calls. Construct one
per process and call :meth:`answer` for every retrieval query.

Failure model
-------------
* Read failures from the Sheets layer wrap into :class:`RetrievalError`.
* Empty / missing ledgers return an empty :class:`RetrievalAnswer` (zero
  total, zero rows). The chat reply layer handles "no data found"
  framing.
* Per-row parse errors *skip the row* and emit a debug log — one
  malformed cell never blocks the rest of the answer.

Step 6 milestone: this file is the new code; ``pipeline/chat.py`` and
``pipeline/reply.py`` are the wiring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Any

from ..extractor.categories import CategoryRegistry
from ..extractor.schemas import Intent, RetrievalQuery
from ..ledger.base import (
    LedgerBackend,
    LedgerError,
    LedgerInspection,
    LedgerRow,
    SkippedRow,
)
from .exceptions import PipelineError

_log = logging.getLogger(__name__)


# ─── Errors ─────────────────────────────────────────────────────────────


class RetrievalError(PipelineError):
    """Raised when reading / filtering the ledger fails.

    Wraps a lower-level :class:`SheetsError` (or any other underlying
    exception) so the chat layer can catch one type and produce one
    graceful reply — same pattern as :class:`ExpenseLogError`.
    """

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


# ─── Retrieval-only data shape ─────────────────────────────────────────


@dataclass(frozen=True)
class RetrievalAnswer:
    """Typed answer for one :class:`RetrievalQuery`.

    Always carries the totals + counts; the per-intent caller picks
    which fields to show. Keeping the shape uniform makes the reply
    formatter trivial and the engine straightforward to test.
    """

    intent: Intent
    query: RetrievalQuery
    matched_rows: list[LedgerRow] = field(default_factory=list)
    total_usd: float = 0.0
    transaction_count: int = 0
    by_category: dict[str, float] = field(default_factory=dict)
    by_day: dict[date_cls, float] = field(default_factory=dict)
    largest: LedgerRow | None = None
    skipped_rows: int = 0
    """Rows that couldn't be parsed and were excluded from the answer."""

    def to_action_dict(self) -> dict[str, Any]:
        """Project to the ``action`` shape stored on ConversationTurn.

        Mirrors :meth:`LogResult.to_action_dict` so the JSONL audit
        trail looks consistent across log + retrieval turns.
        """
        return {
            "type": "sheets_query",
            "status": "ok",
            "intent": self.intent.value,
            "label": self.query.time_range.label,
            "start": self.query.time_range.start.isoformat(),
            "end": self.query.time_range.end.isoformat(),
            "category": self.query.category,
            "vendor": self.query.vendor,
            "limit": self.query.limit,
            "total_usd": round(self.total_usd, 2),
            "transaction_count": self.transaction_count,
            "by_category": {k: round(v, 2) for k, v in self.by_category.items()},
            "skipped_rows": self.skipped_rows,
        }


# ─── Engine ─────────────────────────────────────────────────────────────


class RetrievalEngine:
    """Answer a :class:`RetrievalQuery` by reading + aggregating the
    ``Transactions`` ledger.

    Construct once per process; :meth:`answer` is read-only and
    side-effect-free, safe to call from any thread that holds the
    backend (gspread is connection-pooled internally).
    """

    def __init__(
        self,
        *,
        ledger: LedgerBackend,
        registry: CategoryRegistry,
    ) -> None:
        self._ledger = ledger
        self._registry = registry

    def answer(self, query: RetrievalQuery) -> RetrievalAnswer:
        """Run *query* against the master ledger and return an answer.

        Raises:
            RetrievalError: any unexpected failure reading the
                master ledger.  Empty / missing storage is *not* an
                error — yields an empty answer.
        """
        try:
            inspection = self._ledger.read_all(collect_skipped_detail=False)
        except LedgerError as exc:
            raise RetrievalError(
                f"failed to read {self._ledger.transactions_label!r}: {exc}",
                cause=exc,
            ) from exc
        rows = inspection.parsed
        skipped = len(inspection.skipped)

        canonical_category = self._canonicalize_category(query.category)
        start, end = query.time_range.start, query.time_range.end
        vendor_q = (query.vendor or "").strip().lower() or None

        # Single pass over the ledger — collect every in-window row and
        # accumulate aggregates. Aggregates always describe the full
        # window so the reply can say "your last 5 of 18 in April".
        in_window: list[LedgerRow] = []
        by_category: dict[str, float] = {}
        by_day: dict[date_cls, float] = {}
        total_usd = 0.0
        largest: LedgerRow | None = None

        for row in rows:
            if not (start <= row.date <= end):
                continue
            if canonical_category is not None and row.category != canonical_category:
                continue
            if vendor_q is not None:
                row_vendor = (row.vendor or "").strip().lower()
                if vendor_q not in row_vendor:
                    continue

            in_window.append(row)
            total_usd += row.amount_usd
            by_category[row.category] = by_category.get(row.category, 0.0) + row.amount_usd
            by_day[row.date] = by_day.get(row.date, 0.0) + row.amount_usd
            if largest is None or row.amount_usd > largest.amount_usd:
                largest = row

        # ``query_recent`` returns only the last N (newest first) but
        # the aggregates stay full-window. Other intents echo every
        # matching row so the reply layer can render per-day / per-cat
        # breakdowns.
        if query.intent == Intent.QUERY_RECENT:
            limit = query.limit or 5
            sorted_window = sorted(
                in_window, key=lambda r: (r.date, r.row_index), reverse=True,
            )
            displayed = sorted_window[:limit]
        else:
            displayed = in_window

        return RetrievalAnswer(
            intent=query.intent,
            query=query,
            matched_rows=displayed,
            total_usd=round(total_usd, 2),
            transaction_count=len(in_window),
            by_category={k: round(v, 2) for k, v in by_category.items()},
            by_day={d: round(v, 2) for d, v in by_day.items()},
            largest=largest,
            skipped_rows=skipped,
        )

    # ─── Helpers ───────────────────────────────────────────────────────

    def _canonicalize_category(self, raw: str | None) -> str | None:
        """Return canonical category name, or ``None`` for "all".

        Unknown labels stay as ``None`` rather than collapsing to the
        fallback — a query for a non-existent category should match
        zero rows, not silently rewrite to "Miscellaneous".
        """
        if raw is None or not raw.strip():
            return None
        resolved = self._registry.resolve(raw)
        return resolved if resolved is not None else raw.strip()

    def inspect_ledger(self) -> LedgerInspection:
        """Read every row of the master ledger and report parse results.

        Used by the ``--inspect-ledger`` CLI command for diagnosing
        rows that fail to parse (so the operator can locate + fix
        them in the storage UI).  Empty / missing storage returns an
        inspection with both lists empty.

        Raises:
            RetrievalError: unexpected read failure.
        """
        try:
            return self._ledger.read_all(collect_skipped_detail=True)
        except LedgerError as exc:
            raise RetrievalError(
                f"failed to read {self._ledger.transactions_label!r}: {exc}",
                cause=exc,
            ) from exc


__all__ = [
    "LedgerInspection",
    "LedgerRow",
    "RetrievalAnswer",
    "RetrievalEngine",
    "RetrievalError",
    "SkippedRow",
]
