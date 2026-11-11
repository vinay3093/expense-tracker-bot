"""Spoken expense numbers and date order for the Transactions tab.

Numbers are assigned once. A later expense with an earlier date gets
the next number, then the rows are sorted by date and time. Existing
values are not rewritten except the two new id columns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ..base import TransactionRow
from .backend import SheetsBackend, col_index_to_letter
from .format import SheetFormat
from .transactions import (
    TRANSACTIONS_COLUMNS,
    header_row,
    index_for,
    init_transactions_tab,
    transaction_row_to_cells,
)

_NO = index_for("expense_no")
_UID = index_for("expense_uid")
_TS = index_for("timestamp")
_COLS = len(TRANSACTIONS_COLUMNS)
_HEADER_READY = "_transactions_header_ready"


@dataclass(frozen=True)
class StoredExpense:
    sheet_row: int
    expense_no: int
    expense_uid: str
    when: date
    category: str
    note: str
    vendor: str
    amount: float
    currency: str
    trip: str
    timestamp: str
    cells: tuple[Any, ...]


def sync_append(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    new_rows: list[TransactionRow],
) -> tuple[list[TransactionRow], list[int]]:
    """Give new rows the next numbers, backfill blanks, and sort by date.

    A row that belongs at the bottom is written by itself. Rewriting the
    whole tab is reserved for a backdated row, or a sheet that still has
    blank numbers.
    """
    records = _ensure(backend, sheet_format)
    placed, fresh = _numbered(records, new_rows)
    if _fits_at_end(records, fresh):
        start = (records[-1].sheet_row + 1) if records else 2
        ordered = sorted(fresh, key=_sort_key)
        _put_rows(backend, sheet_format, start, ordered)
        by_uid = {item.expense_uid: start + offset for offset, item in enumerate(ordered)}
        return placed, [by_uid[row.expense_uid or ""] for row in placed]
    records.extend(fresh)
    ordered = sorted(records, key=_sort_key)
    _write(backend, sheet_format, ordered)
    by_uid = {item.expense_uid: offset + 2 for offset, item in enumerate(ordered)}
    return placed, [by_uid[row.expense_uid or ""] for row in placed]


def _numbered(
    records: list[StoredExpense],
    new_rows: list[TransactionRow],
) -> tuple[list[TransactionRow], list[StoredExpense]]:
    used = {item.expense_no for item in records}
    nxt = max(used, default=0) + 1
    placed: list[TransactionRow] = []
    fresh: list[StoredExpense] = []
    for row in new_rows:
        number = row.expense_no if row.expense_no is not None else nxt
        while number in used:
            number += 1
        uid = row.expense_uid or str(uuid.uuid4())
        used.add(number)
        nxt = max(used) + 1
        ready = TransactionRow(
            date=row.date,
            day=row.day,
            month=row.month,
            year=row.year,
            category=row.category,
            note=row.note,
            vendor=row.vendor,
            amount=row.amount,
            currency=row.currency,
            amount_usd=row.amount_usd,
            fx_rate=row.fx_rate,
            source=row.source,
            trace_id=row.trace_id,
            timestamp=row.timestamp,
            trip=row.trip,
            expense_no=number,
            expense_uid=uid,
        )
        placed.append(ready)
        fresh.append(_from_cells(0, transaction_row_to_cells(ready)))
    return placed, fresh


def _fits_at_end(records: list[StoredExpense], fresh: list[StoredExpense]) -> bool:
    """True when the new rows can sit under the last row without a rewrite."""
    if any(item.sheet_row != index + 2 for index, item in enumerate(records)):
        return False
    if not records:
        return True
    last = _sort_key(records[-1])
    return all(_sort_key(item) >= last for item in fresh)


def _put_rows(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    start_row: int,
    records: list[StoredExpense],
) -> None:
    if not records:
        return
    ws = backend.get_worksheet(sheet_format.transactions.sheet_name)
    end = col_index_to_letter(_COLS - 1)
    last = start_row + len(records) - 1
    ws.update_values(
        f"A{start_row}:{end}{last}",
        [list(item.cells) for item in records],
    )


def load_expenses(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
) -> list[StoredExpense]:
    """Read the ledger, filling any missing numbers and sorting if needed."""
    return _ensure(backend, sheet_format)


def delete_expenses(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    numbers: list[int],
) -> list[StoredExpense]:
    """Delete the numbered rows. Returns the rows that were removed."""
    records = _ensure(backend, sheet_format)
    wanted = set(numbers)
    gone = [item for item in records if item.expense_no in wanted]
    kept = [item for item in records if item.expense_no not in wanted]
    if len(gone) != len(wanted):
        missing = wanted - {item.expense_no for item in gone}
        raise ValueError(f"No expense {', '.join('#' + str(n) for n in sorted(missing))}.")
    _write(backend, sheet_format, kept)
    return gone


def update_expense(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    number: int,
    *,
    amount: float | None = None,
    category: str | None = None,
) -> StoredExpense:
    """Change the amount and/or category of one numbered expense."""
    records = _ensure(backend, sheet_format)
    found = next((item for item in records if item.expense_no == number), None)
    if found is None:
        raise ValueError(f"No expense #{number}.")
    cells = list(found.cells)
    if amount is not None:
        cells[index_for("amount")] = amount
        cells[index_for("amount_usd")] = amount
        cells[index_for("fx_rate")] = 1.0
    if category is not None:
        cells[index_for("category")] = category
    updated = _from_cells(found.sheet_row, cells)
    records = [updated if item.expense_no == number else item for item in records]
    _write(backend, sheet_format, records)
    return updated


def _ensure(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
) -> list[StoredExpense]:
    _ready(backend, sheet_format)
    name = sheet_format.transactions.sheet_name
    ws = backend.get_worksheet(name)
    end = col_index_to_letter(_COLS - 1)
    raw_rows = ws.get_values(f"A2:{end}")
    records: list[StoredExpense] = []
    for offset, raw in enumerate(raw_rows):
        if not raw or raw[0] in ("", None):
            continue
        padded = list(raw) + [""] * (_COLS - len(raw))
        records.append(_from_cells(offset + 2, padded[:_COLS]))
    changed = False
    blanks = [item for item in records if item.expense_no <= 0 or not item.expense_uid]
    if blanks:
        used = {item.expense_no for item in records if item.expense_no > 0}
        nxt = max(used, default=0) + 1
        ordered = sorted(blanks, key=_sort_key)
        rebuilt: dict[int, StoredExpense] = {}
        for item in ordered:
            number = item.expense_no if item.expense_no > 0 else nxt
            while number in used:
                number += 1
            used.add(number)
            nxt = max(used) + 1
            uid = item.expense_uid or str(uuid.uuid4())
            cells = list(item.cells)
            cells[_NO] = number
            cells[_UID] = uid
            rebuilt[item.sheet_row] = _from_cells(item.sheet_row, cells)
        records = [rebuilt.get(item.sheet_row, item) for item in records]
        changed = True
    ordered_now = sorted(records, key=_sort_key)
    if [item.expense_no for item in ordered_now] != [item.expense_no for item in records]:
        records = ordered_now
        changed = True
    if changed:
        _write(backend, sheet_format, records)
        return _ensure(backend, sheet_format)
    return records


def _write(
    backend: SheetsBackend,
    sheet_format: SheetFormat,
    records: list[StoredExpense],
) -> None:
    ordered = sorted(records, key=_sort_key)
    ws = backend.get_worksheet(sheet_format.transactions.sheet_name)
    end = col_index_to_letter(_COLS - 1)
    if ordered:
        ws.update_values(
            f"A2:{end}{1 + len(ordered)}",
            [list(item.cells) for item in ordered],
        )
    previous = ws.get_values("A2:A")
    filled = sum(1 for row in previous if row and row[0] not in ("", None))
    extra = filled - len(ordered)
    if extra > 0:
        ws.delete_rows(2 + len(ordered), 1 + filled)


def _ready(backend: SheetsBackend, sheet_format: SheetFormat) -> None:
    """Create or widen the header. Skip the check once this connection has seen it."""
    if getattr(backend, _HEADER_READY, False):
        return
    name = sheet_format.transactions.sheet_name
    expected = header_row()
    if not backend.has_worksheet(name):
        init_transactions_tab(backend, sheet_format)
        setattr(backend, _HEADER_READY, True)
        return
    ws = backend.get_worksheet(name)
    end = col_index_to_letter(_COLS - 1)
    header = ws.get_values(f"A1:{end}1")
    actual = [cell for cell in (header[0] if header else []) if cell not in ("", None)]
    if actual == expected:
        setattr(backend, _HEADER_READY, True)
        return
    if actual and actual == expected[: len(actual)]:
        init_transactions_tab(backend, sheet_format)
        setattr(backend, _HEADER_READY, True)
        return
    if not actual:
        init_transactions_tab(backend, sheet_format)
        setattr(backend, _HEADER_READY, True)


def _from_cells(sheet_row: int, cells: list[Any]) -> StoredExpense:
    padded = list(cells) + [""] * (_COLS - len(cells))
    when = _as_date(padded[0])
    return StoredExpense(
        sheet_row=sheet_row,
        expense_no=_as_int(padded[_NO]),
        expense_uid=str(padded[_UID] or ""),
        when=when,
        category=str(padded[index_for("category")] or ""),
        note=str(padded[index_for("note")] or ""),
        vendor=str(padded[index_for("vendor")] or ""),
        amount=_as_float(padded[index_for("amount")]),
        currency=str(padded[index_for("currency")] or "USD"),
        trip=str(padded[index_for("trip")] or ""),
        timestamp=str(padded[_TS] or ""),
        cells=tuple(padded[:_COLS]),
    )


def _sort_key(item: StoredExpense) -> tuple[date, str, int]:
    return (item.when, item.timestamp, item.expense_no)


def _as_date(raw: Any) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()[:10]
    return date.fromisoformat(text)


def _as_int(raw: Any) -> int:
    try:
        return int(float(str(raw).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _as_float(raw: Any) -> float:
    try:
        return float(str(raw).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return 0.0
