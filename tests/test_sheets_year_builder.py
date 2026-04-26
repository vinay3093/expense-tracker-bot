"""Unit tests for bulk year setup (12 monthly tabs + YTD)."""

from __future__ import annotations

from expense_tracker.sheets import (
    FakeSheetsBackend,
    SheetFormat,
    discover_years_present,
    ensure_month_tab,
    ensure_ytd_tab,
    hide_previous_year_monthly_tabs,
    setup_year,
)
from expense_tracker.sheets.backend import _FakeWorksheet

SIMPLE_CATEGORIES = ["Food", "Groceries", "House"]


# ─── setup_year ────────────────────────────────────────────────────────

def test_setup_year_creates_12_months_plus_ytd():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    report = setup_year(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    assert len(report.months_created) == 12
    assert report.months_skipped == []
    assert report.ytd_tab == "YTD 2026"
    assert report.previous_year_hidden == []

    titles = [w.title for w in b.list_worksheets()]
    assert "January 2026" in titles
    assert "December 2026" in titles
    assert "YTD 2026" in titles


def test_setup_year_idempotent_without_overwrite():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    setup_year(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    # Run again — everything should be skipped.
    report2 = setup_year(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    assert report2.months_created == []
    assert len(report2.months_skipped) == 12
    assert report2.ytd_overwritten is False


def test_setup_year_overwrite_rebuilds_all():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    setup_year(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    # Mutate one tab to detect rebuild.
    apr = b.get_worksheet("April 2026")
    assert isinstance(apr, _FakeWorksheet)
    apr.update_values("A1", [["WIPE"]])
    report = setup_year(
        b, fmt, year=2026, categories=SIMPLE_CATEGORIES, overwrite=True,
    )
    assert len(report.months_created) == 12
    assert report.ytd_overwritten is True
    apr_after = b.get_worksheet("April 2026")
    assert isinstance(apr_after, _FakeWorksheet)
    assert apr_after.cell("A1") == "April 2026 Expenses"


def test_setup_year_partial_existing_creates_missing():
    """Some months exist already; setup should fill in the rest."""
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    # Pre-create a couple of months manually.
    b.create_worksheet("January 2026")
    b.create_worksheet("July 2026")
    report = setup_year(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    assert len(report.months_created) == 10
    assert sorted(report.months_skipped) == ["January 2026", "July 2026"]


def test_short_summary_string():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    report = setup_year(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    s = report.short_summary()
    assert "12 monthly tabs created" in s
    assert "YTD" in s


# ─── hide_previous_year_monthly_tabs ───────────────────────────────────

def test_hide_previous_year_only_hides_monthly_tabs():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    setup_year(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    # Now bootstrap 2027 with hide_previous=True.
    setup_year(
        b, fmt, year=2027, categories=SIMPLE_CATEGORIES, hide_previous=True,
    )
    # All 12 monthly 2026 tabs should be hidden.
    ytd_2026 = b.get_worksheet("YTD 2026")
    apr_2026 = b.get_worksheet("April 2026")
    apr_2027 = b.get_worksheet("April 2027")
    assert apr_2026.hidden is True
    # YTD 2026 stays visible — handy reference.
    assert ytd_2026.hidden is False
    # 2027 tabs are visible.
    assert apr_2027.hidden is False


def test_hide_previous_year_idempotent_when_no_2025_tabs():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    hidden = hide_previous_year_monthly_tabs(b, fmt, year=2025)
    assert hidden == []


# ─── ensure_month_tab / ensure_ytd_tab ─────────────────────────────────

def test_ensure_month_tab_creates_when_missing():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    ws = ensure_month_tab(
        b, fmt, year=2026, month=4, categories=SIMPLE_CATEGORIES,
    )
    assert ws.title == "April 2026"
    # Calling again returns the same tab without raising.
    ws2 = ensure_month_tab(
        b, fmt, year=2026, month=4, categories=SIMPLE_CATEGORIES,
    )
    assert ws2.title == "April 2026"
    # Only one April 2026 in the worksheet list.
    assert sum(1 for w in b.list_worksheets() if w.title == "April 2026") == 1


def test_ensure_ytd_tab_creates_when_missing():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    ws = ensure_ytd_tab(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    assert ws.title == "YTD 2026"
    ws2 = ensure_ytd_tab(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    assert ws2.title == "YTD 2026"


# ─── discover_years_present ────────────────────────────────────────────

def test_discover_years_finds_only_ytd_tabs():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    setup_year(b, fmt, year=2025, categories=SIMPLE_CATEGORIES)
    setup_year(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    # Add a noise tab that mimics monthly format — should NOT be detected.
    b.create_worksheet("April 2024")
    years = discover_years_present(b, fmt)
    assert years == [2025, 2026]


def test_discover_years_empty_spreadsheet():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    assert discover_years_present(b, fmt) == []
