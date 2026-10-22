"""Trip registry, separate trip sheet, and expense projection."""

from __future__ import annotations

from datetime import date

from expense_tracker.ledger.base import TransactionRow
from expense_tracker.ledger.sheets.backend import FakeSheetsBackend
from expense_tracker.trips import InMemoryWorkbookFactory, TripService


def _service() -> tuple[TripService, FakeSheetsBackend, InMemoryWorkbookFactory]:
    master = FakeSheetsBackend(title="Daily")
    books = InMemoryWorkbookFactory()
    return TripService(master, books), master, books


def test_create_lists_and_closes_a_trip():
    trips, _, books = _service()
    reply = trips.handle("/trip create Cali 2026-11-01 2026-11-11 2500")
    assert reply is not None
    assert "Cali is active" in reply
    assert "trip-1" in reply
    book = books.open("trip-1")
    assert book.has_worksheet("Overview")
    assert book.has_worksheet("Transactions")
    assert "active: Cali" in trips.handle("/trip list")
    closed = trips.handle("/trip close")
    assert closed is not None and closed.startswith("Closed Cali")
    assert trips.attach_name("coffee 5") is None


def test_active_trip_projects_and_personal_does_not():
    trips, master, books = _service()
    trips.handle("create a trip to Cali from 2026-11-01 to 2026-11-11 budget 2500")
    assert trips.attach_name("uber 48") == "Cali"
    assert trips.attach_name("internet bill personal") is None

    row = TransactionRow(
        date=date(2026, 11, 2), day="Mon", month="November", year=2026,
        category="Transportation", note=None, vendor="Uber",
        amount=48, currency="USD", amount_usd=48, fx_rate=1,
        trip="Cali",
    )
    assert trips.project(row) is None
    copied = books.open("trip-1").get_worksheet("Transactions").get_values("A2:G2")
    assert copied[0][0] == "2026-11-02"
    assert copied[0][1] == "Transportation"
    assert master.title == "Daily"


def test_daily_button_and_one_message_override():
    from expense_tracker.ledger.sheets.transactions import col_for
    from expense_tracker.trips import split_override

    trips, master, _ = _service()
    trips.handle("/trip create Cali 2026-11-01 2026-11-11 2500")
    assert trips.attach_name("coffee 5") == "Cali"
    assert trips.use_daily() == "Mode: Daily"
    assert trips.attach_name("coffee 5") is None

    text, force = split_override("t: taxi 20")
    assert text == "taxi 20" and force == "trip"
    trips.set_override(force)
    assert trips.attach_name(text) == "Cali"
    trips.set_override(None)

    ws = master.create_worksheet("Transactions", rows=20, cols=16)
    ws.update_values(f"A2:{col_for('trip')}2", [[
        "2026-11-02", "Mon", "November", 2026, "Food", "", "",
        12, "USD", 12, 1, "chat", "", "", "Cali",
    ]])
    assert trips.move_last(None) == "Moved the last expense to Daily."
    assert ws.get_values(f"{col_for('trip')}2")[0][0] == ""


def test_second_trip_pauses_the_first():
    trips, _, _ = _service()
    trips.handle("/trip create Cali 2026-11-01 2026-11-11 2500")
    reply = trips.handle("/trip create Vegas 2026-12-01 2026-12-04 800")
    assert reply is not None and "Cali is paused" in reply
    assert trips.attach_name("dinner 40") == "Vegas"
    switched = trips.handle("/trip switch Cali")
    assert switched is not None and switched.startswith("Cali is the active trip")
    assert trips.attach_name("coffee 6") == "Cali"


def test_trip_tab_is_added_to_the_vacation_workbook():
    master = FakeSheetsBackend(title="Daily")
    vacation = FakeSheetsBackend(title="Travelling/Vacations", spreadsheet_id="vacation")

    class _VacationBooks:
        def create(self, title: str):
            vacation.create_worksheet(title, rows=50, cols=8)
            return vacation, "https://docs.google.com/spreadsheets/d/vacation/edit"

        def open(self, spreadsheet_id: str):
            assert spreadsheet_id == "vacation"
            return vacation

    trips = TripService(master, _VacationBooks())
    reply = trips.handle("/trip create California 2026-10-24 2026-11-02 3500")
    assert reply is not None
    assert "Tab: California, October 2026" in reply
    assert vacation.has_worksheet("California, October 2026")
    assert not master.has_worksheet("California, October 2026")

    row = TransactionRow(
        date=date(2026, 10, 25), day="Sun", month="October", year=2026,
        category="Food", note=None, vendor="Dinner",
        amount=40, currency="USD", amount_usd=40, fx_rate=1,
        trip="California",
    )
    assert trips.project(row) is None
    copied = vacation.get_worksheet("California, October 2026").get_values("A2:B2")
    assert copied[0] == ["2026-10-25", "Food"]
    again = trips.handle("/trip create California 2026-10-24 2026-11-02 3500")
    assert again is not None
    assert "already created" in again
    assert "California is the active trip" in again
    assert vacation.has_worksheet("California, October 2026")
