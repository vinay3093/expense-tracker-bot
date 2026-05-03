"""Typed exceptions for the Google Sheets layer.

Same philosophy as :mod:`expense_tracker.llm.exceptions`: callers ONLY
need to catch our own hierarchy, never any underlying gspread /
google-auth class. Each subclass corresponds to one user-actionable
failure mode.

``SheetsError`` extends :class:`~expense_tracker.ledger.base.LedgerError`
so the chat pipeline can catch one base type regardless of which
storage backend is active (Sheets, Postgres, ...).
"""

from __future__ import annotations

from ..base import LedgerError


class SheetsError(LedgerError):
    """Base class for every error this layer raises."""


class SheetsConfigError(SheetsError):
    """Misconfigured environment — missing key, missing sheet ID, bad format."""


class SheetsAuthError(SheetsError):
    """Service-account JSON is invalid / revoked / not authorised for the sheet."""


class SheetsNotFoundError(SheetsError):
    """The spreadsheet or worksheet doesn't exist (or isn't shared with us)."""


class SheetsAlreadyExistsError(SheetsError):
    """A worksheet with the requested name already exists."""


class SheetsAPIError(SheetsError):
    """Sheets API returned an error we don't have a more specific class for."""


class SheetFormatError(SheetsError):
    """The sheet_format.yaml is malformed or violates an invariant."""
