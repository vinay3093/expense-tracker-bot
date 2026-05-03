"""Unit tests for monthly tab construction + formulas."""

from __future__ import annotations

import calendar

import pytest

from expense_tracker.ledger.sheets import (
    FakeSheetsBackend,
    MonthLayout,
    SheetFormat,
    SheetsAlreadyExistsError,
    build_month_tab,
    daily_cell_formula,
    daily_total_cell_formula,
)
from expense_tracker.ledger.sheets.backend import _FakeWorksheet
from expense_tracker.ledger.sheets.exceptions import SheetFormatError
from expense_tracker.ledger.sheets.month_builder import (
    breakdown_query_formula,
    summary_avg_per_day_formula,
    summary_largest_single_formula,
    summary_total_formula,
    summary_transactions_formula,
)

SIMPLE_CATEGORIES = ["Food", "Groceries", "House"]


# ─── MonthLayout ───────────────────────────────────────────────────────

def test_month_layout_april_2026():
    layout = MonthLayout.for_month(
        year=2026, month=4, n_categories=13, breakdown_top_n=10,
    )
    assert layout.days_in_month == 30
    assert layout.daily_first_row == 11
    assert layout.daily_last_row == 40  # 11 + 30 - 1
    assert layout.total_row == 41
    assert layout.breakdown_title_row > layout.total_row


def test_month_layout_february_leap():
    layout = MonthLayout.for_month(
        year=2024, month=2, n_categories=5, breakdown_top_n=5,
    )
    assert layout.days_in_month == 29


def test_month_layout_february_non_leap():
    layout = MonthLayout.for_month(
        year=2026, month=2, n_categories=5, breakdown_top_n=5,
    )
    assert layout.days_in_month == 28


def test_month_layout_invalid_month():
    with pytest.raises(ValueError):
        MonthLayout.for_month(year=2026, month=13, n_categories=1, breakdown_top_n=1)


# ─── Formula correctness ───────────────────────────────────────────────

def test_daily_cell_formula_references_transactions():
    formula = daily_cell_formula(category="Food", date_cell="$A11")
    assert formula.startswith("=IFERROR(SUMIFS(")
    assert "Transactions!" in formula
    # Quoted category criterion.
    assert '"Food"' in formula
    # Date criterion: uses the cell ref verbatim.
    assert "$A11" in formula


def test_daily_cell_formula_escapes_double_quote_in_category():
    formula = daily_cell_formula(category='Foo "bar"', date_cell="$A11")
    assert '"Foo ""bar"""' in formula


def test_daily_total_formula_sums_category_range():
    formula = daily_total_cell_formula(
        first_cat_col="C", last_cat_col="O", row=11,
    )
    assert formula == "=SUM(C11:O11)"


def test_summary_total_formula_points_at_grand_total():
    assert summary_total_formula(total_row_grand_total_cell="P41") == "=P41"


def test_summary_transactions_formula_uses_eomonth():
    formula = summary_transactions_formula(year=2026, month=4)
    assert "COUNTIFS(" in formula
    assert "DATE(2026,4,1)" in formula
    assert "EOMONTH(DATE(2026,4,1),0)" in formula


def test_summary_avg_per_day_formula_clamps_divisor():
    formula = summary_avg_per_day_formula(
        total_cell="B4", year=2026, month=4,
    )
    # Must guard against /0 with MAX(1, ...).
    assert "MAX(1," in formula
    assert "TODAY()" in formula
    assert "EOMONTH(DATE(2026,4,1),0)" in formula


def test_summary_largest_single_formula_uses_maxifs():
    formula = summary_largest_single_formula(year=2026, month=4)
    assert "MAXIFS(" in formula
    assert "DATE(2026,4,1)" in formula


def test_breakdown_query_formula_format():
    formula = breakdown_query_formula(
        category="Food",
        year=2026,
        month=4,
        days_in_month=30,
        limit=10,
    )
    assert formula.startswith("=IFERROR(QUERY(Transactions!")
    assert "where " in formula
    # QUERY uses single-quoted literals.
    assert "'Food'" in formula
    assert "limit 10" in formula
    assert "2026-04-01" in formula
    assert "2026-04-30" in formula


def test_breakdown_query_formula_escapes_apostrophe():
    formula = breakdown_query_formula(
        category="Trader Joe's",
        year=2026,
        month=4,
        days_in_month=30,
        limit=10,
    )
    # Escaped to doubled single quote inside QUERY language.
    assert "'Trader Joe''s'" in formula


# ─── build_month_tab end-to-end (against fake backend) ─────────────────

def test_build_month_creates_tab_with_correct_layout():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    ws = build_month_tab(
        b, fmt, year=2026, month=4, categories=SIMPLE_CATEGORIES,
    )
    assert ws.title == "April 2026"
    assert b.has_worksheet("April 2026")
    assert isinstance(ws, _FakeWorksheet)

    # Title in A1.
    assert ws.cell("A1") == "April 2026 Expenses"
    # Summary block label/formula in row 4.
    assert ws.cell("A4") == "Total Spent"
    total_formula = ws.cell("B4")
    assert isinstance(total_formula, str) and total_formula.startswith("=")

    # Header row at row 10 — Date, Day, then 3 categories, then TOTAL.
    n_cats = len(SIMPLE_CATEGORIES)
    expected_cols = 2 + n_cats + 1
    # Read the header.
    last_letter = chr(ord("A") + expected_cols - 1)
    header = ws.get_values(f"A10:{last_letter}10")[0]
    assert header[0] == "Date"
    assert header[1] == "Day"
    assert header[2:5] == SIMPLE_CATEGORIES
    assert header[-1] == "TOTAL"

    # Day-1 row should have an ISO date string in column A.
    assert ws.cell("A11") == "2026-04-01"
    # Day-30 row.
    assert ws.cell("A40") == "2026-04-30"

    # Daily cells should be SUMIFS formulas referring to Transactions.
    food_cell_day1 = ws.cell("C11")
    assert isinstance(food_cell_day1, str)
    assert food_cell_day1.startswith("=IFERROR(SUMIFS(")
    assert "Transactions!" in food_cell_day1
    assert '"Food"' in food_cell_day1

    # Total row at row 41 begins with "Total".
    assert ws.cell("A41") == "Total"


def test_build_month_refuses_existing_tab_without_overwrite():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    build_month_tab(b, fmt, year=2026, month=4, categories=SIMPLE_CATEGORIES)
    with pytest.raises(SheetsAlreadyExistsError):
        build_month_tab(b, fmt, year=2026, month=4, categories=SIMPLE_CATEGORIES)


def test_build_month_overwrite_replaces_tab():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    build_month_tab(b, fmt, year=2026, month=4, categories=SIMPLE_CATEGORIES)
    # Mutate the existing tab so we can detect replacement.
    old_ws = b.get_worksheet("April 2026")
    assert isinstance(old_ws, _FakeWorksheet)
    old_ws.update_values("A1", [["DELETE ME"]])
    # Rebuild.
    build_month_tab(
        b, fmt, year=2026, month=4, categories=SIMPLE_CATEGORIES, overwrite=True,
    )
    new_ws = b.get_worksheet("April 2026")
    assert isinstance(new_ws, _FakeWorksheet)
    assert new_ws.cell("A1") == "April 2026 Expenses"


def test_build_month_zero_categories_raises():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    with pytest.raises(SheetFormatError):
        build_month_tab(b, fmt, year=2026, month=4, categories=[])


def test_build_month_works_for_every_month_of_2026():
    """Smoke: every month builds without errors and uses correct day count."""
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    for m in range(1, 13):
        ws = build_month_tab(b, fmt, year=2026, month=m, categories=SIMPLE_CATEGORIES)
        days = calendar.monthrange(2026, m)[1]
        # Last day's date is in column A at row (10 + days).
        assert isinstance(ws, _FakeWorksheet)
        last_day_iso = f"2026-{m:02d}-{days:02d}"
        assert ws.cell(f"A{10 + days}") == last_day_iso


def test_build_month_installs_emphasis_conditional_bands():
    """Daily grid should get two ``>0``-predicated conditional bands:
    one for the category region, one for the TOTAL column.

    Note: Google Sheets' conditional-format API doesn't support font
    size or number format, so emphasis is achieved via bold + dark
    foreground only. Size is uniform across the grid (set by the base
    style at the cell level, not the conditional rule).
    """
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    build_month_tab(b, fmt, year=2026, month=4, categories=SIMPLE_CATEGORIES)
    ws = b.get_worksheet("April 2026")
    assert isinstance(ws, _FakeWorksheet)

    bands = ws.conditional_bands
    # Predicates of the form "=C11>0", "=F11>0" etc.
    gt_predicates = [b for b in bands if b.predicate_formula.endswith(">0")]
    assert len(gt_predicates) == 2, (
        f"expected 2 emphasis bands, got {len(gt_predicates)}: "
        f"{[b.predicate_formula for b in gt_predicates]}"
    )

    # Category-cell emphasis band: bold + a near-black foreground.
    cat_band = next(b for b in gt_predicates if "C11>0" in b.predicate_formula)
    assert cat_band.resolved_format.bold is True
    assert cat_band.resolved_format.foreground_color is not None
    # Conditional rules can't carry font_size — must be stripped.
    assert cat_band.resolved_format.font_size is None

    # TOTAL-column emphasis band: bold + a deep-blue foreground (the
    # loudest body-text rule).
    total_letter = chr(ord("A") + 2 + len(SIMPLE_CATEGORIES))
    total_band = next(
        b for b in gt_predicates if f"{total_letter}11>0" in b.predicate_formula
    )
    assert total_band.resolved_format.bold is True
    assert total_band.resolved_format.foreground_color is not None
    assert total_band.resolved_format.font_size is None
    # Different (typically bluer) color than the category emphasis.
    assert (
        total_band.resolved_format.foreground_color
        != cat_band.resolved_format.foreground_color
    )


def test_build_month_grand_total_cell_has_loudest_style():
    """Grand-total corner cell should always carry the loudest style —
    largest font of the tab and a tinted background."""
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    build_month_tab(b, fmt, year=2026, month=4, categories=SIMPLE_CATEGORIES)
    ws = b.get_worksheet("April 2026")
    assert isinstance(ws, _FakeWorksheet)

    # Grand-total corner: TOTAL column @ row layout.total_row (= 41 for April).
    grand_total_letter = chr(ord("A") + 2 + len(SIMPLE_CATEGORIES))
    target = f"{grand_total_letter}41"
    matching = [
        f for (rng, f) in ws.format_calls() if rng == target
    ]
    assert matching, f"no format calls targeted grand-total cell {target}"
    last_fmt = matching[-1]
    assert last_fmt.bold is True
    assert (last_fmt.font_size or 0) >= 13
    assert last_fmt.background_color is not None
