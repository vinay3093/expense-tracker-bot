"""Spoken expense numbers stay put when rows are sorted by date."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from expense_tracker.chat_actions import handle_chat_action
from expense_tracker.ledger.base import TransactionRow
from expense_tracker.ledger.sheets import (
    FakeSheetsBackend,
    SheetFormat,
    append_transactions,
)
from expense_tracker.ledger.sheets.identity import load_expenses, sync_append


def _row(day: int, *, note: str, stamp: str) -> TransactionRow:
    return TransactionRow(
        date=date(2026, 11, day),
        day="Wed",
        month="November",
        year=2026,
        category="Food",
        note=note,
        vendor="Cafe",
        amount=10.0 + day,
        currency="USD",
        amount_usd=10.0 + day,
        fx_rate=1.0,
        timestamp=datetime.fromisoformat(stamp),
        trip="California",
    )


def _ledger():
    backend = FakeSheetsBackend()
    fmt = SheetFormat.from_dict({})
    return backend, fmt


def test_later_expense_writes_one_row():
    backend, fmt = _ledger()
    sync_append(backend, fmt, [_row(1, note="first", stamp="2026-11-01T09:00:00")])
    ws = backend.get_worksheet("Transactions")
    calls: list[tuple[str, int]] = []
    original = ws.update_values

    def spy(range_a1, values):
        calls.append((range_a1, len(values)))
        return original(range_a1, values)

    ws.update_values = spy
    _, indexes = sync_append(backend, fmt, [_row(4, note="second", stamp="2026-11-04T09:00:00")])
    assert indexes == [3]
    assert calls == [(calls[0][0], 1)]
    assert calls[0][0].startswith("A3:")
    rows = load_expenses(backend, fmt)
    assert [item.note for item in rows] == ["first", "second"]
    assert [item.expense_no for item in rows] == [1, 2]


def test_earlier_date_logged_later_gets_the_next_number_and_sorts_up():
    backend, fmt = _ledger()
    sync_append(backend, fmt, [_row(4, note="later day", stamp="2026-11-04T09:00:00")])
    sync_append(backend, fmt, [_row(1, note="earlier day", stamp="2026-11-05T09:00:00")])
    rows = load_expenses(backend, fmt)
    assert [item.when.day for item in rows] == [1, 4]
    assert [item.expense_no for item in rows] == [2, 1]
    assert rows[0].note == "earlier day"
    assert all(item.expense_uid for item in rows)


def test_blank_rows_are_numbered_in_date_order_once():
    backend, fmt = _ledger()
    append_transactions(backend, fmt, [
        _row(4, note="second", stamp="2026-11-04T09:00:00"),
        _row(1, note="first", stamp="2026-11-01T09:00:00"),
    ])
    rows = load_expenses(backend, fmt)
    by_note = {item.note: item.expense_no for item in rows}
    assert by_note == {"first": 1, "second": 2}
    assert [item.when.day for item in rows] == [1, 4]


def test_chat_commands_find_switch_show_and_delete():
    backend, fmt = _ledger()
    sync_append(backend, fmt, [
        _row(4, note="later", stamp="2026-11-04T09:00:00"),
        _row(1, note="early", stamp="2026-11-01T09:00:00"),
    ])
    pipeline = SimpleNamespace(
        trips=SimpleNamespace(
            use_daily=lambda: "Mode: Daily",
            resume=lambda label: f"Mode: {label}",
            close_named=lambda label: f"Closed {label}",
            stamp_copies=lambda name, rows: None,
            update_copy=lambda *args, **kwargs: None,
            _by_name=lambda name: None,
        ),
        _logger=SimpleNamespace(
            _ledger=SimpleNamespace(sheets_backend=backend, sheet_format=fmt),
            _registry=SimpleNamespace(resolve_or_fallback=lambda raw: raw),
        ),
    )
    assert handle_chat_action("switch to daily", pipeline) == "Mode: Daily"
    assert "california 2026" in handle_chat_action("switch to California 2026", pipeline).lower()
    assert handle_chat_action("end the California trip", pipeline).startswith("Closed")
    shown = handle_chat_action("show me November 2026", pipeline)
    assert "#1" in shown and "#2" in shown
    assert shown.index("#2") < shown.index("#1")
    described = handle_chat_action("what is expense 2", pipeline)
    assert "early" in described
    deleted = handle_chat_action("delete expense 2", pipeline)
    assert "Deleted" in deleted
    assert handle_chat_action("what is expense 2", pipeline) == "No expense #2."
    assert handle_chat_action("spent 40 on coffee", pipeline) is None
    assert handle_chat_action("go to daily", pipeline) == "Mode: Daily"


def test_plain_sentences_reach_salary_and_trip_create():
    from expense_tracker.extractor.prompts import INTENT_SYSTEM
    from expense_tracker.ledger.sheets import FakeSheetsBackend
    from expense_tracker.salary import is_salary_text
    from expense_tracker.trips import InMemoryWorkbookFactory, TripService

    assert is_salary_text("what was my gross pay for 2026")
    assert is_salary_text("spent 40 on coffee") is False
    assert "Never classify" in INTENT_SYSTEM
    trips = TripService(FakeSheetsBackend(title="Daily"), InMemoryWorkbookFactory())
    reply = trips.handle(
        "make a California trip from 2026-10-24 to 2026-11-02 budget 3500"
    )
    assert reply is not None and "California is active" in reply
