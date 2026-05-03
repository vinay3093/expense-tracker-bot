"""Postgres edition of :class:`LedgerBackend`.

Implements the Protocol against a SQL database via SQLAlchemy 2.0.
The chat pipeline talks to this exactly the same way it talks to
:class:`SheetsLedgerBackend` — flip ``STORAGE_BACKEND`` and nothing
else changes.

Behaviour highlights
--------------------

* **Soft delete** — ``/undo`` doesn't physically remove the row; it
  sets ``deleted_at`` so a future "undo undo" can re-surface it.  All
  reads filter ``WHERE deleted_at IS NULL``.
* **Audit log** — every insert, update, and delete writes a row to
  ``transactions_audit_log`` with the before / after JSON.  Free
  forensic trail for the user — and the basis for any future
  "undo undo" / "show me edits this week" features.
* **No per-period setup** — :meth:`ensure_period` is a no-op; there
  are no monthly tabs to create.  :meth:`recompute_period` is also a
  no-op; aggregations are SQL queries, not formula caches.
* **Cross-dialect** — works against Postgres in production *and*
  SQLite in-memory in the test suite.  Generic SQLAlchemy types only,
  no Postgres-specific syntax.
"""

from __future__ import annotations

import calendar
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from ..base import (
    BackendHealth,
    LastRow,
    LedgerInspection,
    LedgerRow,
    PeriodInfo,
    TransactionRow,
)
from .exceptions import (
    PostgresConnectionError,
    PostgresLedgerError,
    PostgresSchemaError,
)
from .models import AuditAction, AuditLog, Base, Transaction

_log = logging.getLogger(__name__)


# ─── Backend ────────────────────────────────────────────────────────────


class PostgresLedgerBackend:
    """Postgres + NocoDB implementation of :class:`LedgerBackend`.

    Construct once per process via
    :func:`expense_tracker.ledger.factory.get_ledger_backend` (which
    in turn pulls the engine from
    :func:`expense_tracker.ledger.nocodb.factory.get_engine`).

    All public methods open a short-lived :class:`Session`,  do their
    work in a single transaction, and commit on success / roll back
    on error.  No long-lived ORM identity maps — keeps memory flat
    even for large reads.
    """

    name = "postgres"
    _TABLE_NAME = Transaction.__tablename__

    def __init__(
        self,
        *,
        engine: Engine,
        actor: str = "chat",
    ) -> None:
        """Wire the backend to an existing engine.

        Args:
            engine: pre-configured SQLAlchemy engine (built by
                :func:`get_engine`).  Sharing a single engine across
                the process is important for connection pooling.
            actor: who's writing — recorded on every audit row.
                CLI commands pass ``"cli"``; the migration script
                passes ``"migration"``; default ``"chat"`` matches
                the chat / Telegram entry point.
        """
        self._engine = engine
        self._actor = actor
        self._Session = sessionmaker(bind=engine, expire_on_commit=False)

    @property
    def transactions_label(self) -> str:
        return self._TABLE_NAME

    @property
    def engine(self) -> Engine:
        """Underlying SQLAlchemy engine — exposed for admin scripts."""
        return self._engine

    # ─── Lifecycle ─────────────────────────────────────────────────────

    def health_check(self) -> BackendHealth:
        """Cheap connectivity ping.  ``SELECT 1`` against the engine."""
        start = time.perf_counter()
        try:
            with self._engine.connect() as conn:
                version = conn.exec_driver_sql("SELECT 1").scalar()
        except OperationalError as exc:
            return BackendHealth(
                ok=False,
                backend=self.name,
                latency_ms=(time.perf_counter() - start) * 1000,
                detail=f"unreachable: {exc}",
            )
        return BackendHealth(
            ok=True,
            backend=self.name,
            latency_ms=(time.perf_counter() - start) * 1000,
            detail=f"select1={version}",
        )

    def init_storage(self) -> None:
        """Create the schema if it doesn't exist.  Idempotent.

        For production deploys, prefer ``alembic upgrade head`` (the
        ``expense --init-postgres`` CLI command runs that under the
        hood).  This method exists so the test suite can build the
        schema in-memory in one line.
        """
        Base.metadata.create_all(self._engine)

    def ensure_period(
        self,
        *,
        year: int,
        month: int,
        categories: Sequence[str],
    ) -> PeriodInfo:
        """No-op in the Postgres edition.

        Postgres has no per-month physical containers — one
        ``transactions`` table holds every year.  Returns
        ``PeriodInfo(name=None, created=False)`` so the chat reply
        doesn't try to mention a "monthly tab".
        """
        return PeriodInfo(name=None, created=False)

    # ─── Write side ────────────────────────────────────────────────────

    def append(self, rows: Sequence[TransactionRow]) -> list[int]:
        """Insert rows + emit an INSERT audit entry per row.

        Atomic: either every row + every audit entry land or none do.
        Returns the assigned ``id`` for each row, in input order.
        """
        if not rows:
            return []
        try:
            with self._Session() as session, session.begin():
                orm_rows = [_transaction_from_row(r) for r in rows]
                session.add_all(orm_rows)
                session.flush()  # populate auto-incremented IDs.
                ids = [r.id for r in orm_rows]
                # One audit entry per insert, with the as-stored payload.
                session.add_all(
                    AuditLog(
                        transaction_id=r.id,
                        action=AuditAction.INSERT,
                        old_values=None,
                        new_values=_serialise_transaction(r),
                        actor=self._actor,
                    )
                    for r in orm_rows
                )
                return ids
        except SQLAlchemyError as exc:
            raise PostgresLedgerError(f"append failed: {exc}") from exc

    def recompute_period(
        self,
        *,
        year: int,
        month: int,
        categories: Sequence[str],
    ) -> str | None:
        """No-op in the Postgres edition (queries are live; nothing to refresh)."""
        return None

    # ─── Read side ─────────────────────────────────────────────────────

    def read_all(
        self,
        *,
        collect_skipped_detail: bool = False,
    ) -> LedgerInspection:
        """Return every active transaction as :class:`LedgerRow`.

        Soft-deleted rows (``deleted_at IS NOT NULL``) are excluded.
        ``skipped`` is always empty for the Postgres edition — the
        DB enforces typed columns, so there's no "unparseable cell"
        category here.  The ``collect_skipped_detail`` flag is
        accepted for Protocol parity but ignored.
        """
        try:
            with self._Session() as session:
                stmt = (
                    select(Transaction)
                    .where(Transaction.deleted_at.is_(None))
                    .order_by(Transaction.date.asc(), Transaction.id.asc())
                )
                txns = session.scalars(stmt).all()
        except OperationalError as exc:
            raise PostgresConnectionError(
                f"failed to read {self._TABLE_NAME!r}: {exc}",
            ) from exc
        except SQLAlchemyError as exc:
            # Most likely "no such table" before init_storage() runs.
            if "no such table" in str(exc).lower() or "does not exist" in str(exc).lower():
                raise PostgresSchemaError(
                    f"table {self._TABLE_NAME!r} doesn't exist — run "
                    "`expense --init-postgres` first.",
                ) from exc
            raise PostgresLedgerError(f"read failed: {exc}") from exc

        return LedgerInspection(
            sheet_name=self._TABLE_NAME,
            parsed=[_ledger_row_from_orm(t) for t in txns],
            skipped=[],
        )

    # ─── Last-row operations (for /undo and /edit) ─────────────────────

    def get_last(self) -> LastRow:
        """Snapshot of the most-recently-written ACTIVE transaction.

        "Most recent" = highest ``id`` among rows with
        ``deleted_at IS NULL``.  ``id`` is monotonic per insert, so
        this matches the Sheets edition's "bottom-most row" semantics
        regardless of how the user backdates ``date``.
        """
        try:
            with self._Session() as session:
                stmt = (
                    select(Transaction)
                    .where(Transaction.deleted_at.is_(None))
                    .order_by(Transaction.id.desc())
                    .limit(1)
                )
                txn = session.scalars(stmt).first()
        except SQLAlchemyError as exc:
            raise PostgresLedgerError(f"get_last failed: {exc}") from exc

        if txn is None:
            return LastRow(is_empty=True, row_index=None, values={})
        return _last_row_from_orm(txn)

    def delete_last(self) -> LastRow:
        """Soft-delete the most recent transaction.

        Returns the pre-delete snapshot.  The row stays on disk with
        ``deleted_at`` set so future audit / undo-undo flows can
        reference it.  Audit log records the operation.
        """
        try:
            with self._Session() as session, session.begin():
                stmt = (
                    select(Transaction)
                    .where(Transaction.deleted_at.is_(None))
                    .order_by(Transaction.id.desc())
                    .limit(1)
                    .with_for_update(of=Transaction)
                ) if self._engine.dialect.name == "postgresql" else (
                    select(Transaction)
                    .where(Transaction.deleted_at.is_(None))
                    .order_by(Transaction.id.desc())
                    .limit(1)
                )
                txn = session.scalars(stmt).first()
                if txn is None:
                    return LastRow(is_empty=True, row_index=None, values={})

                snapshot = _last_row_from_orm(txn)
                old_values = _serialise_transaction(txn)
                txn.deleted_at = datetime.now(tz=timezone.utc)
                session.add(
                    AuditLog(
                        transaction_id=txn.id,
                        action=AuditAction.DELETE,
                        old_values=old_values,
                        new_values=None,
                        actor=self._actor,
                    ),
                )
                return snapshot
        except SQLAlchemyError as exc:
            raise PostgresLedgerError(f"delete_last failed: {exc}") from exc

    def update_last(self, updates: dict[str, Any]) -> LastRow:
        """Patch named fields on the most recent active transaction.

        ``updates`` keys are canonical column names (``"category"``,
        ``"amount"``, ``"amount_usd"``, ``"fx_rate"``, ...).  Only
        whitelisted fields are accepted — passing an unknown key is
        a no-op (defensive against caller bugs).  Returns the pre-edit
        snapshot.  Audit log records the change.
        """
        editable = {
            "category", "note", "vendor",
            "amount", "currency", "amount_usd", "fx_rate",
            "date", "day", "month", "year",
        }
        clean_updates = {k: v for k, v in updates.items() if k in editable}

        try:
            with self._Session() as session, session.begin():
                stmt = (
                    select(Transaction)
                    .where(Transaction.deleted_at.is_(None))
                    .order_by(Transaction.id.desc())
                    .limit(1)
                )
                txn = session.scalars(stmt).first()
                if txn is None:
                    return LastRow(is_empty=True, row_index=None, values={})

                snapshot = _last_row_from_orm(txn)
                old_values = _serialise_transaction(txn)
                for key, value in clean_updates.items():
                    setattr(txn, key, _coerce_for_column(key, value))

                if clean_updates:
                    session.add(
                        AuditLog(
                            transaction_id=txn.id,
                            action=AuditAction.UPDATE,
                            old_values=old_values,
                            new_values=_serialise_transaction(txn),
                            actor=self._actor,
                        ),
                    )
                return snapshot
        except SQLAlchemyError as exc:
            raise PostgresLedgerError(f"update_last failed: {exc}") from exc

    # ─── Helpful diagnostics for admin scripts (not on the Protocol) ───

    def count_active(self) -> int:
        """How many active rows are in the ledger.  For ``--healthcheck``."""
        try:
            with self._Session() as session:
                return session.scalar(
                    select(func.count(Transaction.id)).where(
                        Transaction.deleted_at.is_(None),
                    ),
                ) or 0
        except SQLAlchemyError as exc:
            raise PostgresLedgerError(f"count_active failed: {exc}") from exc

    def schema_present(self) -> bool:
        """``True`` iff both expected tables exist in the database."""
        insp = inspect(self._engine)
        names = set(insp.get_table_names())
        return {Transaction.__tablename__, AuditLog.__tablename__}.issubset(names)


# ─── ORM <-> universal data-shape conversions ──────────────────────────


def _transaction_from_row(row: TransactionRow) -> Transaction:
    """Build an ORM :class:`Transaction` from the universal write
    shape.  All money values are converted to :class:`Decimal` for
    exact arithmetic (the chat pipeline still uses floats — Decimal
    stays inside the storage layer)."""
    ts = row.timestamp or datetime.now(tz=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return Transaction(
        date=row.date,
        day=row.day,
        month=row.month,
        year=row.year,
        category=row.category,
        note=row.note,
        vendor=row.vendor,
        amount=Decimal(str(row.amount)),
        currency=row.currency.upper(),
        amount_usd=Decimal(str(row.amount_usd)),
        fx_rate=Decimal(str(row.fx_rate)),
        source=row.source,
        trace_id=row.trace_id,
        timestamp=ts,
        deleted_at=None,
    )


def _ledger_row_from_orm(t: Transaction) -> LedgerRow:
    """Project an ORM row into the universal read shape."""
    return LedgerRow(
        row_index=int(t.id),
        date=t.date,
        day=t.day,
        month=t.month,
        year=int(t.year),
        category=t.category,
        note=t.note,
        vendor=t.vendor,
        amount=float(t.amount),
        currency=t.currency,
        amount_usd=float(t.amount_usd),
        fx_rate=float(t.fx_rate),
        source=t.source,
        trace_id=t.trace_id,
        timestamp=t.timestamp,
    )


def _last_row_from_orm(t: Transaction) -> LastRow:
    """Project an ORM row into the universal :class:`LastRow` shape.

    Money values become floats here so the chat reply formatter
    (which already speaks float) doesn't need to know about Decimal.
    """
    return LastRow(
        is_empty=False,
        row_index=int(t.id),
        values={
            "date": t.date.isoformat(),
            "day": t.day,
            "month": t.month,
            "year": int(t.year),
            "category": t.category,
            "note": t.note or "",
            "vendor": t.vendor or "",
            "amount": float(t.amount),
            "currency": t.currency,
            "amount_usd": float(t.amount_usd),
            "fx_rate": float(t.fx_rate),
            "source": t.source,
            "trace_id": t.trace_id or "",
            "timestamp": t.timestamp.isoformat() if t.timestamp else "",
        },
    )


def _serialise_transaction(t: Transaction) -> dict[str, Any]:
    """JSON-friendly snapshot of an ORM row, for the audit log."""
    return {
        "id": int(t.id),
        "date": t.date.isoformat(),
        "day": t.day,
        "month": t.month,
        "year": int(t.year),
        "category": t.category,
        "note": t.note,
        "vendor": t.vendor,
        "amount": str(t.amount),
        "currency": t.currency,
        "amount_usd": str(t.amount_usd),
        "fx_rate": str(t.fx_rate),
        "source": t.source,
        "trace_id": t.trace_id,
        "timestamp": t.timestamp.isoformat() if t.timestamp else None,
        "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
    }


def _coerce_for_column(key: str, value: Any) -> Any:
    """Coerce a chat-side value into the right type for an ORM column.

    The chat pipeline passes floats for money, ISO strings for dates,
    integers for years, and plain strings for everything else.
    Postgres / SQLAlchemy expect Decimal for ``Numeric`` columns and
    ``date`` objects for ``Date`` columns — so we translate here
    rather than asking every caller to remember.
    """
    if key in ("amount", "amount_usd", "fx_rate"):
        return Decimal(str(value))
    if key == "date":
        if isinstance(value, str):
            from datetime import date as _d
            return _d.fromisoformat(value)
    if key == "year":
        return int(value)
    if key == "currency" and isinstance(value, str):
        return value.upper()
    return value


# Used by the migration script to recompute calendar fields from a
# date object — keeps "April"/"Mon"/2026 in sync with the date.
def derived_calendar_fields(d: Any) -> dict[str, Any]:
    """``date -> {day, month, year}`` for inserts that only carry ``date``."""
    return {
        "day": d.strftime("%a"),
        "month": calendar.month_name[d.month],
        "year": d.year,
    }


__all__ = [
    "PostgresLedgerBackend",
    "derived_calendar_fields",
]
