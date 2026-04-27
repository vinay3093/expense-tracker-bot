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
from datetime import datetime
from typing import Any

from ..extractor.categories import CategoryRegistry
from ..extractor.schemas import Intent, RetrievalQuery
from ..sheets.backend import SheetsBackend, col_index_to_letter
from ..sheets.exceptions import SheetsError
from ..sheets.format import SheetFormat
from ..sheets.transactions import (
    TRANSACTIONS_COLUMNS,
    index_for,
)
from .correction import _parse_date_cell
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


# ─── Public data shapes ─────────────────────────────────────────────────


@dataclass(frozen=True)
class LedgerRow:
    """One parsed row from the ``Transactions`` master ledger.

    Mirrors :class:`expense_tracker.sheets.transactions.TransactionRow`
    but is built from *read* values (strings / numbers from the Sheets
    API), not from a typed in-memory object. Cells that fail to parse
    are coerced to a safe default (``None`` for optional fields, ``0.0``
    for ``amount_usd`` so totals don't blow up on one bad row).
    """

    row_index: int
    """1-based spreadsheet row index. Useful for diagnostics + `/undo`."""

    date: date_cls
    day: str
    month: str
    year: int
    category: str
    note: str | None
    vendor: str | None
    amount: float
    currency: str
    amount_usd: float
    fx_rate: float
    source: str
    trace_id: str | None
    timestamp: datetime | None


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
        backend: SheetsBackend,
        sheet_format: SheetFormat,
        registry: CategoryRegistry,
    ) -> None:
        self._backend = backend
        self._format = sheet_format
        self._registry = registry

    def answer(self, query: RetrievalQuery) -> RetrievalAnswer:
        """Run *query* against the master ledger and return an answer.

        Raises:
            RetrievalError: any unexpected failure reading the
                ``Transactions`` tab. Empty / missing tabs are *not* an
                error — they yield an empty answer.
        """
        try:
            rows, skipped = self._read_ledger()
        except SheetsError as exc:
            raise RetrievalError(
                f"failed to read {self._format.transactions.sheet_name!r}: {exc}",
                cause=exc,
            ) from exc

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

    def _read_ledger(self) -> tuple[list[LedgerRow], int]:
        """Read every data row from the ``Transactions`` tab.

        Returns ``(rows, skipped)``. Skipped rows are those whose
        Date or Amount (USD) cells couldn't be parsed; they're logged
        but never raise — one bad row shouldn't black-hole the whole
        answer.
        """
        name = self._format.transactions.sheet_name
        if not self._backend.has_worksheet(name):
            return [], 0

        ws = self._backend.get_worksheet(name)
        last_col_letter = col_index_to_letter(len(TRANSACTIONS_COLUMNS) - 1)
        raw = ws.get_values(f"A2:{last_col_letter}")

        out: list[LedgerRow] = []
        skipped = 0
        for offset, row_values in enumerate(raw):
            sheet_row = 2 + offset
            parsed = _parse_ledger_row(row_values, sheet_row=sheet_row)
            if parsed is None:
                # Empty row — nothing to skip, just stop tracking it.
                continue
            if isinstance(parsed, _ParseError):
                skipped += 1
                _log.debug(
                    "Skipping row %d in %s: %s",
                    sheet_row,
                    name,
                    parsed.reason,
                )
                continue
            out.append(parsed)
        return out, skipped


# ─── Per-row parser ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ParseError:
    reason: str


def _parse_ledger_row(
    values: list[Any],
    *,
    sheet_row: int,
) -> LedgerRow | _ParseError | None:
    """Coerce one read row into a :class:`LedgerRow`.

    Returns:
        - ``None`` for empty rows (no Date cell at all → skip silently).
        - :class:`_ParseError` for rows where Date or Amount (USD)
          can't be parsed (caller increments a counter + debug-logs).
        - :class:`LedgerRow` on success.

    Optional cells (Note, Vendor, Trace ID, Timestamp) parse to ``None``
    when blank rather than to empty strings — easier to format in the
    reply layer.
    """
    if not values:
        return None

    def _at(key: str) -> Any:
        idx = index_for(key)
        if idx >= len(values):
            return ""
        return values[idx]

    date_raw = _at("date")
    if date_raw in ("", None):
        return None

    try:
        d = _parse_date_cell(date_raw)
    except (TypeError, ValueError) as exc:
        return _ParseError(f"Date cell {date_raw!r}: {exc}")

    amount_usd_raw = _at("amount_usd")
    try:
        amount_usd = float(amount_usd_raw) if amount_usd_raw not in ("", None) else 0.0
    except (TypeError, ValueError):
        return _ParseError(f"Amount (USD) cell {amount_usd_raw!r} not numeric")

    amount_raw = _at("amount")
    try:
        amount = float(amount_raw) if amount_raw not in ("", None) else amount_usd
    except (TypeError, ValueError):
        amount = amount_usd

    fx_raw = _at("fx_rate")
    try:
        fx_rate = float(fx_raw) if fx_raw not in ("", None) else 1.0
    except (TypeError, ValueError):
        fx_rate = 1.0

    year_raw = _at("year")
    try:
        year = int(year_raw) if year_raw not in ("", None) else d.year
    except (TypeError, ValueError):
        year = d.year

    timestamp_raw = _at("timestamp")
    timestamp: datetime | None = None
    if isinstance(timestamp_raw, datetime):
        timestamp = timestamp_raw
    elif isinstance(timestamp_raw, str) and timestamp_raw.strip():
        try:
            timestamp = datetime.fromisoformat(timestamp_raw.strip())
        except ValueError:
            timestamp = None

    return LedgerRow(
        row_index=sheet_row,
        date=d,
        day=str(_at("day") or ""),
        month=str(_at("month") or ""),
        year=year,
        category=str(_at("category") or "").strip(),
        note=_optional_str(_at("note")),
        vendor=_optional_str(_at("vendor")),
        amount=amount,
        currency=str(_at("currency") or "USD").strip().upper() or "USD",
        amount_usd=amount_usd,
        fx_rate=fx_rate,
        source=str(_at("source") or "").strip(),
        trace_id=_optional_str(_at("trace_id")),
        timestamp=timestamp,
    )


def _optional_str(raw: Any) -> str | None:
    """Trim a cell, returning ``None`` for empties."""
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


__all__ = [
    "LedgerRow",
    "RetrievalAnswer",
    "RetrievalEngine",
    "RetrievalError",
]
