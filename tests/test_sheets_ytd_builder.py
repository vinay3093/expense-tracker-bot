"""Unit tests for YTD tab construction + formulas."""

from __future__ import annotations

import pytest

from expense_tracker.sheets import (
    FakeSheetsBackend,
    SheetFormat,
    SheetsAlreadyExistsError,
    YTDLayout,
    build_ytd_tab,
)
from expense_tracker.sheets.backend import _FakeWorksheet
from expense_tracker.sheets.exceptions import SheetFormatError
from expense_tracker.sheets.ytd_builder import (
    monthly_category_cell_formula,
    top_vendors_query_formula,
    year_avg_per_day_formula,
    year_avg_per_month_formula,
    year_largest_single_formula,
    year_total_formula,
    year_transactions_formula,
)

SIMPLE_CATEGORIES = ["Food", "Groceries", "House"]


# ─── YTDLayout ─────────────────────────────────────────────────────────

def test_ytd_layout_columns_for_3_categories():
    layout = YTDLayout(year=2026, n_categories=3)
    # A=Month, B/C/D = categories, E = TOTAL.
    assert layout.first_cat_col_letter == "B"
    assert layout.last_cat_col_letter == "D"
    assert layout.total_col_letter == "E"
    assert layout.total_cells_count == 1 + 3 + 1


def test_ytd_layout_columns_for_13_categories():
    layout = YTDLayout(year=2026, n_categories=13)
    # Categories occupy columns B..N (13 letters); TOTAL = O.
    assert layout.first_cat_col_letter == "B"
    assert layout.last_cat_col_letter == "N"
    assert layout.total_col_letter == "O"


# ─── Formula correctness ───────────────────────────────────────────────

def test_monthly_category_cell_formula_uses_eomonth():
    formula = monthly_category_cell_formula(year=2026, month=4, category="Food")
    assert "SUMIFS(" in formula
    assert "DATE(2026,4,1)" in formula
    assert "EOMONTH(DATE(2026,4,1),0)" in formula
    assert '"Food"' in formula


def test_year_total_formula_points_at_grid_total_row():
    formula = year_total_formula(total_col_letter="O")
    assert formula == "=O24"  # ROW_GRID_TOTAL is 24


def test_year_transactions_formula_covers_full_year():
    formula = year_transactions_formula(year=2026)
    assert "DATE(2026,1,1)" in formula
    assert "DATE(2026,12,31)" in formula


def test_year_avg_per_day_formula_clamps_divisor():
    formula = year_avg_per_day_formula(total_cell="B4", year=2026)
    assert "MAX(1," in formula
    assert "TODAY()" in formula
    assert "DATE(2026,12,31)" in formula


def test_year_avg_per_month_formula_uses_datedif():
    formula = year_avg_per_month_formula(total_cell="B4", year=2026)
    assert "DATEDIF" in formula
    assert "MAX(1," in formula


def test_year_largest_single_formula():
    formula = year_largest_single_formula(year=2026)
    assert "MAXIFS" in formula
    assert "DATE(2026,1,1)" in formula


def test_top_vendors_query_formula_format():
    formula = top_vendors_query_formula(year=2026, top_n=5)
    assert formula.startswith("=IFERROR(QUERY(Transactions!")
    assert "2026-01-01" in formula
    assert "2026-12-31" in formula
    assert "limit 5" in formula
    # Filter excludes empty vendors (otherwise top vendor = "").
    assert "is not null" in formula
    assert "!= ''" in formula


# ─── build_ytd_tab end-to-end ──────────────────────────────────────────

def test_build_ytd_creates_tab():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    ws = build_ytd_tab(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    assert ws.title == "YTD 2026"
    assert b.has_worksheet("YTD 2026")
    assert isinstance(ws, _FakeWorksheet)

    # Title.
    assert ws.cell("A1") == "Year to Date — 2026"
    # Year summary header.
    assert ws.cell("A3") == "Year Summary"
    # Grid header at row 11.
    layout = YTDLayout(year=2026, n_categories=len(SIMPLE_CATEGORIES))
    last_col = layout.last_col_letter
    header = ws.get_values(f"A11:{last_col}11")[0]
    assert header[0] == "Month"
    assert header[1:1 + len(SIMPLE_CATEGORIES)] == SIMPLE_CATEGORIES
    assert header[-1] == "TOTAL"

    # Jan row.
    assert ws.cell("A12") == "January"
    # Dec row.
    assert ws.cell("A23") == "December"
    # Total row.
    assert ws.cell("A24") == "Total"

    # Vendor section.
    assert ws.cell("A26") == "Top Vendors — 2026"
    vendor_query = ws.cell("A27")
    assert isinstance(vendor_query, str)
    assert vendor_query.startswith("=IFERROR(QUERY(")


def test_build_ytd_refuses_existing_without_overwrite():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    build_ytd_tab(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    with pytest.raises(SheetsAlreadyExistsError):
        build_ytd_tab(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)


def test_build_ytd_overwrite_replaces_tab():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    build_ytd_tab(b, fmt, year=2026, categories=SIMPLE_CATEGORIES)
    old = b.get_worksheet("YTD 2026")
    assert isinstance(old, _FakeWorksheet)
    old.update_values("A1", [["WIPE"]])
    build_ytd_tab(
        b, fmt, year=2026, categories=SIMPLE_CATEGORIES, overwrite=True,
    )
    new = b.get_worksheet("YTD 2026")
    assert isinstance(new, _FakeWorksheet)
    assert new.cell("A1") == "Year to Date — 2026"


def test_build_ytd_zero_categories_raises():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    with pytest.raises(SheetFormatError):
        build_ytd_tab(b, fmt, year=2026, categories=[])
