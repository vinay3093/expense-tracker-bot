"""Initial schema for the Postgres + NocoDB edition.

Creates two tables:

* ``transactions``           — master ledger.
* ``transactions_audit_log`` — append-only history.

Plus the indexes the chat pipeline actually queries against.
Mirrors :mod:`expense_tracker.ledger.nocodb.models` exactly; if you
add a column there, generate a follow-up migration with::

    alembic -c alembic.ini revision --autogenerate -m "add foo column"

and review the diff before committing.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-03
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("day", sa.String(length=8), nullable=False),
        sa.Column("month", sa.String(length=20), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("vendor", sa.String(length=128), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("fx_rate", sa.Numeric(14, 6), nullable=False),
        sa.Column(
            "source", sa.String(length=16), nullable=False,
            server_default="chat",
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "timestamp", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_transactions_date", "transactions", ["date"])
    op.create_index(
        "ix_transactions_category_date", "transactions", ["category", "date"],
    )
    op.create_index(
        "ix_transactions_year_month", "transactions", ["year", "month"],
    )
    op.create_index(
        "ix_transactions_active", "transactions", ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "transactions_audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("old_values", sa.JSON(), nullable=True),
        sa.Column("new_values", sa.JSON(), nullable=True),
        sa.Column(
            "actor", sa.String(length=64), nullable=False,
            server_default="system",
        ),
        sa.Column(
            "at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"], ["transactions.id"], ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_audit_log_transaction_id", "transactions_audit_log",
        ["transaction_id"],
    )
    op.create_index("ix_audit_log_at", "transactions_audit_log", ["at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_at", table_name="transactions_audit_log")
    op.drop_index(
        "ix_audit_log_transaction_id", table_name="transactions_audit_log",
    )
    op.drop_table("transactions_audit_log")
    op.drop_index("ix_transactions_active", table_name="transactions")
    op.drop_index("ix_transactions_year_month", table_name="transactions")
    op.drop_index("ix_transactions_category_date", table_name="transactions")
    op.drop_index("ix_transactions_date", table_name="transactions")
    op.drop_table("transactions")
