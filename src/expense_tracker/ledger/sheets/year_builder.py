"""Bulk year-setup helpers.

One sentence in chat — *"make arrangement for 2027"* — should map to
:func:`setup_year`, which provisions:

  * 12 monthly tabs (``January 2027`` … ``December 2027``)
  * 1 YTD tab (``YTD 2027``)
  * Optionally hides all monthly tabs from the previous year so the
    worksheet bar stays uncluttered.

The Transactions tab is created on demand by :func:`init_transactions_tab`
the first time a row is appended; this module assumes it's either there
or will be there shortly.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass

from .backend import SheetsBackend, WorksheetHandle
from .exceptions import SheetsAlreadyExistsError, SheetsNotFoundError
from .format import SheetFormat
from .month_builder import build_month_tab
from .ytd_builder import build_ytd_tab


@dataclass(frozen=True)
class YearSetupReport:
    """Summary of what :func:`setup_year` did. Used by the CLI."""

    year: int
    months_created: list[str]
    months_skipped: list[str]      # already existed and overwrite=False
    ytd_tab: str
    ytd_overwritten: bool
    previous_year_hidden: list[str]

    def short_summary(self) -> str:
        parts = [
            f"{len(self.months_created)} monthly tabs created",
        ]
        if self.months_skipped:
            parts.append(f"{len(self.months_skipped)} skipped (already existed)")
        parts.append("YTD " + ("rebuilt" if self.ytd_overwritten else "created"))
        if self.previous_year_hidden:
            parts.append(f"{len(self.previous_year_hidden)} previous-year tabs hidden")
        return ", ".join(parts)


def setup_year(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    *,
    year: int,
    categories: list[str],
    overwrite: bool = False,
    hide_previous: bool = False,
) -> YearSetupReport:
    """Build all 12 monthly tabs + YTD for ``year``.

    Args:
        overwrite: if True, existing monthly / YTD tabs for this year
                   are deleted and rebuilt. If False, existing tabs are
                   left in place and listed in ``months_skipped``.
        hide_previous: if True, all monthly tabs for ``year - 1`` are
                   hidden (they still exist; formulas still work).
                   The previous-year YTD tab stays visible — it's a
                   useful reference.
    """
    months_created: list[str] = []
    months_skipped: list[str] = []

    for month_idx in range(1, 13):
        sheet_name = sheet_format.monthly_sheet_name(
            month_name=calendar.month_name[month_idx],
            month_short=calendar.month_abbr[month_idx],
            month_num=month_idx,
            year=year,
        )
        try:
            build_month_tab(
                backend,
                sheet_format,
                year=year,
                month=month_idx,
                categories=categories,
                overwrite=overwrite,
            )
            months_created.append(sheet_name)
        except SheetsAlreadyExistsError:
            months_skipped.append(sheet_name)

    ytd_name = sheet_format.ytd_sheet_name(year=year)
    ytd_existed = backend.has_worksheet(ytd_name)
    if ytd_existed and not overwrite:
        # Treat as a "skip" — same semantics as monthly tabs.
        ytd_overwritten = False
    else:
        build_ytd_tab(
            backend,
            sheet_format,
            year=year,
            categories=categories,
            overwrite=overwrite,
        )
        ytd_overwritten = ytd_existed

    previous_year_hidden = (
        hide_previous_year_monthly_tabs(backend, sheet_format, year=year - 1)
        if hide_previous
        else []
    )

    return YearSetupReport(
        year=year,
        months_created=months_created,
        months_skipped=months_skipped,
        ytd_tab=ytd_name,
        ytd_overwritten=ytd_overwritten,
        previous_year_hidden=previous_year_hidden,
    )


def hide_previous_year_monthly_tabs(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    *,
    year: int,
) -> list[str]:
    """Hide all monthly tabs for ``year``. YTD tab stays visible."""
    hidden: list[str] = []
    for month_idx in range(1, 13):
        name = sheet_format.monthly_sheet_name(
            month_name=calendar.month_name[month_idx],
            month_short=calendar.month_abbr[month_idx],
            month_num=month_idx,
            year=year,
        )
        if not backend.has_worksheet(name):
            continue
        ws = backend.get_worksheet(name)
        ws.set_hidden(True)
        hidden.append(name)
    return hidden


def ensure_month_tab(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    *,
    year: int,
    month: int,
    categories: list[str],
) -> WorksheetHandle:
    """Lazy auto-create: build the tab if it's missing, return it.

    This is the safety net wired into the chat-write path (Step 5).
    First transaction of a new month silently provisions the tab,
    so the user never sees a "tab not found" error.
    """
    sheet_name = sheet_format.monthly_sheet_name(
        month_name=calendar.month_name[month],
        month_short=calendar.month_abbr[month],
        month_num=month,
        year=year,
    )
    if backend.has_worksheet(sheet_name):
        return backend.get_worksheet(sheet_name)
    return build_month_tab(
        backend,
        sheet_format,
        year=year,
        month=month,
        categories=categories,
        overwrite=False,
    )


def ensure_ytd_tab(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    *,
    year: int,
    categories: list[str],
) -> WorksheetHandle:
    """Lazy auto-create: build the YTD <year> tab if missing."""
    name = sheet_format.ytd_sheet_name(year=year)
    if backend.has_worksheet(name):
        return backend.get_worksheet(name)
    return build_ytd_tab(
        backend,
        sheet_format,
        year=year,
        categories=categories,
        overwrite=False,
    )


def discover_years_present(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
) -> list[int]:
    """Return the years that already have a YTD tab in this spreadsheet."""
    found: list[int] = []
    pattern = sheet_format.ytd.sheet_name_pattern
    # Pattern always contains ``{year}`` — we anchor on it.
    for ws in backend.list_worksheets():
        for year in range(2020, 2100):
            if pattern.format(year=year) == ws.title:
                found.append(year)
                break
    return sorted(found)


__all__ = [
    "SheetsNotFoundError",  # re-export for callers that catch it
    "YearSetupReport",
    "discover_years_present",
    "ensure_month_tab",
    "ensure_ytd_tab",
    "hide_previous_year_monthly_tabs",
    "setup_year",
]
