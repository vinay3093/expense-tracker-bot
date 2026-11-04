"""Rent statements land on the rent sheet, one column per charge."""

from __future__ import annotations

from expense_tracker.ledger.sheets import FakeSheetsBackend
from expense_tracker.rent import (
    is_rent_text,
    parse_rent_statement,
    parse_rent_update,
    save_rent,
)

_STATEMENT = """
Account History
11/3/2026
WelcomeHome ACH payment -- Resident ( $2,130.86 )
$0.00
11/1/2026
Rent $1,840.00
11/1/2026
Trash Reimb $40.00
11/1/2026
PEST $6.00
11/1/2026
Deposit Waiver Min $22.00
11/1/2026
Amenity Fee $100.00
11/1/2026
Water Billing - 09/01/26-10/01/26 $45.03
11/1/2026
Water and Sewer Admin Fee - 09/01/26-10/01/26 $9.90
11/1/2026
Sewer Billing - 09/01/26-10/01/26 $64.94
11/1/2026
Rent Service Fee - 11/01/26-11/30/26 $2.99
10/27/2026
Insurance Violation Charge - 08/01/2026 - 08/31/2026 Unit - Apt 1 $40.00
10/1/2026
Rent $1,840.00
6/1/2026
1XNEW CONC ( $920.00 )
3/31/2026
Rent $59.35
3/31/2026
Package Lockers $30.00
"""


def _book():
    book = FakeSheetsBackend(title="Rent")
    book.create_worksheet("Rent", rows=40, cols=40)
    return book


def test_statement_splits_charges_and_keeps_the_service_period():
    months = {item.month: item for item in parse_rent_statement(_STATEMENT)}
    november = months[11]
    assert november.amounts["base_rent"] == 1840
    assert november.amounts["water"] == 45.03
    assert november.periods["water_period"] == "09/01/26-10/01/26"
    assert november.amounts["payments"] == 2130.86
    assert months[10].amounts["insurance"] == 40
    assert "08/01/2026-08/31/2026" in months[10].periods["insurance_period"]
    assert months[6].amounts["concession"] == 920
    assert months[3].amounts["base_rent"] == 59.35
    assert months[3].amounts["package_lockers"] == 30


def test_save_sorts_months_and_a_later_edit_keeps_the_other_columns():
    book = _book()
    save_rent(book, parse_rent_statement(_STATEMENT))
    ws = book.get_worksheet("Rent")
    values = [row for row in ws.get_values("A1:C20") if row and row[0]]
    assert values[0][:2] == ["Month", "Year"]
    assert values[1][0] == "March"
    assert values[-1][0] == "November"
    edited = parse_rent_update("rent entry water 10 for November 2026")
    assert edited is not None
    save_rent(book, [edited])
    rows = ws.get_values("A2:K20")
    november = next(row for row in rows if row and row[0] == "November")
    assert float(november[2]) == 1840
    assert float(november[9]) == 10


def test_a_used_tab_is_left_alone():
    book = _book()
    book.get_worksheet("Rent").update_values("A1:B2", [["Notes", "Amount"], ["manual", "5"]])
    save_rent(book, parse_rent_statement(_STATEMENT))
    original = book.get_worksheet("Rent").get_values("A1:B2")
    assert original[0] == ["Notes", "Amount"]
    assert original[1][0] == "manual"
    assert book.has_worksheet("Rent statement")


def test_rent_phrases_do_not_include_a_rental_car():
    assert is_rent_text("rent entry")
    assert is_rent_text("paid rent 1840")
    assert is_rent_text("rent a car $40") is False
