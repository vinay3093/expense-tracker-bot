"""Unit tests for the mirror reconciliation helper.

Two ``FakeSheetsBackend``-backed ledgers stand in for primary +
secondary so we can verify drift detection + back-fill without any
network or real Sheets / Postgres.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from expense_tracker.ledger.base import LedgerError, TransactionRow
from expense_tracker.ledger.mirror.reconcile import ReconcileReport, reconcile
from expense_tracker.ledger.sheets.adapter import SheetsLedgerBackend
from expense_tracker.ledger.sheets.backend import FakeSheetsBackend
from expense_tracker.ledger.sheets.format import get_sheet_format


def _make_ledger(name: str) -> SheetsLedgerBackend:
    return SheetsLedgerBackend(
        backend=FakeSheetsBackend(title=name, spreadsheet_id=f"sid-{name}"),
        sheet_format=get_sheet_format(),
    )


def _row(
    amount: float = 5.0,
    category: str = "Food",
    note: str = "coffee",
    day_offset: int = 0,
) -> TransactionRow:
    d = date(2026, 5, 3 + day_offset)
    return TransactionRow(
        date=d,
        day=d.strftime("%a"),
        month=d.strftime("%B"),
        year=d.year,
        category=category,
        note=note,
        vendor="Starbucks",
        amount=amount,
        currency="USD",
        amount_usd=amount,
        fx_rate=1.0,
        source="chat",
        trace_id=None,
        timestamp=datetime(2026, 5, 3 + day_offset, 14, 0, tzinfo=timezone.utc),
    )


# ─── Pre-conditions ─────────────────────────────────────────────────────


def test_in_sync_when_both_empty():
    p = _make_ledger("p")
    s = _make_ledger("s")
    p.init_storage()
    s.init_storage()

    rep = reconcile(p, s)
    assert rep.in_sync is True
    assert rep.missing_in_secondary == 0
    assert rep.extras_in_secondary == 0
    assert rep.backfilled == 0
    assert rep.needed_action is False


def test_in_sync_when_both_match():
    p = _make_ledger("p")
    s = _make_ledger("s")
    p.init_storage()
    s.init_storage()
    rows = [_row(amount=5), _row(amount=10, day_offset=1)]
    p.append(rows)
    s.append(rows)

    rep = reconcile(p, s)
    assert rep.in_sync is True
    assert rep.primary_total == 2
    assert rep.secondary_total == 2


# ─── Drift detection + back-fill ────────────────────────────────────────


def test_backfills_rows_missing_from_secondary():
    p = _make_ledger("p")
    s = _make_ledger("s")
    p.init_storage()
    s.init_storage()
    p.append([_row(amount=5), _row(amount=10, day_offset=1), _row(amount=15, day_offset=2)])
    s.append([_row(amount=5)])  # secondary only got the first

    rep = reconcile(p, s)

    assert rep.missing_in_secondary == 2
    assert rep.backfilled == 2
    assert rep.in_sync is True
    assert s.read_all().total_rows == 3


def test_backfill_preserves_chronological_order():
    """Back-filled rows should land in the same order they were
    originally logged on the primary, so MAX(id) on the secondary
    keeps pointing at the most-recent expense."""
    p = _make_ledger("p")
    s = _make_ledger("s")
    p.init_storage()
    s.init_storage()
    rows = [
        _row(amount=5, note="oldest"),
        _row(amount=10, note="middle", day_offset=1),
        _row(amount=15, note="newest", day_offset=2),
    ]
    p.append(rows)

    reconcile(p, s)

    secondary_rows = s.read_all().parsed
    notes_in_order = [r.note for r in secondary_rows]
    assert notes_in_order == ["oldest", "middle", "newest"]


def test_handles_duplicate_rows_correctly():
    """Two identical expenses on the same day must both back-fill,
    not collapse into one."""
    p = _make_ledger("p")
    s = _make_ledger("s")
    p.init_storage()
    s.init_storage()
    p.append([_row(amount=5), _row(amount=5)])  # two identical
    s.append([_row(amount=5)])                  # secondary only has one

    rep = reconcile(p, s)
    assert rep.missing_in_secondary == 1
    assert rep.backfilled == 1
    assert s.read_all().total_rows == 2


def test_reports_extras_but_does_not_delete_them():
    """A row in secondary but not in primary is reported as
    ``extras_in_secondary`` and LEFT IN PLACE.  Auto-deleting could
    eat a legit Postgres-only audit trail."""
    p = _make_ledger("p")
    s = _make_ledger("s")
    p.init_storage()
    s.init_storage()
    p.append([_row(amount=5)])
    s.append([_row(amount=5), _row(amount=99, category="Shopping", note="orphan")])

    rep = reconcile(p, s)
    assert rep.extras_in_secondary == 1
    assert rep.missing_in_secondary == 0
    assert rep.backfilled == 0
    # Extras retained:
    assert s.read_all().total_rows == 2
    assert rep.in_sync is False  # extras count against sync state


# ─── Dry-run mode ───────────────────────────────────────────────────────


def test_dry_run_does_not_mutate():
    p = _make_ledger("p")
    s = _make_ledger("s")
    p.init_storage()
    s.init_storage()
    p.append([_row(amount=5), _row(amount=10, day_offset=1)])

    rep = reconcile(p, s, dry_run=True)
    assert rep.missing_in_secondary == 2
    assert rep.backfilled == 0
    assert s.read_all().total_rows == 0


# ─── Error paths ────────────────────────────────────────────────────────


def test_read_failure_propagates():
    """Reads MUST fail loudly — silently mis-detecting drift would
    erode the operator's trust in the report."""
    p = _make_ledger("p")
    p.init_storage()

    class _BoomReader:
        name = "boom"
        transactions_label = "boom"

        def read_all(self, *, collect_skipped_detail: bool = False):
            raise LedgerError("read failed")

    with pytest.raises(LedgerError, match="read failed"):
        reconcile(p, _BoomReader())


def test_per_row_backfill_errors_are_collected_not_raised():
    """One bad row mustn't stop the rest from landing — append
    failures are accumulated into the report's ``backfill_errors``
    list."""
    p = _make_ledger("p")
    p.init_storage()
    p.append([_row(amount=5), _row(amount=10, day_offset=1)])

    # Hand-rolled secondary that fails on the FIRST append, succeeds
    # on the second.  We prove the loop continues + reports both.
    class _PartialFailureSecondary:
        name = "partial"
        transactions_label = "partial"

        def __init__(self) -> None:
            self.appended: list[TransactionRow] = []
            self._appends = 0

        def read_all(self, *, collect_skipped_detail: bool = False):
            from expense_tracker.ledger.base import LedgerInspection

            return LedgerInspection(
                sheet_name=self.transactions_label, parsed=[], skipped=[]
            )

        def append(self, rows):
            self._appends += 1
            if self._appends == 1:
                raise LedgerError("schema mismatch on row 1")
            self.appended.extend(rows)
            return [self._appends]

    sec = _PartialFailureSecondary()
    rep = reconcile(p, sec)

    assert rep.missing_in_secondary == 2
    assert rep.backfilled == 1
    assert len(rep.backfill_errors) == 1
    assert "schema mismatch" in rep.backfill_errors[0]
    assert rep.in_sync is False


# ─── Report structure sanity ────────────────────────────────────────────


def test_report_is_dataclass_immutable():
    rep = ReconcileReport(
        primary_total=3,
        secondary_total=3,
        missing_in_secondary=0,
        extras_in_secondary=0,
        backfilled=0,
        in_sync=True,
    )
    with pytest.raises((AttributeError, TypeError)):
        rep.backfilled = 99  # type: ignore[misc]
