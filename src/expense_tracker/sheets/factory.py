"""Settings → :class:`SheetsBackend` factory.

Mirrors :mod:`expense_tracker.llm.factory`: one entry point that takes
``Settings`` (or env) and returns a wired-up backend, lazy-importing
gspread only when the production backend is selected.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from .backend import FakeSheetsBackend, SheetsBackend
from .exceptions import SheetsConfigError


def get_sheets_backend(
    settings: Settings | None = None,
    *,
    fake: bool = False,
) -> SheetsBackend:
    """Return a backend ready to operate on the user's expense sheet.

    Args:
        settings: optional :class:`Settings` override (tests pass this).
        fake: if True, return an empty :class:`FakeSheetsBackend`. Useful
              for offline CLI experimentation: ``--build-month --fake``
              rebuilds the layout in-memory and prints a summary, no
              network involved.
    """
    cfg = settings or get_settings()

    if fake:
        return FakeSheetsBackend(spreadsheet_id="fake", title="Fake Spreadsheet")

    if not cfg.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise SheetsConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not set. Either point it at "
            "your service-account JSON file in .env, or pass fake=True "
            "for offline development."
        )
    if not cfg.EXPENSE_SHEET_ID:
        raise SheetsConfigError(
            "EXPENSE_SHEET_ID is not set. Set it to the long token between "
            "/spreadsheets/d/ and /edit in your Google Sheet URL."
        )

    from .gspread_backend import open_spreadsheet  # lazy

    return open_spreadsheet(
        service_account_path=cfg.GOOGLE_SERVICE_ACCOUNT_JSON,
        spreadsheet_id=cfg.EXPENSE_SHEET_ID,
        timeout_s=cfg.SHEETS_TIMEOUT_S,
    )
