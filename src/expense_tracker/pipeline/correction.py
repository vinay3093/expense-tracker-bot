"""Undo / edit the most-recent expense.

This module is the chat-side counterpart to :class:`ExpenseLogger`. It
operates on the **bottom-most** row of the ``Transactions`` master
ledger — by convention always the most recently appended expense.

Two operations:

* :meth:`CorrectionLogger.undo` — delete the row entirely. The next
  :meth:`undo` then targets the row that used to be one above it.
* :meth:`CorrectionLogger.edit` — patch the amount and/or category of
  that row in place. Amount edits re-run FX so ``amount_usd`` stays
  consistent (the monthly + YTD tabs sum from ``amount_usd``).

After every successful undo / edit we ask the (year, month)-affected
monthly tab to recompute, working around a Google Sheets quirk where
API-driven changes to ``Transactions`` don't always invalidate the
formula cache on dependent tabs. See
:func:`expense_tracker.ledger.sheets.month_builder.force_month_recompute`.

Failure model: typed errors (``SheetsError``, ``CurrencyError``) get
wrapped in :class:`CorrectionError` so the chat layer catches one type.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Any

from ..extractor.categories import CategoryRegistry
from ..ledger.base import LastRow, LedgerBackend, LedgerError
from ..ledger.sheets.currency import CurrencyConverter, CurrencyError
from .exceptions import ExpenseLogError

_log = logging.getLogger(__name__)


class CorrectionError(ExpenseLogError):
    """Raised when undo / edit hits a typed error in the layers below."""


@dataclass(frozen=True)
class UndoResult:
    """Public return shape for :meth:`CorrectionLogger.undo`.

    ``deleted_row`` is the snapshot taken *before* deletion so the chat
    reply can echo what just disappeared. ``deleted_row.is_empty`` means
    the user invoked ``/undo`` on an empty ledger — handled gracefully
    rather than crashing.
    """

    deleted_row: LastRow
    transactions_tab: str
    """Human-readable label of the destination (Sheets tab name or
    Postgres table name)."""
    monthly_tab: str | None
    """Sheets-edition only: name of the recomputed monthly tab.
    ``None`` when the backend has no per-month concept."""
    monthly_tab_recomputed: bool


@dataclass(frozen=True)
class EditResult:
    """Public return shape for :meth:`CorrectionLogger.edit`.

    Carries both the pre-edit snapshot and the actual fields written.
    The chat layer uses both to render a diff like::

        Updated row 4: amount $100 → $50, category Saloon → Shopping.
    """

    before: LastRow
    applied: dict[str, Any]
    transactions_tab: str
    monthly_tab: str | None
    monthly_tab_recomputed: bool


class CorrectionLogger:
    """Undo / edit the bottom-most Transactions row.

    Stateless across calls; all state lives in the spreadsheet. Safe to
    construct once per process and call repeatedly.
    """

    def __init__(
        self,
        *,
        ledger: LedgerBackend,
        registry: CategoryRegistry,
        converter: CurrencyConverter,
    ) -> None:
        self._ledger = ledger
        self._registry = registry
        self._converter = converter

    # ─── Public API ────────────────────────────────────────────────────

    def peek_last(self) -> LastRow:
        """Return the bottom-most row without modifying it.

        Used by the chat layer to show a "you're about to delete X"
        confirmation when paranoid mode is on. Free of side effects.
        """
        try:
            return self._ledger.get_last()
        except LedgerError as exc:
            raise CorrectionError(f"failed to read last row: {exc}", cause=exc) from exc

    def undo(self) -> UndoResult:
        """Delete the bottom-most Transactions row.

        Idempotent on an empty ledger — returns a result with
        ``deleted_row.is_empty == True`` so the caller can reply
        "nothing to undo". The monthly tab covering the deleted row
        gets nudged to recompute so the daily grid + summary stay
        consistent.
        """
        try:
            snap = self._ledger.delete_last()
        except LedgerError as exc:
            raise CorrectionError(
                f"failed to delete last row: {exc}", cause=exc,
            ) from exc

        monthly_tab, recomputed = self._maybe_recompute_for_row(snap)
        return UndoResult(
            deleted_row=snap,
            transactions_tab=self._ledger.transactions_label,
            monthly_tab=monthly_tab,
            monthly_tab_recomputed=recomputed,
        )

    def edit(
        self,
        *,
        amount: float | None = None,
        category: str | None = None,
    ) -> EditResult:
        """Patch ``amount`` / ``category`` on the bottom-most row.

        At least one of ``amount`` / ``category`` must be provided.
        Amount edits re-run currency conversion against the original
        row's currency + date so the ``Amount (USD)`` and ``FX Rate``
        columns stay consistent. Category edits canonicalize through
        the registry so aliases like ``"groceries"`` collapse to the
        canonical ``"Groceries"``.
        """
        if amount is None and category is None:
            raise CorrectionError(
                "edit() requires at least one of amount / category"
            )

        try:
            before = self._ledger.get_last()
        except LedgerError as exc:
            raise CorrectionError(
                f"failed to read last row: {exc}", cause=exc,
            ) from exc

        if before.is_empty:
            return EditResult(
                before=before,
                applied={},
                transactions_tab=self._ledger.transactions_label,
                monthly_tab=None,
                monthly_tab_recomputed=False,
            )

        updates: dict[str, Any] = {}
        if category is not None:
            updates["category"] = self._registry.resolve_or_fallback(category)
        if amount is not None:
            if amount <= 0:
                raise CorrectionError(
                    f"amount must be positive, got {amount}"
                )
            currency = str(before.value("currency") or "USD").upper() or "USD"
            on_date = _parse_date_cell(before.value("date"))
            try:
                conv = self._converter.convert(
                    amount=amount, from_currency=currency, on_date=on_date,
                )
            except CurrencyError as exc:
                raise CorrectionError(
                    f"FX failed for {amount} {currency}: {exc}", cause=exc,
                ) from exc
            updates["amount"] = float(amount)
            updates["amount_usd"] = float(conv.amount)
            updates["fx_rate"] = float(conv.rate)

        try:
            self._ledger.update_last(updates)
        except LedgerError as exc:
            raise CorrectionError(
                f"failed to update last row: {exc}", cause=exc,
            ) from exc

        monthly_tab, recomputed = self._maybe_recompute_for_row(before)
        return EditResult(
            before=before,
            applied=updates,
            transactions_tab=self._ledger.transactions_label,
            monthly_tab=monthly_tab,
            monthly_tab_recomputed=recomputed,
        )

    # ─── Helpers ───────────────────────────────────────────────────────

    def _maybe_recompute_for_row(
        self, snap: LastRow,
    ) -> tuple[str | None, bool]:
        """Nudge the affected monthly tab to recompute, return its name.

        Returns ``(monthly_tab_name, was_recomputed)``. If the row is
        empty (nothing to recompute against) or its date can't be
        parsed, returns ``(None, False)`` and skips the nudge.
        Failures are logged but never raised — the user-facing operation
        already succeeded; refresh issues are a UX-only concern.
        """
        if snap.is_empty:
            return None, False
        try:
            on_date = _parse_date_cell(snap.value("date"))
        except (TypeError, ValueError):
            _log.debug("Could not parse Date cell %r — skipping recompute nudge",
                       snap.value("date"))
            return None, False

        tab_name = self._ledger.recompute_period(
            year=on_date.year,
            month=on_date.month,
            categories=self._registry.canonical_names(),
        )
        return tab_name, tab_name is not None


def _parse_date_cell(raw: Any) -> date_cls:
    """Best-effort parse a Sheets date cell back to ``datetime.date``.

    Sheets normalizes dates to ISO strings ("2026-04-27") when written
    via USER_ENTERED, but it can also surface them as serial numbers
    or already-parsed dates. We handle the common shapes and let the
    caller choose how to react to a parse failure.
    """
    if isinstance(raw, date_cls):
        return raw
    if isinstance(raw, str):
        return date_cls.fromisoformat(raw.strip())
    if isinstance(raw, (int, float)):
        # Sheets serial date: days since 1899-12-30.
        from datetime import timedelta

        epoch = date_cls(1899, 12, 30)
        return epoch + timedelta(days=int(raw))
    raise TypeError(f"unparseable Date cell value: {raw!r} ({type(raw).__name__})")


__all__ = [
    "CorrectionError",
    "CorrectionLogger",
    "EditResult",
    "UndoResult",
]
