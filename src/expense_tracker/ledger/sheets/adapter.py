"""Sheets edition of :class:`LedgerBackend`.

This is a thin adapter that exposes the existing function-based
Sheets API as the storage Protocol the chat pipeline depends on.

Why not rewrite the Sheets module to be class-based?
----------------------------------------------------

The existing free-function API (:func:`init_transactions_tab`,
:func:`append_transactions`, :func:`get_last_row`, etc.) is well
tested, documented, and used directly by the admin CLI commands
(``--init-transactions``, ``--build-month``, ``--setup-year``, ...).
Wrapping it in an adapter is a one-time cost; rewriting it would
churn dozens of tests and the CLI for no functional gain.

This adapter:

1. Holds the underlying :class:`SheetsBackend` connection + the
   :class:`SheetFormat` config.
2. Translates Protocol calls (``ledger.append([row])``) into module
   calls (``append_transactions(self._backend, self._format, [row])``).
3. Translates the on-disk row layout into the universal
   :class:`LedgerRow` / :class:`LedgerInspection` shapes — the same
   parsing logic that previously lived inside
   :class:`RetrievalEngine` is centralised here so every read path
   (retrieval, summary, inspection) sees identical results.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime
from typing import Any

from ..base import (
    BackendHealth,
    LastRow,
    LedgerInspection,
    LedgerRow,
    PeriodInfo,
    SkippedRow,
    TransactionRow,
)
from .backend import SheetsBackend, col_index_to_letter
from .exceptions import SheetsError
from .format import SheetFormat
from .month_builder import force_month_recompute
from .transactions import (
    TRANSACTIONS_COLUMNS,
    append_transactions,
    delete_last_row,
    get_last_row,
    index_for,
    init_transactions_tab,
    update_last_row_fields,
)
from .year_builder import ensure_month_tab

_log = logging.getLogger(__name__)


class SheetsLedgerBackend:
    """Sheets implementation of :class:`LedgerBackend`.

    Construct once per process and pass into the chat pipeline.  All
    methods are safe to call repeatedly; the backing
    :class:`SheetsBackend` handles its own connection pooling.
    """

    name = "sheets"

    def __init__(
        self,
        *,
        backend: SheetsBackend,
        sheet_format: SheetFormat,
    ) -> None:
        self._backend = backend
        self._format = sheet_format

    @property
    def transactions_label(self) -> str:
        return self._format.transactions.sheet_name

    @property
    def sheet_format(self) -> SheetFormat:
        """Underlying YAML config — exposed for Sheets-only callers
        (admin CLI commands) that still need direct format access."""
        return self._format

    @property
    def sheets_backend(self) -> SheetsBackend:
        """Underlying gspread connection — exposed for Sheets-only
        admin commands like ``--list-sheets``."""
        return self._backend

    # ─── Lifecycle ─────────────────────────────────────────────────────

    def health_check(self) -> BackendHealth:
        """Cheap connectivity ping.  Reads the spreadsheet title.

        Failure (network down, bad creds) returns ``ok=False`` with a
        diagnostic detail string rather than raising — the
        ``--healthcheck`` CLI command surfaces every backend's status
        without short-circuiting on the first one to fail.
        """
        start = time.perf_counter()
        try:
            title = self._backend.title
        except SheetsError as exc:
            return BackendHealth(
                ok=False,
                backend=self.name,
                latency_ms=(time.perf_counter() - start) * 1000,
                detail=f"sheets unreachable: {exc}",
            )
        return BackendHealth(
            ok=True,
            backend=self.name,
            latency_ms=(time.perf_counter() - start) * 1000,
            detail=f"spreadsheet={title!r}",
        )

    def init_storage(self) -> None:
        """Create the master ``Transactions`` tab if missing.  Idempotent."""
        init_transactions_tab(self._backend, self._format)

    def ensure_period(
        self,
        *,
        year: int,
        month: int,
        categories: Sequence[str],
    ) -> PeriodInfo:
        """Create the monthly summary tab for ``(year, month)`` if missing.

        Returns the tab name + a flag indicating whether we just
        created it, so the chat reply can announce "I set up May
        2026 for you" on the first row of a new month.
        """
        import calendar

        sheet_name = self._format.monthly_sheet_name(
            month_name=calendar.month_name[month],
            month_short=calendar.month_abbr[month],
            month_num=month,
            year=year,
        )
        existed = self._backend.has_worksheet(sheet_name)
        ensure_month_tab(
            self._backend,
            self._format,
            year=year,
            month=month,
            categories=list(categories),
        )
        return PeriodInfo(name=sheet_name, created=not existed)

    # ─── Write side ────────────────────────────────────────────────────

    def append(self, rows: Sequence[TransactionRow]) -> list[int]:
        """Append rows to the master ledger.

        Returns the assigned 1-based row indices.  We need a follow-up
        read to discover the indices because ``append_rows`` is
        fire-and-forget; the cost is one extra ``get_values`` per
        write batch, negligible at personal scale.
        """
        if not rows:
            return []
        ws = append_transactions(self._backend, self._format, list(rows))
        # Indices = (current row count) - (rows we just added) + ... + (current row count).
        # ``get_values`` on a single date column gives an accurate count
        # without pulling the full row payload.
        date_col_letter = "A"  # ``date`` is always column A; see TRANSACTIONS_COLUMNS.
        date_values = ws.get_values(f"{date_col_letter}2:{date_col_letter}")
        # Trim trailing blank rows that the Sheets API can pad with.
        last_filled_offset: int = -1
        for i, r in enumerate(date_values):
            cell = r[0] if r else ""
            if cell not in ("", None):
                last_filled_offset = i
        if last_filled_offset == -1:
            return []
        end_sheet_row = 2 + last_filled_offset
        start_sheet_row = end_sheet_row - len(rows) + 1
        return list(range(start_sheet_row, end_sheet_row + 1))

    def recompute_period(
        self,
        *,
        year: int,
        month: int,
        categories: Sequence[str],
    ) -> str | None:
        """Force the monthly tab to recompute its formulas.

        Sheets serves a stale formula cache after API writes; this
        re-asserts headline formulas to bust it.  Failure logged +
        swallowed — recomputation is a UX concern, never correctness.
        """
        try:
            return force_month_recompute(
                self._backend,
                self._format,
                year=year,
                month=month,
                categories=list(categories),
            )
        except SheetsError as exc:
            _log.warning("recompute_period(%d-%02d) failed: %s", year, month, exc)
            return None

    # ─── Read side ─────────────────────────────────────────────────────

    def read_all(
        self,
        *,
        collect_skipped_detail: bool = False,
    ) -> LedgerInspection:
        """Read every data row from the ``Transactions`` tab.

        Empty / missing tabs return an inspection with both lists
        empty (no error).  Per-row parse failures are quietly skipped
        unless ``collect_skipped_detail=True``, in which case they're
        surfaced for diagnostic CLI commands.
        """
        name = self._format.transactions.sheet_name
        if not self._backend.has_worksheet(name):
            return LedgerInspection(sheet_name=name, parsed=[], skipped=[])

        ws = self._backend.get_worksheet(name)
        last_col_letter = col_index_to_letter(len(TRANSACTIONS_COLUMNS) - 1)
        raw = ws.get_values(f"A2:{last_col_letter}")

        parsed: list[LedgerRow] = []
        skipped: list[SkippedRow] = []
        for offset, row_values in enumerate(raw):
            sheet_row = 2 + offset
            result = _parse_sheets_row(row_values, sheet_row=sheet_row)
            if result is None:
                continue
            if isinstance(result, _ParseError):
                _log.debug(
                    "Skipping row %d in %s: %s", sheet_row, name, result.reason,
                )
                if collect_skipped_detail:
                    skipped.append(
                        SkippedRow(
                            row_index=sheet_row,
                            reason=result.reason,
                            raw_values=[str(v) for v in row_values],
                        ),
                    )
                else:
                    # We still need to count skipped rows so the
                    # caller can report them; use a stub SkippedRow
                    # with empty raw_values — cheap.
                    skipped.append(
                        SkippedRow(
                            row_index=sheet_row,
                            reason=result.reason,
                            raw_values=[],
                        ),
                    )
                continue
            parsed.append(result)
        return LedgerInspection(sheet_name=name, parsed=parsed, skipped=skipped)

    # ─── Last-row operations ───────────────────────────────────────────

    def get_last(self) -> LastRow:
        return get_last_row(self._backend, self._format)

    def delete_last(self) -> LastRow:
        return delete_last_row(self._backend, self._format)

    def update_last(self, updates: dict[str, Any]) -> LastRow:
        return update_last_row_fields(
            self._backend, self._format, updates=updates,
        )


# ─── Per-row parser (lifted from RetrievalEngine, centralised here) ────


@dataclass(frozen=True)
class _ParseError:
    reason: str


def _parse_sheets_row(
    values: list[Any],
    *,
    sheet_row: int,
) -> LedgerRow | _ParseError | None:
    """Coerce one positional Sheets row into a :class:`LedgerRow`.

    Returns:
        - ``None`` for empty rows (no Date cell at all → silent skip).
        - :class:`_ParseError` for rows where Date or Amount (USD)
          can't be parsed — caller logs + counts.
        - :class:`LedgerRow` on success.
    """
    if not values:
        return None

    def _at(key: str) -> Any:
        idx = index_for(key)
        return values[idx] if idx < len(values) else ""

    date_raw = _at("date")
    if date_raw in ("", None):
        return None

    try:
        d = _parse_date_cell(date_raw)
    except (TypeError, ValueError) as exc:
        return _ParseError(f"Date cell {date_raw!r}: {exc}")

    amount_usd_raw = _at("amount_usd")
    amount_usd = _coerce_number(amount_usd_raw, default=0.0)
    if amount_usd is None:
        return _ParseError(f"Amount (USD) cell {amount_usd_raw!r} not numeric")

    parsed_amount = _coerce_number(_at("amount"), default=amount_usd)
    amount = parsed_amount if parsed_amount is not None else amount_usd

    parsed_fx = _coerce_number(_at("fx_rate"), default=1.0)
    fx_rate = parsed_fx if parsed_fx is not None else 1.0

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


def _parse_date_cell(raw: Any) -> date_cls:
    """Best-effort parse a Sheets date cell back to ``datetime.date``."""
    if isinstance(raw, date_cls) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        return date_cls.fromisoformat(raw.strip())
    if isinstance(raw, (int, float)):
        from datetime import timedelta
        epoch = date_cls(1899, 12, 30)
        return epoch + timedelta(days=int(raw))
    raise TypeError(f"unparseable Date cell value: {raw!r} ({type(raw).__name__})")


def _optional_str(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _coerce_number(raw: Any, *, default: float) -> float | None:
    """Parse a numeric cell tolerantly.

    Handles three real-world formats:

    * Pure numbers — pass through ``float()``.
    * Empty / ``None`` — return ``default``.
    * Locale-formatted strings (``"1,000.00"``, ``"$1,000.00"``) —
      strip commas, currency signs, whitespace before parsing.

    Returns ``None`` only when the value is genuinely non-numeric
    (e.g. ``"oops"``) so the caller can flag it as a parse error.
    """
    if raw in ("", None):
        return default
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return default
    cleaned = s.replace(",", "").replace("$", "").replace("\u00a0", "").strip()
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return None


__all__ = ["SheetsLedgerBackend"]
