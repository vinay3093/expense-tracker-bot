"""Unit tests for the Transactions tab schema, init, and append helpers."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from expense_tracker.sheets import (
    TRANSACTIONS_COLUMNS,
    FakeSheetsBackend,
    SheetFormat,
    TransactionRow,
    append_transactions,
    init_transactions_tab,
    transactions_col_for,
    transactions_header_row,
    transactions_index_for,
)
from expense_tracker.sheets.backend import _FakeWorksheet
from expense_tracker.sheets.exceptions import SheetFormatError
from expense_tracker.sheets.transactions import ColumnType

# ─── Schema lookups ────────────────────────────────────────────────────

def test_columns_have_unique_keys_and_headers():
    keys = [c.key for c in TRANSACTIONS_COLUMNS]
    headers = [c.header for c in TRANSACTIONS_COLUMNS]
    assert len(keys) == len(set(keys)), "duplicate keys"
    assert len(headers) == len(set(headers)), "duplicate headers"


def test_index_for_known_keys():
    # New schema: Date is the leftmost column, Timestamp is the rightmost.
    assert transactions_index_for("date") == 0
    assert transactions_index_for("category") == 4
    assert transactions_index_for("timestamp") == len(TRANSACTIONS_COLUMNS) - 1


def test_index_for_unknown_raises():
    with pytest.raises(KeyError):
        transactions_index_for("not_a_key")


def test_col_for_returns_letters():
    assert transactions_col_for("date") == "A"
    assert transactions_col_for("day") == "B"
    assert transactions_col_for("month") == "C"
    assert transactions_col_for("year") == "D"
    assert transactions_col_for("category") == "E"
    # ``amount_usd`` is at index 9 → column J.
    assert transactions_col_for("amount_usd") == "J"
    # ``timestamp`` lives at the very right (after Trace ID).
    assert transactions_col_for("timestamp") == "N"


def test_header_row_matches_columns():
    assert transactions_header_row() == [c.header for c in TRANSACTIONS_COLUMNS]


def test_amount_columns_are_numeric():
    by_key = {c.key: c for c in TRANSACTIONS_COLUMNS}
    assert by_key["amount"].type is ColumnType.NUMBER
    assert by_key["amount_usd"].type is ColumnType.NUMBER
    assert by_key["fx_rate"].type is ColumnType.NUMBER
    assert by_key["year"].type is ColumnType.NUMBER


# ─── TransactionRow projection ─────────────────────────────────────────

def test_transaction_row_as_row_order_matches_columns():
    row = TransactionRow(
        date=date(2026, 4, 24),
        day="Fri",
        month="April",
        year=2026,
        category="Food",
        note="coffee",
        vendor="Starbucks",
        amount=4.50,
        currency="USD",
        amount_usd=4.50,
        fx_rate=1.0,
        source="chat",
        trace_id="req_abc",
        timestamp=datetime(2026, 4, 24, 12, 0, 0),
    )
    cells = row.as_row()
    assert len(cells) == len(TRANSACTIONS_COLUMNS)
    assert cells[transactions_index_for("category")] == "Food"
    assert cells[transactions_index_for("amount")] == 4.50
    assert cells[transactions_index_for("amount_usd")] == 4.50
    assert cells[transactions_index_for("fx_rate")] == 1.0
    assert cells[transactions_index_for("trace_id")] == "req_abc"
    assert cells[transactions_index_for("month")] == "April"
    assert cells[transactions_index_for("year")] == 2026
    # Date/timestamp serialised to ISO strings.
    assert cells[transactions_index_for("date")] == "2026-04-24"
    assert cells[transactions_index_for("timestamp")] == "2026-04-24T12:00:00"


def test_transaction_row_handles_missing_optional_fields():
    row = TransactionRow(
        date=date(2026, 4, 24),
        day="Fri",
        month="April",
        year=2026,
        category="Misc",
        note=None,
        vendor=None,
        amount=10.0,
        currency="USD",
        amount_usd=10.0,
        fx_rate=1.0,
    )
    cells = row.as_row()
    assert cells[transactions_index_for("note")] == ""
    assert cells[transactions_index_for("vendor")] == ""
    assert cells[transactions_index_for("trace_id")] == ""
    # Timestamp is optional (defaults to None) — serialised as "".
    assert cells[transactions_index_for("timestamp")] == ""
    # Default ``source`` is "chat".
    assert cells[transactions_index_for("source")] == "chat"


# ─── init_transactions_tab ─────────────────────────────────────────────

def test_init_creates_tab_with_header_and_formatting():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    ws = init_transactions_tab(b, fmt)
    assert b.has_worksheet("Transactions")
    assert isinstance(ws, _FakeWorksheet)

    last_letter = transactions_col_for(TRANSACTIONS_COLUMNS[-1].key)
    actual_header = ws.get_values(f"A1:{last_letter}1")[0]
    assert actual_header == [c.header for c in TRANSACTIONS_COLUMNS]

    # Header formatting should have been applied.
    formatted_ranges = [r for (r, _f) in ws.format_calls()]
    assert any(r.startswith("A1:") for r in formatted_ranges)
    # Freeze row 1.
    assert ws.freeze_state[0] == 1
    # Conditional band should be installed for month banding. The band
    # references ``$A`` (the Date column) under the new schema.
    assert len(ws.conditional_bands) == 1
    band = ws.conditional_bands[0]
    assert "MONTH" in band.predicate_formula
    assert "$A" in band.predicate_formula


def test_init_idempotent_when_header_matches():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    init_transactions_tab(b, fmt)
    # Second call should not raise / not create a duplicate.
    init_transactions_tab(b, fmt)
    assert sum(1 for w in b.list_worksheets() if w.title == "Transactions") == 1


def test_init_rejects_existing_tab_with_wrong_headers():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    ws = b.create_worksheet("Transactions")
    ws.update_values("A1:B1", [["Date", "Stuff"]])
    with pytest.raises(SheetFormatError):
        init_transactions_tab(b, fmt)


def test_init_uses_custom_sheet_name():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({"transactions": {"sheet_name": "Ledger"}})
    init_transactions_tab(b, fmt)
    assert b.has_worksheet("Ledger")
    assert not b.has_worksheet("Transactions")


# ─── append_transactions ───────────────────────────────────────────────

def test_append_creates_tab_and_writes_row():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    row = TransactionRow(
        date=date(2026, 4, 24),
        day="Fri",
        month="April",
        year=2026,
        category="Food",
        note="coffee",
        vendor="Starbucks",
        amount=4.5,
        currency="USD",
        amount_usd=4.5,
        fx_rate=1.0,
        timestamp=datetime(2026, 4, 24, 12, 0),
    )
    append_transactions(b, fmt, [row])
    ws = b.get_worksheet("Transactions")
    assert isinstance(ws, _FakeWorksheet)
    # Header still in row 1, Date column is leftmost.
    assert ws.cell("A1") == "Date"
    assert ws.cell(f"{transactions_col_for('category')}2") == "Food"
    assert ws.cell(f"{transactions_col_for('amount_usd')}2") == 4.5
    assert ws.cell(f"{transactions_col_for('month')}2") == "April"
    assert ws.cell(f"{transactions_col_for('year')}2") == 2026


def test_append_empty_list_just_ensures_tab_exists():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    append_transactions(b, fmt, [])
    assert b.has_worksheet("Transactions")


def test_append_multiple_rows_appended_in_order():
    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    rows = [
        TransactionRow(
            date=date(2026, 4, d),
            day="Mon",
            month="April",
            year=2026,
            category="Food",
            note=None,
            vendor=None,
            amount=10.0 * d,
            currency="USD",
            amount_usd=10.0 * d,
            fx_rate=1.0,
            timestamp=datetime(2026, 4, d, 12, 0),
        )
        for d in (1, 2, 3)
    ]
    append_transactions(b, fmt, rows)
    ws = b.get_worksheet("Transactions")
    assert isinstance(ws, _FakeWorksheet)
    amount_col = transactions_col_for("amount_usd")
    assert ws.cell(f"{amount_col}2") == 10.0
    assert ws.cell(f"{amount_col}3") == 20.0
    assert ws.cell(f"{amount_col}4") == 30.0


# ─── reinit_transactions_tab ───────────────────────────────────────────

def test_reinit_creates_tab_when_missing():
    from expense_tracker.sheets.transactions import reinit_transactions_tab

    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    ws = reinit_transactions_tab(b, fmt)
    assert b.has_worksheet("Transactions")
    last_letter = transactions_col_for(TRANSACTIONS_COLUMNS[-1].key)
    assert ws.get_values(f"A1:{last_letter}1")[0] == [
        c.header for c in TRANSACTIONS_COLUMNS
    ]


# ─── get_last_row / delete_last_row / update_last_row_fields ───────────


def _seed_two_rows(backend, fmt) -> None:
    """Append two rows so we can assert "bottom-most" semantics."""
    rows = [
        TransactionRow(
            date=date(2026, 4, 24),
            day="Fri",
            month="April",
            year=2026,
            category="Food",
            note="coffee",
            vendor="Starbucks",
            amount=4.5,
            currency="USD",
            amount_usd=4.5,
            fx_rate=1.0,
            timestamp=datetime(2026, 4, 24, 12, 0),
        ),
        TransactionRow(
            date=date(2026, 4, 25),
            day="Sat",
            month="April",
            year=2026,
            category="Saloon",
            note="haircut",
            vendor="Supercuts",
            amount=30.0,
            currency="USD",
            amount_usd=30.0,
            fx_rate=1.0,
            timestamp=datetime(2026, 4, 25, 14, 30),
        ),
    ]
    append_transactions(backend, fmt, rows)


def test_get_last_row_returns_bottom_most_data_row():
    from expense_tracker.sheets.transactions import get_last_row

    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    _seed_two_rows(b, fmt)

    snap = get_last_row(b, fmt)
    assert snap.is_empty is False
    assert snap.row_index == 3  # header row + two data rows
    assert snap.value("category") == "Saloon"
    assert snap.value("amount") == 30.0
    assert snap.value("date") == "2026-04-25"


def test_get_last_row_handles_empty_tab():
    from expense_tracker.sheets.transactions import get_last_row

    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    init_transactions_tab(b, fmt)

    snap = get_last_row(b, fmt)
    assert snap.is_empty
    assert snap.row_index is None
    assert snap.values == []


def test_get_last_row_handles_missing_tab():
    from expense_tracker.sheets.transactions import get_last_row

    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    snap = get_last_row(b, fmt)
    assert snap.is_empty


def test_delete_last_row_removes_bottom_row_only():
    from expense_tracker.sheets.transactions import delete_last_row, get_last_row

    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    _seed_two_rows(b, fmt)

    deleted = delete_last_row(b, fmt)
    assert deleted.value("category") == "Saloon"

    # The previous row should now be the new bottom-most.
    snap = get_last_row(b, fmt)
    assert snap.row_index == 2
    assert snap.value("category") == "Food"


def test_delete_last_row_on_empty_returns_empty_snap():
    from expense_tracker.sheets.transactions import delete_last_row

    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    init_transactions_tab(b, fmt)

    snap = delete_last_row(b, fmt)
    assert snap.is_empty


def test_update_last_row_fields_patches_only_named_cols():
    from expense_tracker.sheets.transactions import (
        get_last_row,
        update_last_row_fields,
    )

    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    _seed_two_rows(b, fmt)

    before = update_last_row_fields(
        b, fmt, updates={"category": "Shopping", "amount": 50.0},
    )
    assert before.value("category") == "Saloon"  # pre-edit snapshot
    assert before.value("amount") == 30.0

    after = get_last_row(b, fmt)
    assert after.row_index == before.row_index  # same row, in-place
    assert after.value("category") == "Shopping"
    assert after.value("amount") == 50.0
    # Untouched columns survive the patch.
    assert after.value("note") == "haircut"
    assert after.value("currency") == "USD"


def test_update_last_row_fields_on_empty_is_noop():
    from expense_tracker.sheets.transactions import update_last_row_fields

    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    init_transactions_tab(b, fmt)

    snap = update_last_row_fields(b, fmt, updates={"category": "Food"})
    assert snap.is_empty


def test_reinit_wipes_existing_rows():
    """The whole point of reinit: throw away existing data."""
    from expense_tracker.sheets.transactions import reinit_transactions_tab

    b = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    init_transactions_tab(b, fmt)
    row = TransactionRow(
        date=date(2026, 4, 24),
        day="Fri",
        month="April",
        year=2026,
        category="Food",
        note="coffee",
        vendor=None,
        amount=4.5,
        currency="USD",
        amount_usd=4.5,
        fx_rate=1.0,
    )
    append_transactions(b, fmt, [row])
    ws_before = b.get_worksheet("Transactions")
    assert isinstance(ws_before, _FakeWorksheet)
    cat_col = transactions_col_for("category")
    assert ws_before.cell(f"{cat_col}2") == "Food"

    reinit_transactions_tab(b, fmt)
    ws_after = b.get_worksheet("Transactions")
    assert isinstance(ws_after, _FakeWorksheet)
    # Row 2 should now be empty (header still in row 1).
    assert ws_after.cell(f"{cat_col}2") == ""
