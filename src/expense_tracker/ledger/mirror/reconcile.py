"""Drift detection + repair for :class:`MirrorLedgerBackend`.

The mirror writes to a primary and a best-effort secondary.  When the
secondary is briefly unreachable (Supabase outage, network blip),
writes pile up on the primary while the secondary falls behind.

This module finds those gaps and fixes them.

Algorithm
---------

1. Read every row from primary (source-of-truth scan).
2. Read every row from secondary.
3. Compute a content fingerprint for each row that is stable across
   backends (date, vendor, note, category, amount, currency).
4. For each fingerprint in primary not in secondary → append to
   secondary.
5. Print (and return) a structured report: missing, extras,
   back-filled, errors.

Why fingerprint instead of row index?
-------------------------------------

Sheets row indices and Postgres SERIAL IDs are independent
sequences.  A fingerprint of the user-entered fields is the only
identifier that survives across backends — and the natural-key
combination of (date, vendor, amount, currency, category, note) is
unique enough for a personal bot (you'd have to log two identical
expenses on the same day for false positives, and even then we
fall back on multiplicity counting).

Multiplicity is preserved by counting fingerprints, not just
checking presence — so logging "$5 coffee" twice in one day still
back-fills correctly.

Caveats
-------

Reconcile is a one-way back-fill: rows in secondary but not in
primary are *reported* but never deleted.  That's defensive —
a Postgres row that's in the audit log but missing from Sheets
might be a legit edit history we'd want to keep.  The operator
decides whether to act on the report.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..base import LedgerBackend, LedgerError, LedgerRow, TransactionRow

_log = logging.getLogger(__name__)


# ─── Public report shape ────────────────────────────────────────────────


@dataclass(frozen=True)
class ReconcileReport:
    """Outcome of one ``--reconcile`` run.

    All counts are *post-execution*: ``missing_in_secondary`` is the
    set we *attempted* to back-fill; ``backfilled`` is how many
    actually landed; ``backfill_errors`` is the (count, message) of
    failures during back-fill.
    """

    primary_total: int
    """Number of rows successfully read from primary."""

    secondary_total: int
    """Number of rows successfully read from secondary."""

    missing_in_secondary: int
    """Rows present in primary but absent from secondary
    (pre-back-fill count)."""

    extras_in_secondary: int
    """Rows present in secondary but absent from primary.
    Reported, NEVER auto-deleted."""

    backfilled: int
    """Rows successfully appended to secondary during this run."""

    backfill_errors: list[str] = field(default_factory=list)
    """Human-readable errors from the back-fill phase, if any."""

    in_sync: bool = False
    """True iff post-run primary and secondary contain the same
    multiset of fingerprints."""

    @property
    def needed_action(self) -> bool:
        """True iff there was anything to fix when the run started."""
        return self.missing_in_secondary > 0 or self.extras_in_secondary > 0


# ─── Public entry point ─────────────────────────────────────────────────


def reconcile(
    primary: LedgerBackend,
    secondary: LedgerBackend,
    *,
    dry_run: bool = False,
) -> ReconcileReport:
    """Bring ``secondary`` into sync with ``primary`` by back-filling
    missing rows.

    Args:
        primary: source-of-truth backend (Sheets in the standard
            mirror config).
        secondary: backend that may have fallen behind (Postgres /
            Supabase typically).
        dry_run: when True, compute the report but do NOT append to
            the secondary.  Useful for "show me drift before I let
            you fix it."

    Raises:
        LedgerError: if EITHER read fails.  We deliberately don't
            try to mask read failures — they indicate the user can't
            assess drift safely.

    Note:
        Back-fill failures are caught and counted into
        ``backfill_errors`` rather than raised — one bad row mustn't
        stop the rest from landing.
    """
    primary_inspection = primary.read_all(collect_skipped_detail=False)
    secondary_inspection = secondary.read_all(collect_skipped_detail=False)

    primary_rows = primary_inspection.parsed
    secondary_rows = secondary_inspection.parsed

    primary_fps = Counter(_fingerprint(r) for r in primary_rows)
    secondary_fps = Counter(_fingerprint(r) for r in secondary_rows)

    missing = primary_fps - secondary_fps          # in P but not S
    extras = secondary_fps - primary_fps           # in S but not P

    missing_count = sum(missing.values())
    extras_count = sum(extras.values())

    if missing_count == 0:
        _log.info(
            "reconcile: no drift — primary and secondary already in sync "
            "(primary=%d rows, secondary=%d rows, %d extras in secondary).",
            len(primary_rows), len(secondary_rows), extras_count,
        )
        return ReconcileReport(
            primary_total=len(primary_rows),
            secondary_total=len(secondary_rows),
            missing_in_secondary=0,
            extras_in_secondary=extras_count,
            backfilled=0,
            in_sync=(extras_count == 0),
        )

    if dry_run:
        _log.info(
            "reconcile: dry-run — would back-fill %d row(s) into %s.",
            missing_count, secondary.name,
        )
        return ReconcileReport(
            primary_total=len(primary_rows),
            secondary_total=len(secondary_rows),
            missing_in_secondary=missing_count,
            extras_in_secondary=extras_count,
            backfilled=0,
            in_sync=False,
        )

    # Materialise the rows to back-fill.  We pull them from the
    # primary's `LedgerRow` list (rather than the Counter) so we
    # carry through every field — including fields the fingerprint
    # ignores (timestamp, fx_rate, source, trace_id).
    to_backfill = list(_select_rows_for_backfill(primary_rows, missing))
    _log.info(
        "reconcile: back-filling %d row(s) into %s ...",
        len(to_backfill), secondary.name,
    )

    backfilled = 0
    errors: list[str] = []
    # One-row-per-call so a single bad row doesn't trash a whole batch.
    # Costlier in API calls but reconcile is a maintenance op, not a
    # hot path — typically run a handful of times a year.
    for ledger_row in to_backfill:
        tx = _ledger_row_to_transaction(ledger_row)
        try:
            secondary.append([tx])
        except LedgerError as exc:
            msg = (
                f"row_index={ledger_row.row_index} "
                f"date={ledger_row.date.isoformat()} "
                f"category={ledger_row.category!r} "
                f"amount={ledger_row.amount!r}: {exc}"
            )
            _log.warning("reconcile: back-fill failed for %s", msg)
            errors.append(msg)
        else:
            backfilled += 1

    # Re-check sync state cheaply: subtract back-fills from the
    # missing counter; sync iff missing now empty AND no extras.
    in_sync = (missing_count - backfilled == 0) and extras_count == 0

    return ReconcileReport(
        primary_total=len(primary_rows),
        secondary_total=len(secondary_rows) + backfilled,
        missing_in_secondary=missing_count,
        extras_in_secondary=extras_count,
        backfilled=backfilled,
        backfill_errors=errors,
        in_sync=in_sync,
    )


# ─── Internals ──────────────────────────────────────────────────────────


# Fingerprint = the user-entered fields that uniquely identify what
# they typed.  Excludes:
#   - row_index (backend-assigned, won't match across editions)
#   - timestamp (auto-stamped, will differ between primary write and
#     secondary back-fill)
#   - source / trace_id (provenance, not identity)
#   - fx_rate (derivable from amount + amount_usd)
#   - amount_usd, day, month, year (derivable from amount/currency/date)
_FingerprintKey = tuple[str, str, str, str, str, str, str]
"""(date_iso, category, amount, currency, vendor, note, amount_usd_rounded)

amount + amount_usd combine to discriminate edits that change one but
not the other (e.g. user edited the FX rate manually).  Rounded to
2 dp on amount_usd because cross-backend float<->Decimal round-trips
sometimes drift in the 4th-5th decimal.
"""


def _fingerprint(row: LedgerRow) -> _FingerprintKey:
    return (
        row.date.isoformat(),
        row.category.strip().lower(),
        f"{row.amount:.4f}",
        row.currency.strip().upper(),
        (row.vendor or "").strip().lower(),
        (row.note or "").strip().lower(),
        f"{row.amount_usd:.2f}",
    )


def _select_rows_for_backfill(
    primary_rows: Iterable[LedgerRow],
    missing: Counter[_FingerprintKey],
) -> Iterable[LedgerRow]:
    """Yield primary rows that need back-fill, preserving multiplicity.

    Walking the primary rows in their native order means back-fills
    land in the secondary in the same chronological sequence as the
    user logged them — keeps ``MAX(id)`` semantics meaningful for
    /undo after a reconcile.
    """
    remaining = Counter(missing)
    for row in primary_rows:
        fp = _fingerprint(row)
        if remaining.get(fp, 0) > 0:
            yield row
            remaining[fp] -= 1
            if remaining[fp] == 0:
                del remaining[fp]


def _ledger_row_to_transaction(row: LedgerRow) -> TransactionRow:
    """Convert a read-side :class:`LedgerRow` back into a write-side
    :class:`TransactionRow`.  Used during back-fill.

    ``source`` is rewritten to ``"reconcile"`` so the secondary's
    audit trail makes the row's provenance obvious.  Everything else
    is preserved.  When the primary row had no timestamp (legacy
    Sheets rows), we stamp ``now`` so the secondary has a usable
    timestamp for ``MAX(id)`` ordering.
    """
    return TransactionRow(
        date=row.date,
        day=row.day,
        month=row.month,
        year=row.year,
        category=row.category,
        note=row.note,
        vendor=row.vendor,
        amount=row.amount,
        currency=row.currency,
        amount_usd=row.amount_usd,
        fx_rate=row.fx_rate,
        source="reconcile",
        trace_id=row.trace_id,
        timestamp=row.timestamp or datetime.now(timezone.utc),
    )


__all__ = ["ReconcileReport", "reconcile"]
