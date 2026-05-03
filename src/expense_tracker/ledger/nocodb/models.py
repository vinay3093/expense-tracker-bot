"""SQLAlchemy 2.0 typed models for the Postgres edition.

Two tables:

* ``transactions`` — the master ledger.  One row per logged expense,
  schema mirrors the universal :class:`TransactionRow` shape.  Soft
  deleted (``deleted_at`` timestamp) so ``/undo`` is reversible and
  every change leaves an audit trail.
* ``transactions_audit_log`` — append-only history of every insert /
  update / delete touching the ``transactions`` table.  Carries
  before / after JSON payloads so an admin can replay any change.

Why SQLAlchemy 2.0 typed Mapped columns
---------------------------------------

* IDE-friendly (``Transaction.amount`` autocompletes as ``Decimal``).
* Single source of truth — model fields drive Alembic migrations,
  ORM session inserts, and read-side ``.scalar()`` results.
* Cross-dialect: the same models work against Postgres in production
  and SQLite-in-memory in the test suite.  We use generic SQL types
  (``Numeric`` rather than ``NUMERIC``, ``JSON`` rather than
  ``JSONB``) so the dialect picks the right physical type.

Constraints + indexes are picked to match the chat pipeline's
access patterns:

* ``ix_transactions_date``                   → period queries.
* ``ix_transactions_category_date``          → category drill-downs.
* ``ix_transactions_year_month``             → monthly + YTD reports.
* ``ix_transactions_active``                 → ``WHERE deleted_at IS NULL``
  partial index, makes the hot read path index-only on Postgres.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# BIGSERIAL on Postgres, INTEGER PK on SQLite (so the test suite gets
# auto-increment for free).  Same Python-side ``int`` either way.
_BigPK = BigInteger().with_variant(Integer(), "sqlite")
_BigFK = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    """Single declarative base shared by every model in the edition.

    Centralising the base means Alembic's
    ``compare_type=True`` / ``compare_server_default=True`` autogen
    options pick up *every* model without any extra wiring.
    """


# ─── transactions ──────────────────────────────────────────────────────


class Transaction(Base):
    """One row in the master ledger — the Postgres analogue of a
    spreadsheet row in the Sheets edition's ``Transactions`` tab.

    All money columns use :class:`~decimal.Decimal` for exact
    arithmetic.  ``amount`` is the original amount in ``currency``;
    ``amount_usd`` is what every aggregation sums (already converted
    via :class:`~expense_tracker.ledger.sheets.currency.CurrencyConverter`
    before storage).
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(_BigPK, primary_key=True, autoincrement=True)

    # Expense info — what the user actually told us.
    date: Mapped[date_cls] = mapped_column(Date, nullable=False)
    day: Mapped[str] = mapped_column(String(8), nullable=False)
    """Short weekday name (``"Mon"``, ``"Tue"``, ...)."""
    month: Mapped[str] = mapped_column(String(20), nullable=False)
    """Full month name (``"April"``)."""
    year: Mapped[int] = mapped_column(nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Money — Decimal in Python; NUMERIC in Postgres; REAL in SQLite.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)

    # Provenance + audit metadata.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="chat",
    )
    """How the row entered the system: ``"chat"``, ``"cli"``, ``"manual"``."""
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """LLM call ID that produced this row (for debugging)."""
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """When the bot wrote the row (vs ``date``, the expense date)."""

    # Soft delete: ``deleted_at IS NULL`` means "active".  ``/undo``
    # sets the timestamp; the row stays on disk for auditability.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # ORM-side back-references.
    audit_entries: Mapped[list[AuditLog]] = relationship(
        "AuditLog", back_populates="transaction", cascade="all"
    )

    __table_args__ = (
        Index("ix_transactions_date", "date"),
        Index("ix_transactions_category_date", "category", "date"),
        Index("ix_transactions_year_month", "year", "month"),
        # Partial index — Postgres only; SQLite gracefully ignores
        # the ``postgresql_where`` kwarg on dialects that don't
        # support it (it just creates a non-partial equivalent).
        Index(
            "ix_transactions_active",
            "deleted_at",
            postgresql_where="deleted_at IS NULL",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<Transaction id={self.id} date={self.date} "
            f"category={self.category!r} amount_usd={self.amount_usd}>"
        )


# ─── transactions_audit_log ────────────────────────────────────────────


class AuditAction:
    """Allowed values for :attr:`AuditLog.action`.

    Plain string constants — kept out of an enum so admins can write
    ad-hoc rows from a SQL prompt without importing Python types.
    """

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    UNDELETE = "undelete"
    """Used when an undo is itself undone (future feature)."""


class AuditLog(Base):
    """Append-only history of every change to the ``transactions`` table.

    Each row carries the JSON payload of the row before and after the
    change (``old_values`` / ``new_values``), the actor that made the
    change (``"chat"``, ``"cli"``, ``"migration"``, ...), and the
    wall-clock time it happened.

    Why JSON columns rather than schema-shaped columns: the audit
    schema must survive every future schema change to the
    ``transactions`` table.  JSON keeps the audit log forward-compatible
    without ``ALTER TABLE`` cascades.
    """

    __tablename__ = "transactions_audit_log"

    id: Mapped[int] = mapped_column(_BigPK, primary_key=True, autoincrement=True)

    transaction_id: Mapped[int | None] = mapped_column(
        _BigFK,
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Source transaction.  Nullable because hard-deletes (rare; not
    issued by the chat path) leave the audit row pointing nowhere."""

    action: Mapped[str] = mapped_column(String(16), nullable=False)
    """One of :class:`AuditAction`'s constants."""

    old_values: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """Pre-change row payload (``None`` for inserts)."""

    new_values: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """Post-change row payload (``None`` for deletes)."""

    actor: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="system",
    )

    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    transaction: Mapped[Transaction | None] = relationship(
        "Transaction", back_populates="audit_entries"
    )

    __table_args__ = (
        Index("ix_audit_log_transaction_id", "transaction_id"),
        Index("ix_audit_log_at", "at"),
    )


__all__ = [
    "AuditAction",
    "AuditLog",
    "Base",
    "Transaction",
]
