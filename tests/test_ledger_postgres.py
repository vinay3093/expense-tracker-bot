"""Unit tests for :class:`PostgresLedgerBackend`.

Strategy
--------

Run every test against an **in-memory SQLite** database via a shared
:class:`StaticPool`.  This gives us:

* Hermetic CI — no Postgres server required.
* Realistic SQL semantics (transactions, FK constraints, autoincrement).
* Sub-second test runs.

Anywhere the production Postgres edition would behave *differently*
from SQLite (partial indexes, ``SELECT FOR UPDATE`` semantics) we
either guard with a dialect check inside the adapter or add an
integration test gated behind a real ``DATABASE_URL`` env var.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from expense_tracker.ledger.base import LedgerBackend, TransactionRow
from expense_tracker.ledger.nocodb.adapter import (
    PostgresLedgerBackend,
    derived_calendar_fields,
)
from expense_tracker.ledger.nocodb.exceptions import (
    PostgresConfigError,
    PostgresLedgerError,
)
from expense_tracker.ledger.nocodb.factory import get_engine
from expense_tracker.ledger.nocodb.models import (
    AuditAction,
    AuditLog,
    Transaction,
)

# ─── Helpers ────────────────────────────────────────────────────────────


def _engine():
    """Single-connection SQLite — the only safe ``:memory:`` shape."""
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _ledger(actor: str = "test") -> PostgresLedgerBackend:
    backend = PostgresLedgerBackend(engine=_engine(), actor=actor)
    backend.init_storage()
    return backend


def _row(
    *,
    d: date = date(2026, 4, 24),
    amount: float = 12.50,
    category: str = "Food",
    currency: str = "USD",
    amount_usd: float | None = None,
    note: str | None = "lunch",
    vendor: str | None = "Cafe",
    trace: str = "t1",
) -> TransactionRow:
    return TransactionRow(
        date=d,
        day=d.strftime("%a"),
        month=d.strftime("%B"),
        year=d.year,
        category=category,
        note=note,
        vendor=vendor,
        amount=amount,
        currency=currency,
        amount_usd=amount_usd if amount_usd is not None else amount,
        fx_rate=1.0,
        source="chat",
        trace_id=trace,
        timestamp=datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc),
    )


# ─── Identity / metadata ────────────────────────────────────────────────


def test_backend_identifies_itself_as_postgres():
    ledger = _ledger()
    assert ledger.name == "postgres"
    assert ledger.transactions_label == "transactions"


def test_satisfies_ledger_backend_protocol_at_runtime():
    """isinstance against the runtime_checkable Protocol."""
    ledger = _ledger()
    assert isinstance(ledger, LedgerBackend)


# ─── Lifecycle ──────────────────────────────────────────────────────────


def test_init_storage_creates_both_tables():
    backend = PostgresLedgerBackend(engine=_engine(), actor="t")
    assert backend.schema_present() is False
    backend.init_storage()
    assert backend.schema_present() is True


def test_init_storage_is_idempotent():
    backend = PostgresLedgerBackend(engine=_engine(), actor="t")
    backend.init_storage()
    backend.init_storage()  # should not raise.
    assert backend.schema_present() is True


def test_health_check_reports_ok_against_live_engine():
    health = _ledger().health_check()
    assert health.ok is True
    assert health.backend == "postgres"
    assert health.latency_ms >= 0


def test_ensure_period_is_a_no_op_returning_no_period_name():
    info = _ledger().ensure_period(year=2026, month=4, categories=["Food"])
    assert info.name is None
    assert info.created is False


def test_recompute_period_is_a_no_op_returning_none():
    assert _ledger().recompute_period(
        year=2026, month=4, categories=["Food"],
    ) is None


# ─── Append + read ──────────────────────────────────────────────────────


def test_append_returns_assigned_ids_in_order():
    ledger = _ledger()
    ids = ledger.append([
        _row(d=date(2026, 4, 24), amount=10.0),
        _row(d=date(2026, 4, 25), amount=20.0, category="Saloon"),
        _row(d=date(2026, 4, 26), amount=30.0, category="Tesla Car"),
    ])
    assert ids == [1, 2, 3]


def test_append_empty_list_returns_empty_list_and_no_writes():
    ledger = _ledger()
    assert ledger.append([]) == []
    assert ledger.count_active() == 0


def test_append_writes_one_audit_row_per_inserted_transaction():
    ledger = _ledger()
    ledger.append([_row(amount=10.0), _row(amount=20.0, category="Saloon")])
    with ledger._Session() as sess:
        actions = sess.scalars(
            select(AuditLog.action).order_by(AuditLog.id),
        ).all()
    assert actions == [AuditAction.INSERT, AuditAction.INSERT]


def test_append_audit_carries_actor_label():
    ledger = _ledger(actor="cli")
    ledger.append([_row()])
    with ledger._Session() as sess:
        actor = sess.scalar(select(AuditLog.actor))
    assert actor == "cli"


def test_read_all_returns_active_rows_sorted_by_date_then_id():
    ledger = _ledger()
    ledger.append([
        _row(d=date(2026, 4, 26), amount=30.0),
        _row(d=date(2026, 4, 24), amount=10.0),
        _row(d=date(2026, 4, 25), amount=20.0),
    ])
    inspection = ledger.read_all()
    parsed = inspection.parsed
    assert [r.date for r in parsed] == [
        date(2026, 4, 24), date(2026, 4, 25), date(2026, 4, 26),
    ]
    assert inspection.skipped == []  # SQL never produces skipped rows.


def test_read_all_excludes_soft_deleted_rows():
    ledger = _ledger()
    ledger.append([_row(amount=10.0), _row(amount=20.0, category="Saloon")])
    ledger.delete_last()  # soft-delete the Saloon row
    parsed = ledger.read_all().parsed
    assert len(parsed) == 1
    assert parsed[0].category == "Food"


def test_read_all_returns_empty_inspection_when_no_rows():
    inspection = _ledger().read_all()
    assert inspection.parsed == []
    assert inspection.skipped == []


# ─── get_last / delete_last (undo) ──────────────────────────────────────


def test_get_last_on_empty_ledger_returns_empty_snapshot():
    snap = _ledger().get_last()
    assert snap.is_empty is True
    assert snap.row_index is None


def test_get_last_returns_highest_id_active_row():
    ledger = _ledger()
    ledger.append([
        _row(d=date(2026, 4, 24), amount=10.0),
        _row(d=date(2026, 4, 24), amount=20.0, category="Saloon"),
    ])
    snap = ledger.get_last()
    assert snap.row_index == 2
    assert snap.value("category") == "Saloon"


def test_get_last_skips_already_soft_deleted_rows():
    ledger = _ledger()
    ledger.append([_row(amount=10.0), _row(amount=20.0, category="Saloon")])
    ledger.delete_last()
    snap = ledger.get_last()
    assert snap.row_index == 1
    assert snap.value("category") == "Food"


def test_delete_last_returns_pre_delete_snapshot():
    ledger = _ledger()
    ledger.append([_row(amount=10.0, category="Food")])
    snap = ledger.delete_last()
    assert snap.is_empty is False
    assert snap.value("category") == "Food"
    assert snap.value("amount") == pytest.approx(10.0)


def test_delete_last_marks_row_soft_deleted_not_hard_deleted():
    ledger = _ledger()
    ledger.append([_row()])
    ledger.delete_last()
    with ledger._Session() as sess:
        rows = sess.scalars(select(Transaction)).all()
    assert len(rows) == 1
    assert rows[0].deleted_at is not None


def test_delete_last_records_a_delete_audit_entry_with_old_values():
    ledger = _ledger()
    ledger.append([_row(category="Food", amount=42.0)])
    ledger.delete_last()
    with ledger._Session() as sess:
        delete_audit = sess.scalar(
            select(AuditLog).where(AuditLog.action == AuditAction.DELETE),
        )
    assert delete_audit is not None
    assert delete_audit.old_values is not None
    assert delete_audit.old_values["category"] == "Food"
    assert delete_audit.new_values is None


def test_delete_last_on_empty_ledger_is_a_noop_returning_empty():
    snap = _ledger().delete_last()
    assert snap.is_empty is True


# ─── update_last (edit) ─────────────────────────────────────────────────


def test_update_last_patches_only_whitelisted_fields():
    ledger = _ledger()
    ledger.append([_row(category="Saloon", amount=100.0, amount_usd=100.0)])
    ledger.update_last({
        "category": "Shopping",
        "amount": 50.0,
        "amount_usd": 50.0,
        "fx_rate": 1.0,
        "ignored_garbage": "should not crash",
    })
    snap = ledger.get_last()
    assert snap.value("category") == "Shopping"
    assert snap.value("amount") == pytest.approx(50.0)


def test_update_last_returns_pre_edit_snapshot():
    ledger = _ledger()
    ledger.append([_row(category="Saloon", amount=100.0)])
    snap = ledger.update_last({"category": "Shopping"})
    assert snap.value("category") == "Saloon"  # the BEFORE value


def test_update_last_writes_an_update_audit_with_old_and_new_values():
    ledger = _ledger()
    ledger.append([_row(category="Saloon")])
    ledger.update_last({"category": "Shopping"})
    with ledger._Session() as sess:
        upd = sess.scalar(
            select(AuditLog).where(AuditLog.action == AuditAction.UPDATE),
        )
    assert upd is not None
    assert upd.old_values["category"] == "Saloon"
    assert upd.new_values["category"] == "Shopping"


def test_update_last_on_empty_ledger_is_a_noop_returning_empty():
    snap = _ledger().update_last({"category": "Whatever"})
    assert snap.is_empty is True


# ─── Edge cases / error wrapping ────────────────────────────────────────


def test_money_columns_round_trip_as_floats_for_chat_layer():
    ledger = _ledger()
    ledger.append([_row(amount=12.34, amount_usd=12.34, currency="USD")])
    snap = ledger.get_last()
    assert isinstance(snap.value("amount"), float)
    assert snap.value("amount") == pytest.approx(12.34)


def test_currency_uppercased_on_insert():
    ledger = _ledger()
    ledger.append([_row(currency="usd")])
    snap = ledger.get_last()
    assert snap.value("currency") == "USD"


def test_derived_calendar_fields_helper_for_migration():
    """Used by the migration script to stamp Day / Month / Year on rows
    that only carry a Date (e.g. legacy CSV imports)."""
    out = derived_calendar_fields(date(2026, 4, 24))
    assert out["day"] == "Fri"
    assert out["month"] == "April"
    assert out["year"] == 2026


def test_underlying_sqlalchemy_error_wraps_into_typed_ledger_error(monkeypatch):
    """SQLAlchemyError must surface as PostgresLedgerError so the
    chat pipeline catches one type."""
    ledger = _ledger()

    def _boom(self):
        from sqlalchemy.exc import SQLAlchemyError
        raise SQLAlchemyError("simulated DB outage")

    monkeypatch.setattr(
        "sqlalchemy.orm.Session.flush", _boom,
    )
    with pytest.raises(PostgresLedgerError):
        ledger.append([_row()])


# ─── Factory ────────────────────────────────────────────────────────────


def test_get_engine_raises_config_error_when_database_url_missing():
    """Postgres edition is selected by env; missing URL must fail loud."""
    from expense_tracker.config import Settings
    cfg = Settings(STORAGE_BACKEND="nocodb", DATABASE_URL=None)
    with pytest.raises(PostgresConfigError):
        get_engine(cfg)


def test_get_engine_raises_config_error_when_database_url_empty():
    from pydantic import SecretStr

    from expense_tracker.config import Settings
    cfg = Settings(STORAGE_BACKEND="nocodb", DATABASE_URL=SecretStr(""))
    with pytest.raises(PostgresConfigError):
        get_engine(cfg)


def test_get_engine_accepts_sqlite_memory_url():
    from pydantic import SecretStr

    from expense_tracker.config import Settings
    cfg = Settings(STORAGE_BACKEND="nocodb", DATABASE_URL=SecretStr("sqlite://"))
    engine = get_engine(cfg)
    assert engine.dialect.name == "sqlite"
