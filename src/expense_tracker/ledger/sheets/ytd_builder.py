"""Build (or rebuild) a Year-To-Date tab.

The YTD tab is a calendar-year rollup with three regions:

    ┌────────────────────────────────────────────────────────┐
    │  Row 1   Title  ("Year to Date — 2026")                │
    │  Row 3   "Year Summary"                                │
    │  Row 4-8   Total / Transactions / Avg-day / Avg-month  │
    │            / Largest                                   │
    │                                                        │
    │  Row 10  "Monthly by Category"                         │
    │  Row 11  Header: Month | <13 categories> | TOTAL       │
    │  Row 12-23  One row per month (Jan..Dec)               │
    │  Row 24  "Total" row — column sums                     │
    │                                                        │
    │  Row 26  "Top Vendors — 2026"                          │
    │  Row 27  QUERY spilling top-N vendor breakdown         │
    └────────────────────────────────────────────────────────┘

Like the monthly tabs, every cell is a live formula. The Transactions
tab is the only place expense data lives; this tab just visualises it.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass

from .backend import (
    CellFormat,
    ConditionalBand,
    SheetsBackend,
    WorksheetHandle,
    col_index_to_letter,
)
from .exceptions import SheetFormatError, SheetsAlreadyExistsError
from .format import CellStyle, SheetFormat
from .transactions import (
    TRANSACTIONS_COLUMNS,
)
from .transactions import (
    col_for as txn_col_for,
)

# ─── Layout ─────────────────────────────────────────────────────────────

ROW_TITLE = 1
ROW_SUMMARY_TITLE = 3
ROW_SUMMARY_FIRST = 4
ROW_SUMMARY_LAST = 8
ROW_GRID_TITLE = 10
ROW_GRID_HEADER = 11
ROW_GRID_FIRST = 12     # January row
ROW_GRID_LAST = 23      # December row
ROW_GRID_TOTAL = 24
ROW_VENDOR_TITLE = 26
ROW_VENDOR_QUERY = 27


@dataclass(frozen=True)
class YTDLayout:
    year: int
    n_categories: int

    @property
    def first_cat_col_letter(self) -> str:
        return col_index_to_letter(1)  # B = first category

    @property
    def last_cat_col_letter(self) -> str:
        return col_index_to_letter(1 + self.n_categories - 1)

    @property
    def total_col_letter(self) -> str:
        return col_index_to_letter(1 + self.n_categories)

    @property
    def last_col_letter(self) -> str:
        return self.total_col_letter

    @property
    def total_cells_count(self) -> int:
        return 1 + self.n_categories + 1  # Month + cats + TOTAL


# ─── Formula helpers ────────────────────────────────────────────────────

def _txn_range(key: str) -> str:
    return f"Transactions!{txn_col_for(key)}:{txn_col_for(key)}"


def monthly_category_cell_formula(*, year: int, month: int, category: str) -> str:
    """Sum Transactions for one (month, category) cell of the grid."""
    return (
        f"=IFERROR(SUMIFS("
        f"{_txn_range('amount_usd')},"
        f"{_txn_range('category')},{_sheets_string_literal(category)},"
        f"{_txn_range('date')},\">=\"&DATE({year},{month},1),"
        f"{_txn_range('date')},\"<=\"&EOMONTH(DATE({year},{month},1),0)"
        f"),0)"
    )


def monthly_total_cell_formula(*, first_cat_col: str, last_cat_col: str, row: int) -> str:
    return f"=SUM({first_cat_col}{row}:{last_cat_col}{row})"


def column_total_formula(*, col_letter: str) -> str:
    return f"=SUM({col_letter}{ROW_GRID_FIRST}:{col_letter}{ROW_GRID_LAST})"


def year_total_formula(*, total_col_letter: str) -> str:
    """Top-line "Total Spent" for the year — read from the grid grand total."""
    return f"={total_col_letter}{ROW_GRID_TOTAL}"


def year_transactions_formula(*, year: int) -> str:
    return (
        f"=COUNTIFS("
        f"{_txn_range('date')},\">=\"&DATE({year},1,1),"
        f"{_txn_range('date')},\"<=\"&DATE({year},12,31)"
        f")"
    )


def year_avg_per_day_formula(*, total_cell: str, year: int) -> str:
    return (
        f"=IFERROR({total_cell}/MAX(1,"
        f"MIN(TODAY(),DATE({year},12,31))-DATE({year},1,1)+1"
        f"),0)"
    )


def year_avg_per_month_formula(*, total_cell: str, year: int) -> str:
    return (
        f"=IFERROR({total_cell}/MAX(1,"
        f"DATEDIF(DATE({year},1,1),MIN(TODAY(),DATE({year},12,31)),\"M\")+1"
        f"),0)"
    )


def year_largest_single_formula(*, year: int) -> str:
    return (
        f"=IFERROR(MAXIFS("
        f"{_txn_range('amount_usd')},"
        f"{_txn_range('date')},\">=\"&DATE({year},1,1),"
        f"{_txn_range('date')},\"<=\"&DATE({year},12,31)"
        f"),0)"
    )


def top_vendors_query_formula(*, year: int, top_n: int) -> str:
    vendor_col = txn_col_for("vendor")
    date_col = txn_col_for("date")
    usd_col = txn_col_for("amount_usd")
    last_col = col_index_to_letter(len(TRANSACTIONS_COLUMNS) - 1)

    return (
        f"=IFERROR(QUERY(Transactions!A:{last_col},"
        f"\"select {vendor_col}, sum({usd_col}) "
        f"where {date_col} >= date '{year}-01-01' "
        f"and {date_col} <= date '{year}-12-31' "
        f"and {vendor_col} is not null "
        f"and {vendor_col} != '' "
        f"group by {vendor_col} "
        f"order by sum({usd_col}) desc "
        f"limit {top_n} "
        f"label sum({usd_col}) 'Total (USD)'\","
        f"1),\"—\")"
    )


def _sheets_string_literal(s: str) -> str:
    escaped = s.replace('"', '""')
    return f'"{escaped}"'


# ─── Build entrypoint ───────────────────────────────────────────────────

def build_ytd_tab(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    *,
    year: int,
    categories: list[str],
    overwrite: bool = False,
) -> WorksheetHandle:
    """Create or rebuild the YTD <year> tab."""
    if not categories:
        raise SheetFormatError("cannot build a YTD tab with zero categories")

    sheet_name = sheet_format.ytd_sheet_name(year=year)
    layout = YTDLayout(year=year, n_categories=len(categories))

    # Allocate enough rows for the vendor block to spill into.
    rows_needed = ROW_VENDOR_QUERY + sheet_format.ytd.top_vendors_count + 5
    cols_needed = layout.total_cells_count

    if backend.has_worksheet(sheet_name):
        if not overwrite:
            raise SheetsAlreadyExistsError(
                f"worksheet {sheet_name!r} already exists. "
                f"Pass overwrite=True (or use --rebuild-ytd) to replace it."
            )
        backend.delete_worksheet(sheet_name)

    ws = backend.create_worksheet(sheet_name, rows=rows_needed, cols=cols_needed)
    _populate_ytd_tab(ws, sheet_format, layout, categories)
    _format_ytd_tab(ws, sheet_format, layout, categories)
    return ws


def _populate_ytd_tab(
    ws: WorksheetHandle,
    sheet_format: SheetFormat,
    layout: YTDLayout,
    categories: list[str],
) -> None:
    last_col = layout.last_col_letter
    total_col = layout.total_col_letter

    # Title.
    ws.update_values(
        f"A{ROW_TITLE}",
        [[sheet_format.ytd_title(year=layout.year)]],
    )

    # Year summary.
    ws.update_values(f"A{ROW_SUMMARY_TITLE}", [["Year Summary"]])
    ws.update_values(
        f"A{ROW_SUMMARY_FIRST}:B{ROW_SUMMARY_LAST}",
        [
            ["Total Spent", year_total_formula(total_col_letter=total_col)],
            ["Transactions", year_transactions_formula(year=layout.year)],
            ["Avg / day", year_avg_per_day_formula(
                total_cell=f"B{ROW_SUMMARY_FIRST}",
                year=layout.year,
            )],
            ["Avg / month", year_avg_per_month_formula(
                total_cell=f"B{ROW_SUMMARY_FIRST}",
                year=layout.year,
            )],
            ["Largest single", year_largest_single_formula(year=layout.year)],
        ],
    )

    # Grid title + header.
    ws.update_values(f"A{ROW_GRID_TITLE}", [["Monthly by Category"]])
    header = ["Month", *categories, "TOTAL"]
    ws.update_values(f"A{ROW_GRID_HEADER}:{last_col}{ROW_GRID_HEADER}", [header])

    # Grid rows (Jan..Dec).
    grid_rows: list[list[object]] = []
    for month_idx in range(1, 13):
        row_num = ROW_GRID_FIRST + month_idx - 1
        cells: list[object] = [calendar.month_name[month_idx]]
        for cat in categories:
            cells.append(
                monthly_category_cell_formula(
                    year=layout.year, month=month_idx, category=cat,
                )
            )
        cells.append(
            monthly_total_cell_formula(
                first_cat_col=layout.first_cat_col_letter,
                last_cat_col=layout.last_cat_col_letter,
                row=row_num,
            )
        )
        grid_rows.append(cells)
    ws.update_values(
        f"A{ROW_GRID_FIRST}:{last_col}{ROW_GRID_LAST}",
        grid_rows,
    )

    # Total row.
    total_row_cells: list[object] = ["Total"]
    for col_offset in range(layout.n_categories):
        col_letter = col_index_to_letter(1 + col_offset)
        total_row_cells.append(column_total_formula(col_letter=col_letter))
    total_row_cells.append(column_total_formula(col_letter=total_col))
    ws.update_values(
        f"A{ROW_GRID_TOTAL}:{last_col}{ROW_GRID_TOTAL}",
        [total_row_cells],
    )

    # Top Vendors.
    ws.update_values(
        f"A{ROW_VENDOR_TITLE}",
        [[f"Top Vendors — {layout.year}"]],
    )
    ws.update_values(
        f"A{ROW_VENDOR_QUERY}",
        [[top_vendors_query_formula(
            year=layout.year,
            top_n=sheet_format.ytd.top_vendors_count,
        )]],
    )


def _format_ytd_tab(
    ws: WorksheetHandle,
    sheet_format: SheetFormat,
    layout: YTDLayout,
    categories: list[str],
) -> None:
    fmt = sheet_format.ytd.formatting
    last_col = layout.last_col_letter
    total_col = layout.total_col_letter
    n_cats = layout.n_categories

    # Title.
    ws.format_range(
        f"A{ROW_TITLE}:{last_col}{ROW_TITLE}",
        CellFormat(font_size=fmt.title_font_size, bold=fmt.title_bold),
    )

    # Summary.
    ws.format_range(f"A{ROW_SUMMARY_TITLE}", CellFormat(bold=True, font_size=12))
    ws.format_range(
        f"A{ROW_SUMMARY_FIRST}:A{ROW_SUMMARY_LAST}",
        CellFormat(bold=fmt.summary_label_bold),
    )
    ws.format_range(
        f"B{ROW_SUMMARY_FIRST}:B{ROW_SUMMARY_LAST}",
        CellFormat(bold=fmt.summary_value_bold, number_format=fmt.number_format),
    )

    # Grid section title + header.
    ws.format_range(f"A{ROW_GRID_TITLE}", CellFormat(bold=True, font_size=12))
    ws.format_range(
        f"A{ROW_GRID_HEADER}:{last_col}{ROW_GRID_HEADER}",
        CellFormat(
            background_color=fmt.grid_header_background,
            bold=True,
            horizontal_alignment="CENTER",
        ),
    )

    emphasis = sheet_format.emphasis

    # ─── Grid baselines + conditional emphasis ──────────────────────────
    # Quiet baseline for category cells and TOTAL column data cells; loud
    # emphasis applied via conditional bands when value > 0. See
    # month_builder._format_month_tab for the rationale.
    cat_data_range = (
        f"{layout.first_cat_col_letter}{ROW_GRID_FIRST}:"
        f"{layout.last_cat_col_letter}{ROW_GRID_LAST}"
    )
    ws.format_range(
        cat_data_range,
        _style_to_format(emphasis.data_cell_base, number_format=fmt.number_format),
    )

    total_data_range = f"{total_col}{ROW_GRID_FIRST}:{total_col}{ROW_GRID_LAST}"
    ws.format_range(
        total_data_range,
        _style_to_format(
            emphasis.total_cell_base,
            number_format=fmt.number_format,
            background_override=fmt.grid_total_column_background,
        ),
    )

    ws.add_conditional_band(
        ConditionalBand(
            range_a1=cat_data_range,
            predicate_formula=(
                f"={layout.first_cat_col_letter}{ROW_GRID_FIRST}>0"
            ),
            cell_format=_style_to_band_format(emphasis.data_cell_emphasis),
        )
    )
    ws.add_conditional_band(
        ConditionalBand(
            range_a1=total_data_range,
            predicate_formula=f"={total_col}{ROW_GRID_FIRST}>0",
            cell_format=_style_to_band_format(emphasis.total_cell_emphasis),
        )
    )

    # ─── Total row (always-on emphasis) ─────────────────────────────────
    ws.format_range(
        f"A{ROW_GRID_TOTAL}:{last_col}{ROW_GRID_TOTAL}",
        CellFormat(
            background_color=fmt.grid_total_row_background,
            bold=True,
            number_format=fmt.number_format,
        ),
    )
    ws.format_range(
        f"{layout.first_cat_col_letter}{ROW_GRID_TOTAL}:"
        f"{layout.last_cat_col_letter}{ROW_GRID_TOTAL}",
        _style_to_format(emphasis.category_total, number_format=fmt.number_format),
    )
    ws.format_range(
        f"{total_col}{ROW_GRID_TOTAL}",
        _style_to_format(emphasis.grand_total, number_format=fmt.number_format),
    )

    # Vendor section title.
    ws.format_range(
        f"A{ROW_VENDOR_TITLE}",
        CellFormat(bold=fmt.breakdown_title_bold, font_size=12),
    )

    # Freeze.
    if fmt.freeze_rows or fmt.freeze_cols:
        ws.freeze(rows=fmt.freeze_rows, cols=fmt.freeze_cols)

    # Column widths.
    widths = [
        fmt.month_column_width,
        *([fmt.category_column_width] * n_cats),
        fmt.total_column_width,
    ]
    ws.set_column_widths_px(start_col="A", widths=widths)


def _style_to_format(
    style: CellStyle,
    *,
    number_format: str | None = None,
    background_override: str | None = None,
) -> CellFormat:
    """Translate a YAML-defined :class:`CellStyle` to a backend
    :class:`CellFormat`. See :func:`month_builder._style_to_format`."""
    bg = style.background or background_override
    return CellFormat(
        bold=style.bold,
        font_size=style.font_size,
        foreground_color=style.foreground,
        background_color=bg,
        number_format=number_format,
    )


def _style_to_band_format(style: CellStyle) -> CellFormat:
    """Conditional-format-safe :class:`CellFormat`. Strips font_size and
    number_format because Google Sheets doesn't accept them in
    conditional rules — see :func:`month_builder._style_to_band_format`."""
    return CellFormat(
        bold=style.bold,
        foreground_color=style.foreground,
        background_color=style.background,
    )
