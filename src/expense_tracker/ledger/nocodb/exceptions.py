"""Typed exceptions for the Postgres + NocoDB edition.

Same philosophy as the Sheets edition's
:class:`~expense_tracker.ledger.sheets.exceptions.SheetsError`:
callers ONLY need to catch :class:`PostgresLedgerError` (or its base
:class:`LedgerError`), never any underlying SQLAlchemy / psycopg
class.
"""

from __future__ import annotations

from ..base import LedgerError


class PostgresLedgerError(LedgerError):
    """Base class for every error this edition raises."""


class PostgresConfigError(PostgresLedgerError):
    """Misconfigured environment — missing ``DATABASE_URL``, bad URL, etc."""


class PostgresConnectionError(PostgresLedgerError):
    """The database is unreachable or the connection was refused."""


class PostgresSchemaError(PostgresLedgerError):
    """The schema is missing or out-of-date — run ``expense --init-postgres``."""


__all__ = [
    "PostgresConfigError",
    "PostgresConnectionError",
    "PostgresLedgerError",
    "PostgresSchemaError",
]
