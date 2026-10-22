"""Read a pay stub and write one paycheck row to the Salary Review year tab.

The stub text is parsed in this process. SSN, address, and bank accounts
are never copied to the sheet or included in the reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO

from .ledger.sheets.backend import SheetsBackend, col_index_to_letter

_HEADERS = (
    "Pay date",
    "Annual salary",
    "Pay this check",
    "Change $",
    "Change %",
    "Change type",
    "Salary",
    "Holiday",
    "PTO",
    "Bonus",
    "Gross",
    "Federal",
    "State",
    "Local",
    "Social Security",
    "Medicare",
    "Total tax",
    "Medical",
    "Dental",
    "Vision",
    "HSA",
    "401K",
    "401K %",
    "Total deductions",
    "Employer medical",
    "Employer HSA",
    "Employer total",
    "Net pay",
    "Take-home %",
    "YTD gross",
    "YTD net",
    "YTD bonus",
)


@dataclass(frozen=True)
class PayStub:
    pay_date: date
    salary: float
    holiday: float
    pto: float
    bonus: float
    gross: float
    federal: float
    state: float
    local: float
    social_security: float
    medicare: float
    total_tax: float
    medical: float
    dental: float
    vision: float
    hsa: float
    retirement: float
    total_deductions: float
    employer_medical: float
    employer_hsa: float
    employer_total: float
    net_pay: float

    @property
    def annual_salary(self) -> float:
        """Semi-monthly salary annualized from this check's salary line."""
        return round(self.salary * 24, 2)

    @property
    def retirement_pct(self) -> float:
        if self.gross <= 0:
            return 0.0
        return round(self.retirement / self.gross, 4)

    @property
    def take_home_pct(self) -> float:
        if self.gross <= 0:
            return 0.0
        return round(self.net_pay / self.gross, 4)


def parse_paystub(text: str) -> PayStub | None:
    """Parse a Rippling advice-of-deposit. Returns None when the date or gross is missing."""
    pay_date = _pay_date(text)
    summary = text.split("Summary", 1)[-1] if "Summary" in text else text
    gross = _nth(summary, "Gross Pay", 0)
    if pay_date is None or gross is None:
        return None
    medical = _pair(text, "Medical Deductions")
    hsa = _pair(text, "HSA")
    vision = _pair(text, "Vision Deductions")
    dental = _pair(text, "Dental Deductions")
    ltd = _pair(text, "Long Term Disability Deductions")
    std = _pair(text, "Short Term Disability Deductions")
    life = _pair(text, "Life Deductions")
    retirement = _pair(text, "401K")
    employer_total = round(
        medical[1] + hsa[1] + vision[1] + dental[1] + ltd[1] + std[1] + life[1] + retirement[1],
        2,
    )
    return PayStub(
        pay_date=pay_date,
        salary=_nth(text, "Salary", 0) or 0.0,
        holiday=_earning(text, "Holiday Pay"),
        pto=_earning(text, "Paid Time Off"),
        bonus=_nth(text, "Bonus", 0) or 0.0,
        gross=gross,
        federal=_nth(text, "Federal Income Tax", 0) or 0.0,
        state=_nth(text, "State Income Tax", 0) or 0.0,
        local=(_nth(text, "CO PFML", 0) or 0.0) + (_nth(text, "Denver", 0) or 0.0),
        social_security=_nth(text, "Social Security Tax", 0) or 0.0,
        medicare=_nth(text, "Medicare Tax", 0) or 0.0,
        total_tax=_nth(summary, "Taxes", 0) or 0.0,
        medical=medical[0],
        dental=dental[0],
        vision=vision[0],
        hsa=hsa[0],
        retirement=retirement[0],
        total_deductions=_nth(summary, "Deductions", 0) or 0.0,
        employer_medical=medical[1],
        employer_hsa=hsa[1],
        employer_total=employer_total,
        net_pay=_nth(summary, "Net Pay", 0) or 0.0,
    )


def pdf_text(data: bytes) -> str:
    """Extract text from a PDF. The bytes are not written to disk."""
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def save_paystub(book: SheetsBackend, stub: PayStub) -> str:
    """Write this paycheck onto the year tab. The same pay date is updated in place."""
    title = str(stub.pay_date.year)
    if book.has_worksheet(title):
        ws = book.get_worksheet(title)
    else:
        ws = book.create_worksheet(title, rows=80, cols=len(_HEADERS))
    _ensure_header(ws)
    row_number = _row_for_date(ws, stub.pay_date)
    previous_pay = _previous_pay(ws, row_number)
    values = _row_values(stub, previous_pay, row_number)
    end = col_index_to_letter(len(_HEADERS) - 1)
    ws.update_values(f"A{row_number}:{end}{row_number}", [values])
    return _reply(stub, previous_pay, title)


def _ensure_header(ws) -> None:
    current = ws.get_values("A1")
    a1 = str(current[0][0]).strip() if current and current[0] else ""
    header = list(_HEADERS)
    if a1 == header[0]:
        return
    blank = [""] * len(header)
    if a1 == "Month":
        ws.update_values(f"A1:{col_index_to_letter(len(header) - 1)}40", [header, *([blank] * 39)])
        return
    ws.update_values(f"A1:{col_index_to_letter(len(header) - 1)}1", [header])


def _row_for_date(ws, pay_date: date) -> int:
    iso = pay_date.isoformat()
    found = ws.get_values("A2:A80")
    for offset, raw in enumerate(found):
        cell = str(raw[0]).strip() if raw else ""
        if cell in {iso, pay_date.strftime("%m/%d/%Y")}:
            return offset + 2
        if not cell:
            return offset + 2
    return len(found) + 2


def _previous_pay(ws, row_number: int) -> float | None:
    if row_number <= 2:
        return None
    found = ws.get_values(f"C{row_number - 1}")
    if not found or not found[0] or found[0][0] in ("", None):
        return None
    try:
        return float(str(found[0][0]).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _row_values(stub: PayStub, previous_pay: float | None, row_number: int) -> list:
    change = None if previous_pay is None else round(stub.salary - previous_pay, 2)
    change_pct = ""
    if previous_pay:
        change_pct = round((stub.salary - previous_pay) / previous_pay, 4)
    if previous_pay is None:
        change_type = "starting"
    elif change and change > 0.009:
        change_type = "hike"
    elif stub.bonus > 0:
        change_type = "bonus"
    else:
        change_type = ""
    gross_col = col_index_to_letter(_HEADERS.index("Gross"))
    net_col = col_index_to_letter(_HEADERS.index("Net pay"))
    bonus_col = col_index_to_letter(_HEADERS.index("Bonus"))
    return [
        stub.pay_date.isoformat(),
        stub.annual_salary,
        stub.salary,
        0 if change is None else change,
        change_pct,
        change_type,
        stub.salary,
        stub.holiday,
        stub.pto,
        stub.bonus,
        stub.gross,
        stub.federal,
        stub.state,
        stub.local,
        stub.social_security,
        stub.medicare,
        stub.total_tax,
        stub.medical,
        stub.dental,
        stub.vision,
        stub.hsa,
        stub.retirement,
        stub.retirement_pct,
        stub.total_deductions,
        stub.employer_medical,
        stub.employer_hsa,
        stub.employer_total,
        stub.net_pay,
        stub.take_home_pct,
        f"=SUM({gross_col}$2:{gross_col}{row_number})",
        f"=SUM({net_col}$2:{net_col}{row_number})",
        f"=SUM({bonus_col}$2:{bonus_col}{row_number})",
    ]


def _reply(stub: PayStub, previous_pay: float | None, tab: str) -> str:
    change = "starting pay" if previous_pay is None else (
        "hike" if stub.salary - previous_pay > 0.009 else (
            "bonus" if stub.bonus else "no pay change"
        )
    )
    return (
        f"Saved {stub.pay_date.isoformat()} on the {tab} tab. {change}.\n"
        f"Annual salary ${stub.annual_salary:,.2f}\n"
        f"Gross ${stub.gross:,.2f}  Net ${stub.net_pay:,.2f}  "
        f"Take-home {stub.take_home_pct:.1%}\n"
        f"Federal ${stub.federal:,.2f}  State ${stub.state:,.2f}  "
        f"Local ${stub.local:,.2f}\n"
        f"Social Security ${stub.social_security:,.2f}  "
        f"Medicare ${stub.medicare:,.2f}  Total tax ${stub.total_tax:,.2f}\n"
        f"Medical ${stub.medical:,.2f}  Dental ${stub.dental:,.2f}  "
        f"Vision ${stub.vision:,.2f}\n"
        f"HSA ${stub.hsa:,.2f}  401K ${stub.retirement:,.2f} "
        f"({stub.retirement_pct:.1%})  Deductions ${stub.total_deductions:,.2f}\n"
        f"Employer medical ${stub.employer_medical:,.2f}  "
        f"Employer total ${stub.employer_total:,.2f}"
    )


def _pay_date(text: str) -> date | None:
    match = re.search(r"PAY DATE:\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%m/%d/%Y").date()


def _amounts_after(label: str, text: str) -> list[float]:
    match = re.search(re.escape(label), text, re.I)
    if not match:
        return []
    window = text[match.end(): match.end() + 220]
    return [float(item.replace(",", "")) for item in re.findall(r"\$([0-9,]+\.\d{2})", window)]


def _earning(text: str, label: str) -> float:
    """Hourly earnings list the rate first and the current amount second."""
    found = _amounts_after(label, text)
    if len(found) >= 2:
        return found[1]
    return found[0] if found else 0.0


def _nth(text: str, label: str, index: int) -> float | None:
    found = _amounts_after(label, text)
    if index >= len(found):
        return None
    return found[index]


def _pair(text: str, label: str) -> tuple[float, float]:
    found = _amounts_after(label, text)
    employee = found[0] if found else 0.0
    employer = found[1] if len(found) > 1 else 0.0
    return employee, employer
