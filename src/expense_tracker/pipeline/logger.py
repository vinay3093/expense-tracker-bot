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
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..extractor.categories import CategoryRegistry
from ..extractor.schemas import ExpenseEntry
from ..ledger.base import LedgerBackend, LedgerError, TransactionRow
from ..ledger.sheets.currency import ConversionResult, CurrencyConverter, CurrencyError
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
    """Human-readable label of the master ledger destination — for the
    Sheets edition that's the tab name, for the Postgres edition the
    table name."""

    monthly_tab: str | None
    """Sheets-edition only: name of the monthly summary tab covering
    this date.  ``None`` for the Postgres edition (no per-month tabs)."""

    row: TransactionRow
    """The exact row appended to the Transactions tab."""

    fx_source: str
    """Provenance of the FX rate: ``"identity"``, ``"cache"``, ``"api"``,
    or ``"stale_cache_fallback"``."""

    monthly_tab_created: bool
    """Sheets-edition only: ``True`` iff this call had to create the
    monthly tab (first row of a new month).  Always ``False`` on the
    Postgres edition — there's nothing per-month to provision."""

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
        ledger: LedgerBackend,
        registry: CategoryRegistry,
        converter: CurrencyConverter,
        timezone: str,
        source: str = "chat",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
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
            self._ledger.init_storage()
            period = self._ledger.ensure_period(
                year=entry.date.year,
                month=entry.date.month,
                categories=self._registry.canonical_names(),
            )
        except LedgerError as exc:
            raise ExpenseLogError(
                f"failed to prepare ledger destination: {exc}",
                cause=exc,
            ) from exc

        row = self._build_row(
            entry=entry,
            canonical_category=canonical_category,
            conv=conv,
            trace_id=trace_id,
        )

        try:
            self._ledger.append([row])
        except LedgerError as exc:
            raise ExpenseLogError(
                f"failed to append to {self._ledger.transactions_label!r}: {exc}",
                cause=exc,
            ) from exc

        # Nudge any backend-side recomputation (Sheets formula cache
        # bust).  Failure logged + swallowed — the row is already
        # safely written; recomputation is a UX concern, never a
        # correctness one.
        self._ledger.recompute_period(
            year=entry.date.year,
            month=entry.date.month,
            categories=self._registry.canonical_names(),
        )

        return LogResult(
            transactions_tab=self._ledger.transactions_label,
            monthly_tab=period.name,
            row=row,
            fx_source=conv.source,
            monthly_tab_created=period.created,
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
