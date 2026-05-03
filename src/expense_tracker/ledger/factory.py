"""Factory: build the right :class:`LedgerBackend` from settings.

This is the single switch point for "which storage edition is the
bot running against?".  Every other module in the codebase asks for
a :class:`LedgerBackend` and gets back whichever concrete
implementation matches the user's ``STORAGE_BACKEND`` setting.

Selection rules
---------------

* ``STORAGE_BACKEND=sheets`` (default) — Google Sheets edition.
  Requires the usual ``GOOGLE_APPLICATION_CREDENTIALS`` +
  ``GOOGLE_SHEETS_SPREADSHEET_ID`` settings.
* ``STORAGE_BACKEND=nocodb`` (or ``postgres``) — Postgres + NocoDB
  edition.  Requires ``DATABASE_URL`` (Supabase / local docker
  Postgres / RDS / wherever).

The two are interchangeable to the chat pipeline; pick whichever
fits your hosting + UX preference.

Why an explicit factory rather than a Protocol auto-discovery
plugin system?  We have exactly two backends and they're shipped in
the same repo.  A 30-line ``if/elif`` is more debuggable than a
plugin registry, and the import-cost of the unused backend is paid
exactly once at module load (negligible — both backends import
their heavy deps lazily).
"""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import LedgerBackend


def get_ledger_backend(
    settings: Settings | None = None,
    *,
    fake: bool = False,
) -> LedgerBackend:
    """Build the :class:`LedgerBackend` selected by ``settings.STORAGE_BACKEND``.

    Args:
        settings: optional pre-built settings.  Defaults to
            :func:`get_settings()` so callers can stay terse.
        fake: only honoured by the Sheets edition — returns an
            in-memory :class:`FakeSheetsBackend`.  Ignored by the
            Postgres edition (use SQLite-in-memory tests instead;
            see :mod:`tests.test_ledger_postgres`).

    Raises:
        ValueError: if ``STORAGE_BACKEND`` is set to an unknown name.
        SheetsConfigError: if the Sheets edition is selected and its
            credentials / spreadsheet-ID settings are missing.
        PostgresLedgerError: if the Postgres edition is selected and
            ``DATABASE_URL`` is missing.
    """
    cfg = settings or get_settings()
    name = (cfg.STORAGE_BACKEND or "sheets").strip().lower()

    if name == "sheets":
        from .sheets.adapter import SheetsLedgerBackend
        from .sheets.factory import get_sheets_backend
        from .sheets.format import get_sheet_format

        return SheetsLedgerBackend(
            backend=get_sheets_backend(cfg, fake=fake),
            sheet_format=get_sheet_format(),
        )

    if name in {"nocodb", "postgres"}:
        from .nocodb.adapter import PostgresLedgerBackend
        from .nocodb.factory import get_engine

        return PostgresLedgerBackend(engine=get_engine(cfg))

    raise ValueError(
        f"unknown STORAGE_BACKEND={name!r}; "
        "expected one of: sheets, nocodb, postgres"
    )


__all__ = ["get_ledger_backend"]
