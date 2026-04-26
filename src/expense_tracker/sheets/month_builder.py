"""Build (or rebuild) a single monthly tab from the sheet format.

A monthly tab is divided into three regions:

    ┌────────────────────────────────────────────────────────┐
    │  Row 1   Title  ("April 2026 Expenses")                │
    │  Row 3   Summary                                       │
    │  Row 4-7   Total / Transactions / Avg / Largest        │
    │                                                        │
    │  Row 9   "Daily Grid"                                  │
    │  Row 10  Header: Date | Day | <13 categories> | TOTAL  │
    │  Row 11+ Day rows (1..30, 31, etc., per month)         │
    │  Row T   "Total" row — column-wise sums                │
    │                                                        │
    │  Row T+2 "Breakdown by Tag — April 2026"               │
    │  Row T+4 Per-category blocks (one block per category): │
    │           "Groceries"                                  │
    │           QUERY formula spilling into ~11 rows         │
    │           …                                            │
    └────────────────────────────────────────────────────────┘

Every cell in the daily grid + summary + breakdown is a **live formula**
that reads from the Transactions tab. The monthly tab holds *zero
data of its own* — rebuilding it is destructive only of layout, never
of expense history.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from .backend import (
    CellFormat,
    SheetsBackend,
    WorksheetHandle,
    col_index_to_letter,
)
from .exceptions import SheetFormatError
from .format import SheetFormat
from .transactions import (
    TRANSACTIONS_COLUMNS,
)
from .transactions import (
    col_for as txn_col_for,
)

# ─── Layout constants ───────────────────────────────────────────────────

# 1-based row indices. Adjust together if the layout changes.
ROW_TITLE = 1
ROW_SUMMARY_TITLE = 3
ROW_SUMMARY_FIRST = 4   # rows 4..7 are summary metrics
ROW_SUMMARY_LAST = 7
ROW_DAILY_TITLE = 9
ROW_DAILY_HEADER = 10
ROW_DAILY_FIRST = 11    # day-1 row

# Daily grid columns: A=Date, B=Day, then categories, then TOTAL.
COL_DATE_INDEX = 0      # A
COL_DAY_INDEX = 1       # B
CATEGORY_FIRST_INDEX = 2  # C — first category column

# Number of rows we leave between the daily grid and the breakdown
# section, plus between consecutive category blocks within breakdown.
GAP_AFTER_GRID = 2
GAP_BETWEEN_BLOCKS = 2

# Label used for the calendar weekday output (e.g. "Mon"). We use the
# C locale's abbreviation so this stays deterministic across machines.
_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class MonthLayout:
    """Resolved row addresses for a specific (year, month) combination."""

    year: int
    month: int                    # 1..12
    days_in_month: int
    daily_first_row: int          # = ROW_DAILY_FIRST
    daily_last_row: int           # = ROW_DAILY_FIRST + days_in_month - 1
    total_row: int                # = daily_last_row + 1
    breakdown_title_row: int      # = total_row + 1 + GAP_AFTER_GRID  (one blank, then title)
    breakdown_first_block_row: int  # title row of the first category block
    breakdown_last_row: int       # last row used by the last block

    @classmethod
    def for_month(
        cls,
        *,
        year: int,
        month: int,
        n_categories: int,
        breakdown_top_n: int,
    ) -> MonthLayout:
        if not (1 <= month <= 12):
            raise ValueError(f"month must be 1..12, got {month}")
        days_in_month = calendar.monthrange(year, month)[1]
        daily_first = ROW_DAILY_FIRST
        daily_last = daily_first + days_in_month - 1
        total = daily_last + 1
        bt = total + 1 + GAP_AFTER_GRID  # blank row, then title
        # Each block: 1 title row + 1 formula row (spills 1 header + up to N data) + gap
        block_height = 1 + (1 + breakdown_top_n) + GAP_BETWEEN_BLOCKS
        first_block = bt + 2  # blank line after title
        last_row = first_block + n_categories * block_height
        return cls(
            year=year,
            month=month,
            days_in_month=days_in_month,
            daily_first_row=daily_first,
            daily_last_row=daily_last,
            total_row=total,
            breakdown_title_row=bt,
            breakdown_first_block_row=first_block,
            breakdown_last_row=last_row,
        )


# ─── Formula builders ───────────────────────────────────────────────────

def _txn_range(letter_key: str) -> str:
    """e.g. 'date' -> 'Transactions!B:B'."""
    return f"Transactions!{txn_col_for(letter_key)}:{txn_col_for(letter_key)}"


def daily_cell_formula(*, category: str, date_cell: str) -> str:
    """SUMIFS formula for one (day, category) cell.

    ``date_cell`` is the A1 ref of the Date column on the same row,
    e.g. ``"$A11"`` for the first data row. Using a relative row + a
    locked column ($A) means we can write one formula and copy it
    down — Sheets adjusts the row, keeps the column.
    """
    cat = _sheets_string_literal(category)
    return (
        f"=IFERROR(SUMIFS("
        f"{_txn_range('amount_usd')},"
        f"{_txn_range('category')},{cat},"
        f"{_txn_range('date')},{date_cell}"
        f"),0)"
    )


def daily_total_cell_formula(
    *, first_cat_col: str, last_cat_col: str, row: int
) -> str:
    """SUM across category columns for one day's TOTAL cell."""
    return f"=SUM({first_cat_col}{row}:{last_cat_col}{row})"


def column_total_formula(
    *, col_letter: str, first_data_row: int, last_data_row: int
) -> str:
    return f"=SUM({col_letter}{first_data_row}:{col_letter}{last_data_row})"


def summary_total_formula(*, total_row_grand_total_cell: str) -> str:
    """Top-line "Total Spent" — read from the daily-grid grand total."""
    return f"={total_row_grand_total_cell}"


def summary_transactions_formula(*, year: int, month: int) -> str:
    """Count rows in Transactions whose Date falls inside this month."""
    return (
        f"=COUNTIFS("
        f"{_txn_range('date')},\">=\"&DATE({year},{month},1),"
        f"{_txn_range('date')},\"<=\"&EOMONTH(DATE({year},{month},1),0)"
        f")"
    )


def summary_avg_per_day_formula(
    *, total_cell: str, year: int, month: int
) -> str:
    """Average per day, accounting for in-progress vs finished months.

    Divisor = number of days that have actually elapsed within this
    month (clamped to ≥1 to avoid /0 for future months / empty data).
    """
    return (
        f"=IFERROR({total_cell}/MAX(1,"
        f"MIN(TODAY(),EOMONTH(DATE({year},{month},1),0))"
        f"-DATE({year},{month},1)+1"
        f"),0)"
    )


def summary_largest_single_formula(*, year: int, month: int) -> str:
    return (
        f"=IFERROR(MAXIFS("
        f"{_txn_range('amount_usd')},"
        f"{_txn_range('date')},\">=\"&DATE({year},{month},1),"
        f"{_txn_range('date')},\"<=\"&EOMONTH(DATE({year},{month},1),0)"
        f"),0)"
    )


def breakdown_query_formula(
    *,
    category: str,
    year: int,
    month: int,
    days_in_month: int,
    limit: int,
) -> str:
    """QUERY formula that lists the top *limit* notes for this category.

    The QUERY filters Transactions to this month + this category, groups
    by the Note column, and orders by sum(amount_usd) desc. ``IFERROR``
    wraps the whole thing so empty months show a single dash.
    """
    note_col = txn_col_for("note")
    cat_col = txn_col_for("category")
    date_col = txn_col_for("date")
    usd_col = txn_col_for("amount_usd")
    last_col = col_index_to_letter(len(TRANSACTIONS_COLUMNS) - 1)

    # QUERY language uses single quotes for string literals. Categories
    # like "India Expense" embed a space; double-quote-escape would be
    # invalid here, hence we sanitise to single-quote-only literals.
    cat_lit = _query_string_literal(category)
    start_iso = date(year, month, 1).isoformat()
    end_iso = date(year, month, days_in_month).isoformat()

    return (
        f"=IFERROR(QUERY(Transactions!A:{last_col},"
        f"\"select {note_col}, sum({usd_col}) "
        f"where {cat_col}={cat_lit} "
        f"and {date_col} >= date '{start_iso}' "
        f"and {date_col} <= date '{end_iso}' "
        f"and {note_col} is not null "
        f"and {note_col} != '' "
        f"group by {note_col} "
        f"order by sum({usd_col}) desc "
        f"limit {limit} "
        f"label sum({usd_col}) '{category} Total (USD)'\","
        f"1),\"—\")"
    )


def _sheets_string_literal(s: str) -> str:
    """Escape a string for use inside a SUMIFS/COUNTIFS criterion."""
    escaped = s.replace('"', '""')
    return f'"{escaped}"'


def _query_string_literal(s: str) -> str:
    """Escape a string for use inside a QUERY clause's where condition.

    Sheets QUERY language uses single quotes. Doubling them escapes a
    literal apostrophe; e.g. ``Trader Joe's`` -> ``'Trader Joe''s'``.
    """
    return "'" + s.replace("'", "''") + "'"


# ─── The build entrypoint ───────────────────────────────────────────────

def build_month_tab(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    *,
    year: int,
    month: int,
    categories: list[str],
    overwrite: bool = False,
) -> WorksheetHandle:
    """Create or rebuild the monthly tab for ``(year, month)``.

    Args:
        backend:       open spreadsheet handle.
        sheet_format:  parsed format file.
        year, month:   the month to build (1..12).
        categories:    canonical names in column order, left-to-right.
        overwrite:     if True and the tab exists, delete + recreate;
                       if False and the tab exists, raise.

    Returns the worksheet handle pointing at the newly built tab.
    """
    if not categories:
        raise SheetFormatError("cannot build a monthly tab with zero categories")

    month_name = calendar.month_name[month]
    month_short = calendar.month_abbr[month]
    sheet_name = sheet_format.monthly_sheet_name(
        month_name=month_name,
        month_short=month_short,
        month_num=month,
        year=year,
    )

    layout = MonthLayout.for_month(
        year=year,
        month=month,
        n_categories=len(categories),
        breakdown_top_n=sheet_format.monthly.breakdown_top_n_per_category,
    )

    # Total columns: 2 fixed + categories + 1 TOTAL.
    total_cols = 2 + len(categories) + 1
    # Rows: enough headroom for breakdown blocks + a 5-row buffer.
    total_rows = layout.breakdown_last_row + 5

    if backend.has_worksheet(sheet_name):
        if not overwrite:
            from .exceptions import SheetsAlreadyExistsError

            raise SheetsAlreadyExistsError(
                f"worksheet {sheet_name!r} already exists. "
                f"Pass overwrite=True (or use --rebuild-month) to replace it."
            )
        backend.delete_worksheet(sheet_name)

    ws = backend.create_worksheet(sheet_name, rows=total_rows, cols=total_cols)
    _populate_month_tab(ws, sheet_format, layout, categories)
    _format_month_tab(ws, sheet_format, layout, categories)
    return ws


def _populate_month_tab(
    ws: WorksheetHandle,
    sheet_format: SheetFormat,
    layout: MonthLayout,
    categories: list[str],
) -> None:
    """Write all values + formulas. Pure data; styling comes after."""
    n_cats = len(categories)
    first_cat_col_letter = col_index_to_letter(CATEGORY_FIRST_INDEX)
    last_cat_col_letter = col_index_to_letter(CATEGORY_FIRST_INDEX + n_cats - 1)
    total_col_index = CATEGORY_FIRST_INDEX + n_cats
    total_col_letter = col_index_to_letter(total_col_index)
    last_col_letter = total_col_letter
    grand_total_cell = f"{total_col_letter}{layout.total_row}"
    month_name = calendar.month_name[layout.month]

    # Title (row 1).
    ws.update_values(
        f"A{ROW_TITLE}",
        [[sheet_format.monthly_title(
            month_name=month_name,
            month_short=calendar.month_abbr[layout.month],
            month_num=layout.month,
            year=layout.year,
        )]],
    )

    # Summary block (rows 3..7).
    ws.update_values(
        f"A{ROW_SUMMARY_TITLE}",
        [["Summary"]],
    )
    ws.update_values(
        f"A{ROW_SUMMARY_FIRST}:B{ROW_SUMMARY_LAST}",
        [
            ["Total Spent", summary_total_formula(
                total_row_grand_total_cell=grand_total_cell
            )],
            ["Transactions", summary_transactions_formula(
                year=layout.year, month=layout.month
            )],
            ["Avg / day (so far)", summary_avg_per_day_formula(
                total_cell=f"B{ROW_SUMMARY_FIRST}",
                year=layout.year,
                month=layout.month,
            )],
            ["Largest single", summary_largest_single_formula(
                year=layout.year, month=layout.month
            )],
        ],
    )

    # Daily grid header (row 10).
    header_row_values = ["Date", "Day", *categories, "TOTAL"]
    ws.update_values(
        f"A{ROW_DAILY_HEADER}:{last_col_letter}{ROW_DAILY_HEADER}",
        [header_row_values],
    )

    # "Daily Grid" subtitle one row above the header.
    ws.update_values(f"A{ROW_DAILY_TITLE}", [["Daily Grid"]])

    # Day rows (rows 11..N).
    day_rows: list[list[object]] = []
    for day_idx in range(1, layout.days_in_month + 1):
        row_num = layout.daily_first_row + day_idx - 1
        d = date(layout.year, layout.month, day_idx)
        weekday = _WEEKDAY_NAMES[d.weekday()]
        date_cell = f"$A{row_num}"
        cells: list[object] = [d.isoformat(), weekday]
        for cat in categories:
            cells.append(daily_cell_formula(category=cat, date_cell=date_cell))
        cells.append(
            daily_total_cell_formula(
                first_cat_col=first_cat_col_letter,
                last_cat_col=last_cat_col_letter,
                row=row_num,
            )
        )
        day_rows.append(cells)
    ws.update_values(
        f"A{layout.daily_first_row}:{last_col_letter}{layout.daily_last_row}",
        day_rows,
    )

    # Total row.
    total_cells: list[object] = ["Total", ""]
    for col_offset in range(n_cats):
        col_letter = col_index_to_letter(CATEGORY_FIRST_INDEX + col_offset)
        total_cells.append(
            column_total_formula(
                col_letter=col_letter,
                first_data_row=layout.daily_first_row,
                last_data_row=layout.daily_last_row,
            )
        )
    total_cells.append(
        column_total_formula(
            col_letter=total_col_letter,
            first_data_row=layout.daily_first_row,
            last_data_row=layout.daily_last_row,
        )
    )
    ws.update_values(
        f"A{layout.total_row}:{last_col_letter}{layout.total_row}",
        [total_cells],
    )

    # Breakdown title.
    ws.update_values(
        f"A{layout.breakdown_title_row}",
        [[f"Breakdown by Tag — {month_name} {layout.year}"]],
    )

    # One block per category.
    block_height = 1 + (1 + sheet_format.monthly.breakdown_top_n_per_category) + GAP_BETWEEN_BLOCKS
    for i, cat in enumerate(categories):
        block_top = layout.breakdown_first_block_row + i * block_height
        ws.update_values(f"A{block_top}", [[cat]])
        formula = breakdown_query_formula(
            category=cat,
            year=layout.year,
            month=layout.month,
            days_in_month=layout.days_in_month,
            limit=sheet_format.monthly.breakdown_top_n_per_category,
        )
        ws.update_values(f"A{block_top + 1}", [[formula]])


def _format_month_tab(
    ws: WorksheetHandle,
    sheet_format: SheetFormat,
    layout: MonthLayout,
    categories: list[str],
) -> None:
    """Apply colors, bold, freeze, widths."""
    fmt = sheet_format.monthly.formatting
    n_cats = len(categories)
    first_cat_col = col_index_to_letter(CATEGORY_FIRST_INDEX)
    total_col = col_index_to_letter(CATEGORY_FIRST_INDEX + n_cats)
    last_col = total_col

    # Title row.
    ws.format_range(
        f"A{ROW_TITLE}:{last_col}{ROW_TITLE}",
        CellFormat(
            font_size=fmt.title_font_size,
            bold=fmt.title_bold,
        ),
    )

    # Summary section.
    ws.format_range(
        f"A{ROW_SUMMARY_TITLE}",
        CellFormat(bold=True, font_size=12),
    )
    ws.format_range(
        f"A{ROW_SUMMARY_FIRST}:A{ROW_SUMMARY_LAST}",
        CellFormat(bold=fmt.summary_label_bold),
    )
    ws.format_range(
        f"B{ROW_SUMMARY_FIRST}:B{ROW_SUMMARY_LAST}",
        CellFormat(
            bold=fmt.summary_value_bold,
            number_format=fmt.number_format,
        ),
    )

    # Daily Grid section title.
    ws.format_range(
        f"A{ROW_DAILY_TITLE}",
        CellFormat(bold=True, font_size=12),
    )
    # Header row.
    ws.format_range(
        f"A{ROW_DAILY_HEADER}:{last_col}{ROW_DAILY_HEADER}",
        CellFormat(
            background_color=fmt.grid_header_background,
            bold=True,
            horizontal_alignment="CENTER",
        ),
    )

    # Numeric region (categories + total) for the daily grid.
    ws.format_range(
        f"{first_cat_col}{layout.daily_first_row}:{last_col}{layout.daily_last_row}",
        CellFormat(number_format=fmt.number_format),
    )

    # Total row.
    ws.format_range(
        f"A{layout.total_row}:{last_col}{layout.total_row}",
        CellFormat(
            background_color=fmt.grid_total_row_background,
            bold=True,
            number_format=fmt.number_format,
        ),
    )

    # TOTAL column band (highlight the right-most column).
    ws.format_range(
        f"{total_col}{layout.daily_first_row}:{total_col}{layout.daily_last_row}",
        CellFormat(
            background_color=fmt.grid_total_column_background,
            bold=True,
            number_format=fmt.number_format,
        ),
    )

    # Breakdown title.
    ws.format_range(
        f"A{layout.breakdown_title_row}",
        CellFormat(bold=fmt.breakdown_title_bold, font_size=12),
    )

    # Each category block's title row gets bolded.
    block_height = 1 + (1 + sheet_format.monthly.breakdown_top_n_per_category) + GAP_BETWEEN_BLOCKS
    for i in range(n_cats):
        block_top = layout.breakdown_first_block_row + i * block_height
        ws.format_range(
            f"A{block_top}",
            CellFormat(bold=True),
        )

    # Freeze panes.
    if fmt.freeze_rows or fmt.freeze_cols:
        ws.freeze(rows=fmt.freeze_rows, cols=fmt.freeze_cols)

    # Column widths.
    widths = [
        fmt.date_column_width,
        fmt.day_column_width,
        *([fmt.category_column_width] * n_cats),
        fmt.total_column_width,
    ]
    ws.set_column_widths_px(start_col="A", widths=widths)
