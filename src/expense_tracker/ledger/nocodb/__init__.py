"""Postgres + NocoDB edition of the ledger.

This package implements :class:`LedgerBackend` against a PostgreSQL
database (Supabase, RDS, local docker, ...) with NocoDB as an
optional UI layer on top.

Architecture
------------

Same chat / LLM / Telegram code path as the Sheets edition; only the
storage destination changes.  At runtime the bot picks this edition
when ``STORAGE_BACKEND=nocodb`` (or ``postgres``) and ``DATABASE_URL``
is set.

Tables
------

* ``transactions``  — the master ledger.  One row per logged
  expense.  Soft-deleted (``deleted_at`` timestamp) rather than
  physically removed so ``/undo`` is auditable.
* ``transactions_audit_log`` — append-only history of every insert,
  update, and delete on ``transactions``.  Each entry carries the
  before/after JSON payload so you can replay any change.

Indexes
-------

Tuned for the access patterns the chat pipeline actually issues:

* ``(date)``                   — date-range queries (summary, period).
* ``(category, date)``         — category drill-downs.
* ``(year, month)``            — monthly + YTD reports.
* ``(deleted_at) WHERE NULL``  — fast "active rows" filter.

Tests run against SQLite-in-memory (``DATABASE_URL=sqlite://``) so
the suite stays hermetic — no Postgres required for CI.
"""

from __future__ import annotations

__all__: list[str] = []
