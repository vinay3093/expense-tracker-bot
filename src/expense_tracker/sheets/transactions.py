"""The Transactions tab — master ledger.

This module is the **structural contract** for the Transactions tab.
Column order, headers, data types, and formula references all flow
from the :data:`TRANSACTIONS_COLUMNS` list defined here. Every other
piece of the Sheets layer (monthly tab formulas, YTD tab formulas,
the chat → row writer in Step 5) reads from this single source.

If you rename a column or reorder the list, monthly/YTD formulas
update automatically — they look up cell letters via :func:`col_for`.

Why hard-code the schema here rather than read it from YAML:

* Formula correctness is *unit-testable* (and tested!). YAML isn't.
* The schema is the bot's contract; change requires a code release.
* Saves a round-trip through Pydantic validation for an immutable list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from .backend import (
    CellFormat,
    ConditionalBand,
    SheetsBackend,
    WorksheetHandle,
    col_index_to_letter,
)
from .format import SheetFormat


class ColumnType(str, Enum):
    """How a column should be formatted in the spreadsheet."""

    DATETIME = "datetime"
    DATE = "date"
    TEXT = "text"
    NUMBER = "number"


@dataclass(frozen=True)
class TransactionColumn:
    key: str           # internal identifier — used by writers + formulas
    header: str        # what shows in row 1
    type: ColumnType   # drives number_format / display
    description: str = ""


# ─── The canonical Transactions schema ──────────────────────────────────
# Order here = order in the spreadsheet. Adding new columns appends to
# the right; never insert in the middle without a migration plan.

TRANSACTIONS_COLUMNS: tuple[TransactionColumn, ...] = (
    TransactionColumn(
        key="timestamp",
        header="Timestamp",
        type=ColumnType.DATETIME,
        description="When the bot wrote the row (ISO 8601, in user TZ).",
    ),
    TransactionColumn(
        key="date",
        header="Date",
        type=ColumnType.DATE,
        description="Date the expense was incurred (user TZ).",
    ),
    TransactionColumn(
        key="day",
        header="Day",
        type=ColumnType.TEXT,
        description='Weekday name ("Mon", "Tue", ...) — derived from date.',
    ),
    TransactionColumn(
        key="month",
        header="Month",
        type=ColumnType.TEXT,
        description='"YYYY-MM" — easy filter / pivot key.',
    ),
    TransactionColumn(
        key="category",
        header="Category",
        type=ColumnType.TEXT,
        description="Canonical category name (matches categories.yaml).",
    ),
    TransactionColumn(
        key="note",
        header="Note",
        type=ColumnType.TEXT,
        description='Free-text "tag" within a category — e.g. "coffee", "rent".',
    ),
    TransactionColumn(
        key="vendor",
        header="Vendor",
        type=ColumnType.TEXT,
        description='Where it was spent — e.g. "Starbucks", "Trader Joe\'s".',
    ),
    TransactionColumn(
        key="amount",
        header="Amount",
        type=ColumnType.NUMBER,
        description="Original amount the user logged.",
    ),
    TransactionColumn(
        key="currency",
        header="Currency",
        type=ColumnType.TEXT,
        description="ISO-4217 of the original amount.",
    ),
    TransactionColumn(
        key="amount_usd",
        header="Amount (USD)",
        type=ColumnType.NUMBER,
        description="Converted to primary currency — what monthly + YTD sums use.",
    ),
    TransactionColumn(
        key="fx_rate",
        header="FX Rate",
        type=ColumnType.NUMBER,
        description='1.0 when currency == primary; otherwise "primary per source".',
    ),
    TransactionColumn(
        key="source",
        header="Source",
        type=ColumnType.TEXT,
        description='How the row was entered — "chat", "cli", "manual".',
    ),
    TransactionColumn(
        key="trace_id",
        header="Trace ID",
        type=ColumnType.TEXT,
        description="LLM call ID that produced this row (for audit / debugging).",
    ),
)


# ─── Lookup helpers ─────────────────────────────────────────────────────

_KEY_TO_INDEX: dict[str, int] = {col.key: i for i, col in enumerate(TRANSACTIONS_COLUMNS)}


def index_for(key: str) -> int:
    """0-based column index for ``key``. Raises if unknown."""
    if key not in _KEY_TO_INDEX:
        raise KeyError(f"unknown Transactions column key: {key!r}")
    return _KEY_TO_INDEX[key]


def col_for(key: str) -> str:
    """Spreadsheet column letter for ``key`` (e.g. ``'category'`` -> ``'E'``)."""
    return col_index_to_letter(index_for(key))


def header_row() -> list[str]:
    """The first row written into a fresh Transactions tab."""
    return [col.header for col in TRANSACTIONS_COLUMNS]


# ─── A row payload ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class TransactionRow:
    """One row about to be appended to the Transactions tab.

    Fields map 1-to-1 to :data:`TRANSACTIONS_COLUMNS`. ``amount_usd``
    and ``fx_rate`` are computed by the currency module before this
    object is built — see :mod:`expense_tracker.sheets.currency`.
    """

    timestamp: datetime
    date: date
    day: str
    month: str
    category: str
    note: str | None
    vendor: str | None
    amount: float
    currency: str
    amount_usd: float
    fx_rate: float
    source: str = "chat"
    trace_id: str | None = None

    def as_row(self) -> list[Any]:
        """Project to the cell-list order used by the backend."""
        return [
            self.timestamp.isoformat(timespec="seconds"),
            self.date.isoformat(),
            self.day,
            self.month,
            self.category,
            self.note or "",
            self.vendor or "",
            self.amount,
            self.currency,
            self.amount_usd,
            self.fx_rate,
            self.source,
            self.trace_id or "",
        ]


# ─── Tab init / append ──────────────────────────────────────────────────

def init_transactions_tab(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    *,
    expected_rows: int = 5000,
) -> WorksheetHandle:
    """Create the Transactions tab if it doesn't exist; format it in place.

    Idempotent: if the tab already exists with the same header row, just
    re-applies formatting (cheap). If the header row is wrong, raises.
    """
    from .exceptions import SheetFormatError

    name = sheet_format.transactions.sheet_name
    cols = len(TRANSACTIONS_COLUMNS)

    if backend.has_worksheet(name):
        ws = backend.get_worksheet(name)
        existing_header = ws.get_values(f"A1:{col_index_to_letter(cols - 1)}1")
        actual = existing_header[0] if existing_header else []
        # Trim trailing empty cells before comparing — Sheets sometimes
        # returns the header padded out to the worksheet's col_count.
        actual_trimmed = [c for c in actual if c not in ("", None)]
        if actual_trimmed and actual_trimmed != header_row():
            raise SheetFormatError(
                f"Transactions tab {name!r} has unexpected header row: "
                f"got {actual_trimmed!r}, expected {header_row()!r}. "
                "Refusing to overwrite — rename the existing tab and re-run."
            )
    else:
        # Allocate generously so append doesn't have to grow rows often.
        rows = max(expected_rows, 200)
        ws = backend.create_worksheet(name, rows=rows, cols=cols)
        ws.update_values(f"A1:{col_index_to_letter(cols - 1)}1", [header_row()])

    _apply_transactions_formatting(ws, sheet_format)
    return ws


def append_transactions(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    rows: list[TransactionRow],
) -> WorksheetHandle:
    """Append one or more rows. Creates the tab if it's missing."""
    if not rows:
        ws = (
            backend.get_worksheet(sheet_format.transactions.sheet_name)
            if backend.has_worksheet(sheet_format.transactions.sheet_name)
            else init_transactions_tab(backend, sheet_format)
        )
        return ws

    if not backend.has_worksheet(sheet_format.transactions.sheet_name):
        init_transactions_tab(backend, sheet_format)
    ws = backend.get_worksheet(sheet_format.transactions.sheet_name)
    ws.append_rows([r.as_row() for r in rows])
    return ws


def _apply_transactions_formatting(ws: WorksheetHandle, sheet_format: SheetFormat) -> None:
    """Apply header colors, freeze, widths, and the month-band rule."""
    fmt = sheet_format.transactions.formatting
    cols = len(TRANSACTIONS_COLUMNS)
    last_col_letter = col_index_to_letter(cols - 1)

    ws.format_range(
        f"A1:{last_col_letter}1",
        CellFormat(
            background_color=fmt.header_background,
            foreground_color=fmt.header_foreground,
            bold=fmt.header_bold,
        ),
    )

    if fmt.freeze_rows:
        ws.freeze(rows=fmt.freeze_rows)

    # Numeric columns get a number format applied to the whole column
    # (rows 2 onwards) so freshly-appended rows render correctly.
    for c in TRANSACTIONS_COLUMNS:
        if c.type is not ColumnType.NUMBER:
            continue
        letter = col_for(c.key)
        ws.format_range(
            f"{letter}2:{letter}",
            CellFormat(number_format=fmt.number_format),
        )

    # Column widths.
    if fmt.column_widths:
        widths = [
            fmt.column_widths.get(c.key, 100) for c in TRANSACTIONS_COLUMNS
        ]
        ws.set_column_widths_px(start_col="A", widths=widths)

    # Alternating month bands using a conditional format predicate on
    # the Date column. Rows where the month number is even get the band
    # color; odd months stay white. The result is a clean visual break
    # between months without inserting rows that would corrupt SUMIFS.
    if fmt.month_band_color:
        date_col = col_for("date")
        ws.add_conditional_band(
            ConditionalBand(
                range_a1=f"A2:{last_col_letter}",
                predicate_formula=f"=ISEVEN(MONTH(${date_col}2))",
                background_color=fmt.month_band_color,
            )
        )
