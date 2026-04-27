"""Chat → row writer: turn one :class:`ExpenseEntry` into one Sheet row.

This is where the bot's "spent 40 on food" actually lands in Google
Sheets. The logger is intentionally *narrow* — it only handles the
``log_expense`` flow. Retrieval queries get their own reader in Step 6.

Pipeline of one ``log()`` call::

  ExpenseEntry
      │
      ▼
  resolve category to canonical (defensive — extractor already does this)
      │
      ▼
  convert amount to primary currency (USD) via CurrencyConverter
      │     - identity if already primary
      │     - cache hit if previously fetched
      │     - API call (cached on success)
      │     - stale-cache fallback if API down
      ▼
  ensure Transactions ledger tab exists  (init_transactions_tab — idempotent)
      │
      ▼
  ensure monthly summary tab exists       (ensure_month_tab — idempotent)
      │
      ▼
  build TransactionRow (timestamp, day name, month key, FX-converted USD)
      │
      ▼
  append_transactions(...)  → row lands in master ledger
      │
      ▼
  LogResult (used by the chat front-end to compose a reply + audit trail)

Idempotency / dedup: by design the bot allows duplicate amounts on the
same day (the user might genuinely buy two coffees). If we ever want a
client-side dedup heuristic we'd add it here, behind a flag.

Failure model: any underlying typed error (SheetsError, CurrencyError)
gets re-raised wrapped in :class:`ExpenseLogError` so the chat front-end
catches one type and produces one graceful reply.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..extractor.categories import CategoryRegistry
from ..extractor.schemas import ExpenseEntry
from ..sheets.backend import SheetsBackend, WorksheetHandle
from ..sheets.currency import ConversionResult, CurrencyConverter, CurrencyError
from ..sheets.exceptions import SheetsError
from ..sheets.format import SheetFormat
from ..sheets.transactions import (
    TransactionRow,
    append_transactions,
    init_transactions_tab,
)
from ..sheets.year_builder import ensure_month_tab
from .exceptions import ExpenseLogError

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LogResult:
    """What :meth:`ExpenseLogger.log` returns to the caller.

    Carries both *what was written* and *how it was derived* so the
    chat reply can mention the FX path, the auto-vivified tab, and the
    converted amount with full provenance.
    """

    transactions_tab: str
    """Name of the master ledger tab the row landed in."""

    monthly_tab: str
    """Name of the monthly summary tab covering this date — auto-created
    on the first transaction of a new month."""

    row: TransactionRow
    """The exact row appended to the Transactions tab."""

    fx_source: str
    """Provenance of the FX rate: ``"identity"``, ``"cache"``, ``"api"``,
    or ``"stale_cache_fallback"``."""

    monthly_tab_created: bool
    """True iff this call had to create the monthly tab (first row of a
    new month). Useful for telling the user "I set up May 2026 for you"."""

    def to_action_dict(self) -> dict[str, Any]:
        """Project to the ``action`` shape stored on ConversationTurn."""
        return {
            "type": "sheets_append",
            "status": "ok",
            "transactions_tab": self.transactions_tab,
            "monthly_tab": self.monthly_tab,
            "monthly_tab_created": self.monthly_tab_created,
            "category": self.row.category,
            "date": self.row.date.isoformat(),
            "amount": self.row.amount,
            "currency": self.row.currency,
            "amount_usd": self.row.amount_usd,
            "fx_rate": self.row.fx_rate,
            "fx_source": self.fx_source,
            "trace_id": self.row.trace_id,
        }


class ExpenseLogger:
    """Append one chat-derived expense to the spreadsheet.

    Constructed once per process; safe to call :meth:`log` repeatedly.
    The class holds no per-row mutable state — the only stateful
    collaborator is :class:`CurrencyConverter`, whose JSON cache is
    flushed transactionally inside its own ``convert`` calls.
    """

    def __init__(
        self,
        *,
        backend: SheetsBackend,
        sheet_format: SheetFormat,
        registry: CategoryRegistry,
        converter: CurrencyConverter,
        timezone: str,
        source: str = "chat",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend
        self._format = sheet_format
        self._registry = registry
        self._converter = converter
        self._tz = ZoneInfo(timezone)
        self._source = source
        self._now = now or (lambda: datetime.now(tz=self._tz))

    @property
    def primary_currency(self) -> str:
        """ISO-4217 of the primary currency the spreadsheet sums in."""
        return self._converter.primary_currency

    def log(
        self,
        entry: ExpenseEntry,
        *,
        trace_id: str | None = None,
    ) -> LogResult:
        """Append one expense to the master ledger and return a receipt.

        Raises:
            ExpenseLogError: any failure in the FX → ensure-tab → append
                chain, with the original exception attached as ``cause``.
        """
        canonical_category = self._resolve_category(entry.category)

        try:
            conv = self._convert_currency(entry)
        except CurrencyError as exc:
            raise ExpenseLogError(
                f"currency conversion failed for {entry.amount} "
                f"{entry.currency} -> {self.primary_currency}: {exc}",
                cause=exc,
            ) from exc

        try:
            self._ensure_transactions_tab()
            monthly_handle, created = self._ensure_monthly_tab(entry.date)
        except SheetsError as exc:
            raise ExpenseLogError(
                f"failed to prepare destination tabs: {exc}",
                cause=exc,
            ) from exc

        row = self._build_row(
            entry=entry,
            canonical_category=canonical_category,
            conv=conv,
            trace_id=trace_id,
        )

        try:
            append_transactions(self._backend, self._format, [row])
        except SheetsError as exc:
            raise ExpenseLogError(
                f"failed to append to {self._format.transactions.sheet_name!r}: {exc}",
                cause=exc,
            ) from exc

        return LogResult(
            transactions_tab=self._format.transactions.sheet_name,
            monthly_tab=monthly_handle.title,
            row=row,
            fx_source=conv.source,
            monthly_tab_created=created,
        )

    # ─── Helpers ────────────────────────────────────────────────────────

    def _resolve_category(self, raw: str) -> str:
        """Defensive canonicalization — extractor already does this, but
        log() is a public entry point that could be called outside the
        extractor pipeline (e.g. from a CLI ``--add`` shortcut)."""
        return self._registry.resolve_or_fallback(raw)

    def _convert_currency(self, entry: ExpenseEntry) -> ConversionResult:
        return self._converter.convert(
            amount=entry.amount,
            from_currency=entry.currency,
            on_date=entry.date,
        )

    def _ensure_transactions_tab(self) -> WorksheetHandle:
        """Create the master ledger if it doesn't exist; cheap if it does."""
        return init_transactions_tab(self._backend, self._format)

    def _ensure_monthly_tab(
        self, on_date: date_cls
    ) -> tuple[WorksheetHandle, bool]:
        """Make sure the monthly summary tab for ``on_date`` exists.

        Returns ``(handle, created)`` where ``created`` is True iff this
        call provisioned the tab (so the chat reply can mention it).
        """
        sheet_name = self._format.monthly_sheet_name(
            month_name=calendar.month_name[on_date.month],
            month_short=calendar.month_abbr[on_date.month],
            month_num=on_date.month,
            year=on_date.year,
        )
        existed = self._backend.has_worksheet(sheet_name)
        handle = ensure_month_tab(
            self._backend,
            self._format,
            year=on_date.year,
            month=on_date.month,
            categories=self._registry.canonical_names(),
        )
        return handle, not existed

    def _build_row(
        self,
        *,
        entry: ExpenseEntry,
        canonical_category: str,
        conv: ConversionResult,
        trace_id: str | None,
    ) -> TransactionRow:
        """Project an ExpenseEntry into the Transactions row schema."""
        ts = self._now()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=self._tz)

        # ``day`` is the short weekday name in en-US — small, stable,
        # and unambiguous for both humans and SUMIFS predicates.
        # ``month`` is the human month name ("April"); ``year`` is the
        # 4-digit year. Together they give the user a friendlier view
        # than the old "2026-04" key while still being fully filterable.
        day_name = entry.date.strftime("%a")
        month_name = calendar.month_name[entry.date.month]

        return TransactionRow(
            date=entry.date,
            day=day_name,
            month=month_name,
            year=entry.date.year,
            category=canonical_category,
            note=entry.note,
            vendor=entry.vendor,
            amount=float(entry.amount),
            currency=entry.currency.upper(),
            amount_usd=float(conv.amount),
            fx_rate=float(conv.rate),
            source=self._source,
            trace_id=trace_id,
            timestamp=ts,
        )


__all__ = [
    "ExpenseLogger",
    "LogResult",
]
