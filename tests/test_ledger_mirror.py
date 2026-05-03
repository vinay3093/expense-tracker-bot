"""Unit tests for :class:`MirrorLedgerBackend`.

Hermetic — uses two ``FakeSheetsBackend``-backed ledgers (one as
primary, one as secondary) so we can verify mirroring semantics
without touching Sheets, Postgres, or the network.

A custom ``_BoomLedger`` subclass exists for the failure-mode tests
to prove the secondary's exceptions are swallowed and never bubble
up to the chat layer.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Any

import pytest

from expense_tracker.ledger.base import (
    BackendHealth,
    LastRow,
    LedgerBackend,
    LedgerError,
    LedgerInspection,
    PeriodInfo,
    TransactionRow,
)
from expense_tracker.ledger.mirror.adapter import MirrorLedgerBackend
from expense_tracker.ledger.sheets.adapter import SheetsLedgerBackend
from expense_tracker.ledger.sheets.backend import FakeSheetsBackend
from expense_tracker.ledger.sheets.format import get_sheet_format

# ─── Helpers ────────────────────────────────────────────────────────────


def _make_sheets_ledger(title: str) -> SheetsLedgerBackend:
    return SheetsLedgerBackend(
        backend=FakeSheetsBackend(title=title, spreadsheet_id=f"id-{title}"),
        sheet_format=get_sheet_format(),
    )


def _row(amount: float = 5.0, category: str = "Food", note: str = "coffee") -> TransactionRow:
    """One realistic-looking transaction row."""
    return TransactionRow(
        date=date(2026, 5, 3),
        day="Sun",
        month="May",
        year=2026,
        category=category,
        note=note,
        vendor="Starbucks",
        amount=amount,
        currency="USD",
        amount_usd=amount,
        fx_rate=1.0,
        source="chat",
        trace_id="trace-123",
        timestamp=datetime(2026, 5, 3, 14, 30, tzinfo=timezone.utc),
    )


class _BoomLedger:
    """A LedgerBackend that raises on every operation.

    Used as the *secondary* in failure-mode tests to prove the
    primary's path is never affected by the secondary blowing up.
    Implements the Protocol surface only — never actually called for
    real I/O.
    """

    name = "boom"
    transactions_label = "boom"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls: list[str] = []

    def _bang(self, op: str) -> Any:
        self.calls.append(op)
        raise self._exc

    def health_check(self) -> BackendHealth:  # pragma: no cover — see _safe_secondary
        return self._bang("health_check")

    def init_storage(self) -> None:
        self._bang("init_storage")

    def ensure_period(self, *, year: int, month: int, categories: Sequence[str]) -> PeriodInfo:
        self._bang("ensure_period")

    def append(self, rows: Sequence[TransactionRow]) -> list[int]:
        self._bang("append")
        return []

    def recompute_period(self, *, year: int, month: int, categories: Sequence[str]) -> str | None:
        self._bang("recompute_period")
        return None

    def read_all(  # pragma: no cover - never called in mirror tests
        self, *, collect_skipped_detail: bool = False,
    ) -> LedgerInspection:
        self._bang("read_all")

    def get_last(self) -> LastRow:  # pragma: no cover - never called
        self._bang("get_last")
        return LastRow(is_empty=True, row_index=None)

    def delete_last(self) -> LastRow:
        self._bang("delete_last")
        return LastRow(is_empty=True, row_index=None)

    def update_last(self, updates: dict[str, Any]) -> LastRow:
        self._bang("update_last")
        return LastRow(is_empty=True, row_index=None)


# ─── Construction guards ────────────────────────────────────────────────


def test_rejects_same_primary_and_secondary():
    primary = _make_sheets_ledger("only")
    with pytest.raises(ValueError, match="distinct"):
        MirrorLedgerBackend(primary=primary, secondary=primary)


def test_protocol_compliance_runtime_check():
    """Runtime structural check — the wrapper IS-A LedgerBackend."""
    mirror = MirrorLedgerBackend(
        primary=_make_sheets_ledger("p"),
        secondary=_make_sheets_ledger("s"),
    )
    assert isinstance(mirror, LedgerBackend)


# ─── Happy-path: dual write lands in both ───────────────────────────────


def test_append_lands_in_both_backends():
    primary = _make_sheets_ledger("p")
    secondary = _make_sheets_ledger("s")
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)

    mirror.init_storage()
    ids = mirror.append([_row(amount=5), _row(amount=10)])

    assert len(ids) == 2
    assert primary.read_all().total_rows == 2
    assert secondary.read_all().total_rows == 2


def test_returned_ids_come_from_primary():
    """The ids surfaced to the chat reply are PRIMARY ids, not a mix."""
    primary = _make_sheets_ledger("p")
    secondary = _make_sheets_ledger("s")
    # Pre-populate secondary with one row so its row indices are
    # offset from primary's.  Returned ids must still match primary.
    secondary.init_storage()
    secondary.append([_row(amount=99, category="Saloon", note="prefill")])

    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    mirror.init_storage()
    primary_ids = mirror.append([_row(amount=5)])
    direct_primary_ids = primary.read_all().parsed
    assert direct_primary_ids[-1].row_index == primary_ids[0]


def test_init_storage_runs_on_both():
    primary = _make_sheets_ledger("p")
    secondary = _make_sheets_ledger("s")
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)

    mirror.init_storage()
    # Both should now have the Transactions tab.
    assert primary.sheets_backend.has_worksheet(primary.transactions_label)
    assert secondary.sheets_backend.has_worksheet(secondary.transactions_label)


def test_ensure_period_runs_on_both_returns_primary_info():
    primary = _make_sheets_ledger("p")
    secondary = _make_sheets_ledger("s")
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    mirror.init_storage()

    info = mirror.ensure_period(year=2026, month=5, categories=["Food"])
    assert info.created is True
    assert "May" in (info.name or "")
    # Both backends should now have the May 2026 tab.
    assert primary.sheets_backend.has_worksheet(info.name)
    assert secondary.sheets_backend.has_worksheet(info.name)


# ─── Reads stay on primary only ─────────────────────────────────────────


def test_read_all_only_reads_primary():
    primary = _make_sheets_ledger("p")
    secondary = _make_sheets_ledger("s")
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    mirror.init_storage()
    mirror.append([_row(amount=5)])

    # Manually inject an extra row into the secondary.  read_all
    # via mirror must NOT reflect it (primary is source-of-truth).
    secondary.append([_row(amount=99, category="Shopping", note="injected")])

    inspection = mirror.read_all()
    assert inspection.total_rows == 1
    assert inspection.parsed[0].amount == pytest.approx(5.0)


def test_get_last_returns_primary_only():
    primary = _make_sheets_ledger("p")
    secondary = _make_sheets_ledger("s")
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    mirror.init_storage()
    mirror.append([_row(amount=5, note="primary-row")])
    secondary.append([_row(amount=99, note="secondary-row")])

    last = mirror.get_last()
    assert last.is_empty is False
    assert last.value("note") == "primary-row"


# ─── Failure modes: secondary blows up, primary still wins ──────────────


def test_secondary_ledger_error_is_swallowed_and_logged(caplog):
    primary = _make_sheets_ledger("p")
    secondary = _BoomLedger(LedgerError("simulated supabase outage"))
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    primary.init_storage()  # only primary; mirror.init would also fail soft

    with caplog.at_level(logging.WARNING, logger="expense_tracker.ledger.mirror.adapter"):
        ids = mirror.append([_row(amount=5)])

    assert len(ids) == 1, "primary append must succeed regardless of secondary"
    assert primary.read_all().total_rows == 1
    assert any(
        "secondary backend boom failed" in r.message and "append" in r.message
        for r in caplog.records
    ), "expected a WARNING naming the secondary + the operation"


def test_secondary_unexpected_error_is_also_swallowed(caplog):
    """Belt-and-braces: even non-LedgerError exceptions must not break the user."""
    primary = _make_sheets_ledger("p")
    secondary = _BoomLedger(RuntimeError("network ICE candidate exhausted"))
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    primary.init_storage()

    with caplog.at_level(logging.WARNING, logger="expense_tracker.ledger.mirror.adapter"):
        ids = mirror.append([_row(amount=5)])

    assert ids == [2]  # row 1 = header, row 2 = first data row in fake sheets
    assert any("RuntimeError" in r.message for r in caplog.records)


def test_primary_failure_propagates_to_caller():
    """If the PRIMARY fails, the user MUST see it — that's the contract."""
    primary = _BoomLedger(LedgerError("primary down"))
    secondary = _make_sheets_ledger("s")
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)

    with pytest.raises(LedgerError, match="primary down"):
        mirror.append([_row(amount=5)])
    # Secondary must NOT be called — primary is gating.
    assert "append" not in secondary.read_all().parsed


def test_secondary_init_failure_does_not_break_startup(caplog):
    primary = _make_sheets_ledger("p")
    secondary = _BoomLedger(LedgerError("schema permission denied"))
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)

    with caplog.at_level(logging.WARNING, logger="expense_tracker.ledger.mirror.adapter"):
        mirror.init_storage()  # must not raise

    assert primary.sheets_backend.has_worksheet(primary.transactions_label)
    assert any("init_storage" in r.message for r in caplog.records)


# ─── Last-row operations mirror correctly ───────────────────────────────


def test_delete_last_runs_on_both():
    primary = _make_sheets_ledger("p")
    secondary = _make_sheets_ledger("s")
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    mirror.init_storage()
    mirror.append([_row(amount=5, note="first")])
    mirror.append([_row(amount=10, note="second")])

    snap = mirror.delete_last()
    assert snap.is_empty is False
    assert snap.value("note") == "second"
    assert primary.read_all().total_rows == 1
    assert secondary.read_all().total_rows == 1


def test_update_last_runs_on_both():
    primary = _make_sheets_ledger("p")
    secondary = _make_sheets_ledger("s")
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    mirror.init_storage()
    mirror.append([_row(amount=5, category="Saloon", note="shampoo-mistake")])

    pre_edit = mirror.update_last({"category": "Shopping"})
    assert pre_edit.value("category") == "Saloon"
    assert primary.read_all().parsed[-1].category == "Shopping"
    assert secondary.read_all().parsed[-1].category == "Shopping"


def test_delete_last_secondary_failure_does_not_break_chat(caplog):
    primary = _make_sheets_ledger("p")
    primary.init_storage()
    primary.append([_row(amount=5, note="first")])

    secondary = _BoomLedger(LedgerError("delete denied"))
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)

    with caplog.at_level(logging.WARNING, logger="expense_tracker.ledger.mirror.adapter"):
        snap = mirror.delete_last()

    assert snap.value("note") == "first"
    assert primary.read_all().total_rows == 0
    assert any("delete_last" in r.message for r in caplog.records)


# ─── Identity / properties ──────────────────────────────────────────────


def test_transactions_label_follows_primary():
    primary = _make_sheets_ledger("p")
    secondary = _make_sheets_ledger("s")
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    assert mirror.transactions_label == primary.transactions_label
    assert mirror.name == "mirror"


def test_primary_secondary_properties_expose_children():
    primary = _make_sheets_ledger("p")
    secondary = _make_sheets_ledger("s")
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    assert mirror.primary is primary
    assert mirror.secondary is secondary


# ─── Empty input edge cases ─────────────────────────────────────────────


def test_append_empty_no_secondary_call():
    """Empty append should be a free no-op — don't waste a Postgres
    connection on an empty list."""
    primary = _make_sheets_ledger("p")
    secondary = _BoomLedger(LedgerError("should never be called"))
    mirror = MirrorLedgerBackend(primary=primary, secondary=secondary)
    primary.init_storage()

    ids = mirror.append([])
    assert ids == []
    assert secondary.calls == []
