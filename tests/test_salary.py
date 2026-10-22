"""Pay stub parsing and the Salary Review year tab."""

from datetime import date

from expense_tracker.ledger.sheets.backend import FakeSheetsBackend
from expense_tracker.salary import parse_paystub, save_paystub

_STUB = """
PAY DATE:
10/15/2026
EARNINGS
Holiday Pay $61.54 0.00 $0.00 32.00 $1,907.16
Salary - 86.6667 $5,333.33 1591.3093 $92,972.68
Paid Time Off $61.54 0.00 $0.00 24.00 $1,379.94
Bonus - 0.00 $0.00 0.00 $6,246.00
EMPLOYEE TAXES
Social Security Tax - Employee $308.35 $5,931.32
Federal Income Tax $391.14 $7,104.98
Medicare Tax - Employee $72.11 $1,387.16
DEDUCTIONS
Medical Deductions $0.00 $694.75 $0.00 $13,200.25
HSA $347.72 $50.00 $6,606.77 $950.00
Vision Deductions $1.13 $4.74
Dental Deductions $11.11 $32.46
Long Term Disability Deductions $0.00 $11.37
Short Term Disability Deductions $0.00 $4.93
Life Deductions $0.00 $1.80
401K $200.00 $0.00 $8,354.62 $0.00
Summary Current YTD
Gross Pay $5,333.33 $102,505.78
Deductions $559.96 $15,193.95
Taxes $771.60 $14,423.46
Net Pay $4,001.76 $73,056.04
"""


def test_parse_splits_taxes_and_ignores_ytd():
    stub = parse_paystub(_STUB)
    assert stub is not None
    assert stub.pay_date == date(2026, 10, 15)
    assert stub.salary == 5333.33
    assert stub.holiday == 0
    assert stub.gross == 5333.33
    assert stub.federal == 391.14
    assert stub.social_security == 308.35
    assert stub.medicare == 72.11
    assert stub.state == 0
    assert stub.hsa == 347.72
    assert stub.retirement == 200
    assert stub.employer_medical == 694.75
    assert stub.net_pay == 4001.76
    assert stub.annual_salary == 127999.92


def test_save_writes_year_tab_and_marks_a_later_hike():
    book = FakeSheetsBackend(title="Salary Review")
    first = parse_paystub(_STUB)
    assert first is not None
    reply = save_paystub(book, first)
    assert "2026" in reply
    assert "starting pay" in reply

    raised = parse_paystub(_STUB.replace("$5,333.33", "$5,500.00", 1).replace(
        "PAY DATE:\n10/15/2026", "PAY DATE:\n10/31/2026", 1,
    ))
    assert raised is not None
    second = save_paystub(book, raised)
    assert "hike" in second
    rows = book.get_worksheet("2026").get_values("A2:F3")
    assert rows[0][0] == "2026-10-15"
    assert rows[1][5] == "hike"
