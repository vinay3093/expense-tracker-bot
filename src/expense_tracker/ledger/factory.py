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
* ``STORAGE_BACKEND=mirror`` — dual-write edition.  Requires the
  config of *both* child backends (so DATABASE_URL + Sheets creds).
  See :mod:`expense_tracker.ledger.mirror`.

All three are interchangeable to the chat pipeline; pick whichever
fits your hosting + UX preference.

Why an explicit factory rather than a Protocol auto-discovery
plugin system?  We have a small, fixed set of backends and they're
shipped in the same repo.  A short ``if/elif`` is more debuggable
than a plugin registry, and the import-cost of the unused backend
is paid exactly once at module load (negligible — every backend
imports its heavy deps lazily).
"""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import LedgerBackend


def _build_single_backend(
    name: str,
    cfg: Settings,
    *,
    fake: bool,
) -> LedgerBackend:
    """Build one concrete backend by short name.

    Factored out of :func:`get_ledger_backend` so the mirror branch
    can request its two children without duplicating the constructor
    logic.
    """
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
        f"unknown ledger backend name {name!r}; "
        "expected one of: sheets, nocodb, postgres"
    )


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
            see :mod:`tests.test_ledger_postgres`).  Mirror mode
            forwards the flag to its Sheets child only.

    Raises:
        ValueError: if ``STORAGE_BACKEND`` is set to an unknown name,
            or if mirror mode is selected with primary == secondary.
        SheetsConfigError: if the Sheets edition is selected and its
            credentials / spreadsheet-ID settings are missing.
        PostgresLedgerError: if the Postgres edition is selected and
            ``DATABASE_URL`` is missing.
    """
    cfg = settings or get_settings()
    name = (cfg.STORAGE_BACKEND or "sheets").strip().lower()

    if name == "mirror":
        from .mirror.adapter import MirrorLedgerBackend

        primary_name = (cfg.MIRROR_PRIMARY or "sheets").strip().lower()
        secondary_name = (cfg.MIRROR_SECONDARY or "nocodb").strip().lower()
        if primary_name == secondary_name:
            raise ValueError(
                f"STORAGE_BACKEND=mirror requires MIRROR_PRIMARY "
                f"({primary_name!r}) and MIRROR_SECONDARY "
                f"({secondary_name!r}) to be different.  "
                "Pick a different secondary, or switch STORAGE_BACKEND "
                "to a single edition."
            )
        primary = _build_single_backend(primary_name, cfg, fake=fake)
        # Don't propagate ``fake`` to a Postgres secondary — it's
        # ignored there anyway, but being explicit keeps the test
        # path obvious.
        secondary_fake = fake if secondary_name == "sheets" else False
        secondary = _build_single_backend(
            secondary_name, cfg, fake=secondary_fake,
        )
        return MirrorLedgerBackend(primary=primary, secondary=secondary)

    return _build_single_backend(name, cfg, fake=fake)


__all__ = ["get_ledger_backend"]
