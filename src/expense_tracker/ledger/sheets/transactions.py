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
from enum import Enum
from typing import Any

from ..base import LastRow, TransactionRow
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
#
# Layout intent: the leftmost columns are the user's quick-scan info
# (when, what, how much). Audit metadata (Source, Trace ID, Timestamp)
# is pushed to the right. ``Timestamp`` is the *write* time — distinct
# from ``Date``, which is the *expense* time. Keeping it on the right
# means a row backdated by a week visibly shows "I logged this late"
# without dominating the user's view.

TRANSACTIONS_COLUMNS: tuple[TransactionColumn, ...] = (
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
        description='Full month name like "April" — easy filter / pivot key.',
    ),
    TransactionColumn(
        key="year",
        header="Year",
        type=ColumnType.NUMBER,
        description="4-digit year, e.g. 2026.",
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
    TransactionColumn(
        key="timestamp",
        header="Timestamp",
        type=ColumnType.DATETIME,
        description=(
            "When the bot wrote the row (ISO 8601, user TZ). Distinct "
            "from Date — the gap between them shows how late an expense "
            "was logged."
        ),
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


# ─── Sheets-specific projection ─────────────────────────────────────────

def transaction_row_to_cells(row: TransactionRow) -> list[Any]:
    """Project a :class:`TransactionRow` to the Sheets cell-list layout.

    Order matches :data:`TRANSACTIONS_COLUMNS`.  Strings used for
    blank optional cells (``note``, ``vendor``, ``trace_id``) so the
    Sheets API doesn't render ``None`` as ``"None"``.
    """
    ts = row.timestamp.isoformat(timespec="seconds") if row.timestamp else ""
    return [
        row.date.isoformat(),
        row.day,
        row.month,
        row.year,
        row.category,
        row.note or "",
        row.vendor or "",
        row.amount,
        row.currency,
        row.amount_usd,
        row.fx_rate,
        row.source,
        row.trace_id or "",
        ts,
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


def reinit_transactions_tab(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
) -> WorksheetHandle:
    """Wipe the existing Transactions tab (if any) and create it fresh.

    Destructive: every row is lost. Used after a schema change so the
    bot can rebuild the master ledger with the new column layout
    instead of refusing to write into a tab whose header doesn't match.
    """
    name = sheet_format.transactions.sheet_name
    if backend.has_worksheet(name):
        backend.delete_worksheet(name)
    return init_transactions_tab(backend, sheet_format)


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
    ws.append_rows([transaction_row_to_cells(r) for r in rows])
    return ws


# ─── Last-row read / delete / edit (used by /undo & /edit) ─────────────


def _empty_last_row() -> LastRow:
    """Snapshot used when the Transactions tab has no data rows."""
    return LastRow(is_empty=True, row_index=None, values={})


def _row_to_values_dict(values: list[Any]) -> dict[str, Any]:
    """Project a positional Sheets row to the canonical key map.

    Cells beyond the row's actual length are filled with empty
    strings — matches the tolerant style of the old
    ``LastRow.value()`` accessor.
    """
    out: dict[str, Any] = {}
    for col in TRANSACTIONS_COLUMNS:
        idx = index_for(col.key)
        out[col.key] = values[idx] if idx < len(values) else ""
    return out


def get_last_row(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
) -> LastRow:
    """Return the bottom-most data row of the Transactions tab.

    "Bottom-most" = highest 1-based row index whose ``Date`` column
    (column A) is non-empty. Header row (row 1) is excluded.
    """
    name = sheet_format.transactions.sheet_name
    if not backend.has_worksheet(name):
        return _empty_last_row()

    ws = backend.get_worksheet(name)
    last_col_letter = col_index_to_letter(len(TRANSACTIONS_COLUMNS) - 1)

    date_col_letter = col_for("date")
    date_values = ws.get_values(f"{date_col_letter}2:{date_col_letter}")

    last_data_row_offset: int | None = None
    for offset, row in enumerate(date_values):
        cell = row[0] if row else ""
        if cell not in ("", None):
            last_data_row_offset = offset

    if last_data_row_offset is None:
        return _empty_last_row()

    sheet_row = 2 + last_data_row_offset
    full_row = ws.get_values(f"A{sheet_row}:{last_col_letter}{sheet_row}")
    values = full_row[0] if full_row else []
    return LastRow(
        is_empty=False,
        row_index=sheet_row,
        values=_row_to_values_dict(list(values)),
    )


def delete_last_row(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
) -> LastRow:
    """Delete the Transactions bottom-most data row, return its snapshot.

    Returns the deleted row's snapshot so the caller can echo "deleted X"
    or push it onto an undo-undo stack later. ``LastRow.is_empty == True``
    means there was nothing to delete.
    """
    snap = get_last_row(backend, sheet_format)
    if snap.is_empty:
        return snap
    ws = backend.get_worksheet(sheet_format.transactions.sheet_name)
    assert snap.row_index is not None
    ws.delete_rows(snap.row_index)
    return snap


def update_last_row_fields(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    *,
    updates: dict[str, Any],
) -> LastRow:
    """Patch named columns on the bottom-most data row.

    ``updates`` is keyed by :class:`TransactionColumn.key` (e.g.
    ``"category"``, ``"amount"``). Returns the *pre-edit* snapshot so
    the chat layer can show a "changed X to Y" diff.
    """
    snap = get_last_row(backend, sheet_format)
    if snap.is_empty:
        return snap
    ws = backend.get_worksheet(sheet_format.transactions.sheet_name)
    assert snap.row_index is not None
    for key, new_value in updates.items():
        col_letter = col_for(key)
        ws.update_values(
            f"{col_letter}{snap.row_index}",
            [[new_value]],
        )
    return snap


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
    # ``year`` is special-cased: a plain integer renders cleaner as
    # "2026" than as "2,026.00".
    for c in TRANSACTIONS_COLUMNS:
        if c.type is not ColumnType.NUMBER:
            continue
        letter = col_for(c.key)
        col_number_format = "0" if c.key == "year" else fmt.number_format
        ws.format_range(
            f"{letter}2:{letter}",
            CellFormat(number_format=col_number_format),
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


__all__ = [
    "TRANSACTIONS_COLUMNS",
    "ColumnType",
    "LastRow",
    "TransactionColumn",
    "TransactionRow",
    "append_transactions",
    "col_for",
    "delete_last_row",
    "get_last_row",
    "header_row",
    "index_for",
    "init_transactions_tab",
    "reinit_transactions_tab",
    "transaction_row_to_cells",
    "update_last_row_fields",
]
