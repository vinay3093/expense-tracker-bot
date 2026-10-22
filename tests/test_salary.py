"""Pay stub parsing and the Salary Review year tab."""

from datetime import date

from expense_tracker.ledger.sheets.backend import FakeSheetsBackend
from expense_tracker.salary import parse_paystub, query_salary, save_paystub

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


_PDF_TEXT = """
EARNINGS
Holiday Pay $61.54 0.00 $0.00 32.00 $1,907.16
Salary - 86.6667 $5,333.33 1591.3093 $92,972.68
Paid Time Oﬀ $61.54 0.00 $0.00 24.00 $1,379.94
Bonus - 0.00 $0.00 0.00 $6,246.00
EMPLOYEE TAXES CURRENT PERIOD YTD AMOUNT
Social Security T ax - Employee $308.35 $5,931.32
Federal Income T ax $391.14 $7,104.98
Medicare T ax - Employee $72.11 $1,387.16
DEDUCTIONS
Medical
Deductions
$0.00 $694.75 $0.00 $13,200.25
HSA $347.72 $50.00 $6,606.77 $950.00
Vision Deductions $1.13 $4.74 $21.47 $90.15
Long T erm
Disability
Deductions
$0.00 $11.37 $0.00 $216.03
Short T erm
Disability
Deductions
$0.00 $4.93 $0.00 $93.67
Dental
Deductions
$11.11 $32.46 $211.09 $616.74
Life Deductions $0.00 $1.80 $0.00 $34.20
401K $200.00 $0.00 $8,354.62 $0.00
SUMMARY CURRENT YTD
Gross Pay $5,333.33 $102,505.78
Deductions $559.96 $15,193.95
T axes $771.60 $14,423.46
Net Pay $4,001.76 $73,056.04
PAY DATE:
10/15/2026
"""

_SPACED_TEXT = """
PAY DATE:
01/30/2026
Salary - 86.67 $4,910.54 $9,374.67
Bonus - - $6,246.00 $6,246.00
TAXES WITHHELD CURRENT YTD
Federal Income T ax $657 .64 $963.00
Medicare $156.55 $222.53
Social Security $669.39 $951.53
SUMMARY CURRENT YTD
Gross Pay $11,156.54 $16,067 .08
Deductions $4,822.58 $5,674.55
T axes $1,483.58 $2,137 .06
Net Pay $4,982.02 $8,387.11
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


def test_parse_repairs_pdf_text_that_splits_tax_labels():
    stub = parse_paystub(_PDF_TEXT)
    assert stub is not None
    assert stub.pay_date == date(2026, 10, 15)
    assert stub.federal == 391.14
    assert stub.social_security == 308.35
    assert stub.medicare == 72.11
    assert stub.total_tax == 771.60
    assert stub.dental == 11.11
    assert stub.total_deductions == 559.96
    assert stub.employer_medical == 694.75
    assert stub.employer_total == 800.05
    assert stub.pto == 0
    assert stub.net_pay == 4001.76


def test_parse_reads_amounts_when_pdf_splits_the_cents():
    stub = parse_paystub(_SPACED_TEXT)
    assert stub is not None
    assert stub.federal == 657.64
    assert stub.medicare == 156.55
    assert stub.social_security == 669.39
    assert stub.total_tax == 1483.58
    assert stub.gross == 11156.54
    assert stub.bonus == 6246.00
    assert stub.net_pay == 4982.02


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
    rows = book.get_worksheet("2026").get_values("A1:G3")
    assert rows[0][1] == "Month"
    assert rows[1][0] == "2026-10-15"
    assert rows[1][1] == "October"
    assert rows[2][0] == "2026-10-31"
    assert rows[2][1] == "October"
    assert rows[2][6] == "hike"


def test_paychecks_sort_by_month_and_half():
    book = FakeSheetsBackend(title="Salary Review")
    second_half = parse_paystub(_STUB.replace("PAY DATE:\n10/15/2026", "PAY DATE:\n10/31/2026", 1))
    first_half = parse_paystub(_STUB)
    april = parse_paystub(_STUB.replace("10/15/2026", "04/15/2026"))
    assert second_half and first_half and april
    save_paystub(book, second_half)
    save_paystub(book, first_half)
    save_paystub(book, april)

    rows = book.get_worksheet("2026").get_values("A2:B4")
    assert [row[0] for row in rows] == ["2026-04-15", "2026-10-15", "2026-10-31"]
    assert [row[1] for row in rows] == ["April", "October", "October"]


def test_existing_row_keeps_its_amounts_when_month_is_added():
    book = FakeSheetsBackend(title="Salary Review")
    ws = book.create_worksheet("2026", rows=80, cols=40)
    ws.update_values(
        "A1:F1",
        [["Pay date", "Annual salary", "Pay this check", "Gross", "Federal", "Net pay"]],
    )
    ws.update_values("A2:F2", [["2026-10-15", 127999.92, 5333.33, 5333.33, 391.14, 4001.76]])
    april = parse_paystub(_STUB.replace("10/15/2026", "04/30/2026"))
    assert april is not None
    save_paystub(book, april)

    rows = ws.get_values("A1:AG3")
    federal = rows[0].index("Federal")
    assert rows[1][0] == "2026-04-30"
    assert rows[1][1] == "April"
    assert rows[2][0] == "2026-10-15"
    assert rows[2][1] == "October"
    assert rows[2][federal] == 391.14


def test_salary_questions_total_compare_and_explain():
    book = FakeSheetsBackend(title="Salary Review")
    current = parse_paystub(_STUB)
    raised = parse_paystub(_STUB.replace("$5,333.33", "$5,500.00", 1).replace(
        "PAY DATE:\n10/15/2026", "PAY DATE:\n10/31/2026", 1,
    ))
    previous = parse_paystub(
        _STUB.replace("10/15/2026", "10/15/2025")
        .replace("Gross Pay $5,333.33", "Gross Pay $5,000.00", 1)
        .replace("Net Pay $4,001.76", "Net Pay $3,800.00", 1)
        .replace("Taxes $771.60", "Taxes $700.00", 1)
    )
    assert current and raised and previous
    save_paystub(book, current)
    save_paystub(book, raised)
    save_paystub(book, previous)
    today = date(2026, 10, 21)

    gross = query_salary(book, "gross for 2026", today=today)
    assert "Gross for 2026: $10,666.66" in gross
    assert "2 paychecks" in gross

    tax = query_salary(book, "total tax paid for October", today=today)
    assert "Total tax for October 2026: $1,543.20" in tax

    net = query_salary(book, "total net pay for October", today=today)
    assert "Net pay for October 2026: $8,003.52" in net
    assert "Net pay for 2026: $8,003.52" in query_salary(
        book, "total net pay for 2026", today=today,
    )

    compared = query_salary(book, "October vs last year", today=today)
    assert "Salary summary for October 2026" in compared
    assert "October 2025: $5,000.00" in compared
    assert "Net October 2025: $3,800.00" in compared

    breakdown = query_salary(book, "tax breakdown for 2026", today=today)
    assert "Federal $782.28" in breakdown
    assert "Social Security $616.70" in breakdown

    hikes = query_salary(book, "hikes", today=today)
    assert "2026-10-31  hike" in hikes
    assert "+166.67" in hikes

    latest = query_salary(book, "latest", today=today)
    assert latest.startswith("Paycheck 2026-10-31")
    assert "Annual salary $132,000.00" in latest

    annual = query_salary(book, "annual salary for 2026", today=today)
    assert "Annual salary for 2026: $132,000.00" in annual
    assert "Average net pay for 2026: $4,001.76" in query_salary(
        book, "average net pay for 2026", today=today,
    )
    assert "Ask about saved paychecks" in query_salary(book, "help", today=today)
