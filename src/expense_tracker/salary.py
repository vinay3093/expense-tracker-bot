"""Read a pay stub and write one paycheck row to the Salary Review year tab.

The stub text is parsed in this process. SSN, address, and bank accounts
are never copied to the sheet or included in the reply.
"""

from __future__ import annotations

import calendar
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
    text = _normalize_stub(text)
    pay_date = _pay_date(text)
    summary_at = re.search(r"\bsummary\b", text, re.I)
    summary = text[summary_at.end():] if summary_at else text
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
    federal = _nth(text, "Federal Income Tax", 0) or 0.0
    state = _nth(text, "State Income Tax", 0) or 0.0
    local = (_nth(text, "CO PFML", 0) or 0.0) + (_nth(text, "Denver", 0) or 0.0)
    social_security = _nth_any(text, ("Social Security Tax", "Social Security"))
    medicare = _nth_any(text, ("Medicare Tax", "Medicare"))
    total_tax = _nth(summary, "Taxes", 0)
    if total_tax is None:
        total_tax = round(federal + state + local + social_security + medicare, 2)
    total_deductions = _nth(summary, "Deductions", 0)
    if total_deductions is None:
        total_deductions = round(medical[0] + dental[0] + vision[0] + hsa[0] + retirement[0], 2)
    return PayStub(
        pay_date=pay_date,
        salary=_nth(text, "Salary", 0) or 0.0,
        holiday=_labeled_earning(text, ("Holiday Pay", "Holiday Hours")),
        pto=_labeled_earning(text, ("Paid Time Off", "PTO")),
        bonus=_nth(text, "Bonus", 0) or 0.0,
        gross=gross,
        federal=federal,
        state=state,
        local=local,
        social_security=social_security,
        medicare=medicare,
        total_tax=total_tax,
        medical=medical[0],
        dental=dental[0],
        vision=vision[0],
        hsa=hsa[0],
        retirement=retirement[0],
        total_deductions=total_deductions,
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


def query_salary(book: SheetsBackend, question: str, *, today: date | None = None) -> str:
    """Answer a salary question from the year tabs. No model call is used."""
    question = question.strip()
    if not question or re.search(r"\b(help|commands)\b", question, re.I):
        return _QUERY_HELP

    checks = _load_checks(book)
    if not checks:
        return "No salary records yet. Send a pay stub with sal:."

    now = today or date.today()
    left, right = _split_compare(question)
    if right is not None and _asks(right, r"last month|previous month") and not _has_period(left):
        primary = _month_span(now.year, now.month)
    elif (
        _asks(left, r"hikes?|raises?|promotions?|pay changes?|latest|last paycheck|last check|most recent")
        and not _has_period(left)
    ):
        primary = _Span("all time", date(2000, 1, 1), date(2100, 1, 1))
    else:
        primary = _one_span(left, now)
    other_span = _comparison_span(left, right, primary, now)
    picked = _in_span(checks, primary)
    if not picked:
        return f"No salary records for {primary.label}."
    others = _in_span(checks, other_span) if other_span is not None else None

    if _asks(left, r"hikes?|raises?|promotions?|pay changes?"):
        return _format_changes(picked, primary)
    if _asks(left, r"latest|last paycheck|last check|most recent|current pay"):
        return _format_check(picked[-1])
    if _asks(left, r"highest|largest|biggest|max|lowest|smallest|min"):
        return _format_extreme(left, picked, primary)
    if _asks(left, r"breakdown|split|each tax|tax detail"):
        return _format_summary(picked, primary, others, other_span, detailed=True)
    metric = _query_metric(left)
    if metric == "ANNUAL":
        return _format_annual(picked, primary)
    if metric is None or _asks(left, r"summary|overview|recap"):
        return _format_summary(picked, primary, others, other_span, detailed=False)
    if _asks(left, r"average|avg|per paycheck|per check"):
        return _format_average(metric, picked, primary)
    if _asks(left, r"\beach\b|\bevery\b|\blist\b"):
        return _format_each(metric, picked, primary)
    return _format_total(metric, picked, primary, others, other_span)


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


def _normalize_stub(text: str) -> str:
    """Join words and amounts that PDF extraction splits with a space."""
    text = text.replace("\ufb00", "ff").replace("\ufb01", "fi").replace("\ufb02", "fl")
    text = text.replace("\u00a0", " ").replace("\u2009", " ")
    return re.sub(r"\bT (?=[a-z])", "T", text)


def _label_pattern(label: str) -> str:
    return r"\s+".join(re.escape(part) for part in label.split())


def _amounts_after(label: str, text: str) -> list[float]:
    match = re.search(_label_pattern(label), text, re.I)
    if not match:
        return []
    window = text[match.end(): match.end() + 220]
    found: list[float] = []
    for sign, dollars, cents in re.findall(r"(-?)\$\s*([0-9,]+)\s*\.(\d{2})", window):
        value = float(f"{dollars.replace(',', '')}.{cents}")
        found.append(-value if sign else value)
    return found


def _nth_any(text: str, labels: tuple[str, ...]) -> float:
    for label in labels:
        if re.search(_label_pattern(label), text, re.I):
            return _nth(text, label, 0) or 0.0
    return 0.0


def _labeled_earning(text: str, labels: tuple[str, ...]) -> float:
    for label in labels:
        if re.search(_label_pattern(label), text, re.I):
            return _earning(text, label)
    return 0.0


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


@dataclass(frozen=True)
class _Check:
    pay_date: date
    values: dict[str, float]
    change_type: str
    change_pct: float


@dataclass(frozen=True)
class _Span:
    label: str
    start: date
    end: date


_QUERY_METRICS = (
    (("annual salary", "current salary", "base salary"), "ANNUAL"),
    (("social security",), "Social Security"),
    (("federal tax", "federal"), "Federal"),
    (("state tax", "state"), "State"),
    (("local tax", "local"), "Local"),
    (("medicare",), "Medicare"),
    (("total tax", "tax paid", "taxes", "tax"), "Total tax"),
    (("take-home %", "take home %", "take-home rate", "take home rate"), "TAKEHOME"),
    (("net pay", "take home", "take-home", "net"), "Net pay"),
    (("gross pay", "gross", "salary"), "Gross"),
    (("effective tax", "tax rate"), "TAXRATE"),
    (("total deductions", "deductions"), "Total deductions"),
    (("401k %", "401k rate"), "RETIREMENT_PCT"),
    (("401k", "401 k"), "401K"),
    (("employer total", "employer"), "Employer total"),
    (("medical",), "Medical"),
    (("dental",), "Dental"),
    (("vision",), "Vision"),
    (("hsa",), "HSA"),
    (("bonus",), "Bonus"),
    (("holiday",), "Holiday"),
    (("pto",), "PTO"),
)

_QUERY_HELP = (
    "Ask about saved paychecks, for example:\n"
    "sal: gross for 2026\n"
    "sal: total tax paid for October\n"
    "sal: total net pay for October\n"
    "sal: summary for 2026\n"
    "sal: October vs last year\n"
    "sal: tax breakdown for 2026\n"
    "sal: hikes\n"
    "sal: latest\n"
    "sal: average net pay this year"
)

_TAX_LINES = ("Federal", "State", "Local", "Social Security", "Medicare")
_DEDUCTION_LINES = ("Medical", "Dental", "Vision", "HSA", "401K")


def _load_checks(book: SheetsBackend) -> list[_Check]:
    checks: list[_Check] = []
    end = col_index_to_letter(len(_HEADERS) - 1)
    for ws in book.list_worksheets():
        if not re.fullmatch(r"20\d{2}", ws.title):
            continue
        for row in ws.get_values(f"A2:{end}"):
            pay_date = _cell_date(row[0] if row else "")
            if pay_date is None:
                continue
            values = {
                header: _cell_amount(row[index] if index < len(row) else 0)
                for index, header in enumerate(_HEADERS)
                if header not in {"Pay date", "Change type"}
            }
            change = str(row[5]).strip() if len(row) > 5 else ""
            checks.append(_Check(pay_date, values, change, values.get("Change %", 0.0)))
    return sorted(checks, key=lambda item: item.pay_date)


def _split_compare(question: str) -> tuple[str, str | None]:
    parts = re.split(
        r"\b(?:vs\.?|versus|compared(?:\s+to|\s+with)?|against)\b",
        question,
        maxsplit=1,
        flags=re.I,
    )
    if len(parts) == 1:
        return question, None
    return parts[0], parts[1]


def _one_span(fragment: str, today: date) -> _Span:
    years = [int(item) for item in re.findall(r"\b(20\d{2})\b", fragment)]
    month = _query_month(fragment)
    quarter = re.search(r"\bq([1-4])\b", fragment, re.I)
    ytd = _asks(fragment, r"ytd|year to date|year-to-date|so far")
    if _asks(fragment, r"this month"):
        return _month_span(today.year, today.month)
    if _asks(fragment, r"last month"):
        year, month_number = _shift_month(today.year, today.month, -1)
        return _month_span(year, month_number)
    if quarter:
        return _quarter_span(years[0] if years else today.year, int(quarter.group(1)))
    if month:
        return _month_span(years[0] if years else today.year, month)
    if _asks(fragment, r"last year") and not years:
        year = today.year - 1
        return _ytd_span(year, today) if ytd else _year_span(year)
    if since := re.search(r"\bsince\s+(20\d{2})\b", fragment, re.I):
        year = int(since.group(1))
        return _Span(f"since {year}", date(year, 1, 1), today)
    if _asks(fragment, r"all time|all years|everything"):
        return _Span("all time", date(2000, 1, 1), date(2100, 1, 1))
    if _asks(fragment, r"this year") or ytd or not years:
        return _ytd_span(years[0] if years else today.year, today)
    return _year_span(years[0])


def _comparison_span(
    left: str, right: str | None, primary: _Span, today: date,
) -> _Span | None:
    if right is None:
        return None
    if _asks(right, r"last month") and _query_month(left) is None:
        year, month = _shift_month(today.year, today.month, -1)
        return _month_span(year, month)
    if _asks(right, r"last month|previous month"):
        year, month = _shift_month(primary.start.year, primary.start.month, -1)
        return _month_span(year, month)
    if _has_period(right) and not _asks(right, r"last year|previous year|year before"):
        return _one_span(right, today)
    return _shift_span_years(primary, -1)


def _format_total(
    metric: str,
    checks: list[_Check],
    span: _Span,
    others: list[_Check] | None,
    other_span: _Span | None,
) -> str:
    if metric in {"TAKEHOME", "TAXRATE", "RETIREMENT_PCT"}:
        return _format_rate(metric, checks, span, others, other_span)
    total = _sum(checks, metric)
    lines = [f"{metric} for {span.label}: ${total:,.2f}", _check_count(checks)]
    if others is not None and other_span is not None:
        lines.extend(_compare_lines(total, _sum(others, metric), other_span))
    return "\n".join(lines)


def _format_average(metric: str, checks: list[_Check], span: _Span) -> str:
    average = _sum(checks, metric) / len(checks)
    return (
        f"Average {metric.lower()} for {span.label}: ${average:,.2f}\n"
        + _check_count(checks)
    )


def _format_each(metric: str, checks: list[_Check], span: _Span) -> str:
    lines = [f"{metric} for {span.label}"]
    lines.extend(
        f"{item.pay_date.isoformat()}  ${item.values.get(metric, 0):,.2f}"
        for item in checks
    )
    lines.append(f"Total ${ _sum(checks, metric):,.2f}")
    return "\n".join(lines)


def _format_annual(checks: list[_Check], span: _Span) -> str:
    latest = checks[-1]
    return (
        f"Annual salary for {span.label}: ${latest.values.get('Annual salary', 0):,.2f}\n"
        f"From the {latest.pay_date.isoformat()} paycheck."
    )


def _format_rate(
    metric: str,
    checks: list[_Check],
    span: _Span,
    others: list[_Check] | None,
    other_span: _Span | None,
) -> str:
    labels = {
        "TAKEHOME": "Take-home",
        "TAXRATE": "Effective tax rate",
        "RETIREMENT_PCT": "401K rate",
    }
    rate = _rate(metric, checks)
    lines = [f"{labels[metric]} for {span.label}: {rate:.1%}", _check_count(checks)]
    if others is not None and other_span is not None:
        previous = _rate(metric, others)
        lines.append(f"{other_span.label}: {previous:.1%}")
        lines.append(f"Change: {rate - previous:+.1%}")
    return "\n".join(lines)


def _format_summary(
    checks: list[_Check],
    span: _Span,
    others: list[_Check] | None,
    other_span: _Span | None,
    *,
    detailed: bool,
) -> str:
    gross = _sum(checks, "Gross")
    net = _sum(checks, "Net pay")
    tax = _sum(checks, "Total tax")
    lines = [
        f"Salary summary for {span.label}",
        _check_count(checks),
        f"Annual salary ${checks[-1].values.get('Annual salary', 0):,.2f}",
        f"Gross ${gross:,.2f}",
        f"Net ${net:,.2f}  Take-home {_safe_rate(net, gross):.1%}",
        f"Tax ${tax:,.2f}  Effective {_safe_rate(tax, gross):.1%}",
    ]
    if detailed:
        lines.extend(f"  {name} ${_sum(checks, name):,.2f}" for name in _TAX_LINES)
    lines.append(f"Deductions ${_sum(checks, 'Total deductions'):,.2f}")
    if detailed:
        lines.extend(
            f"  {name} ${_sum(checks, name):,.2f}"
            for name in _DEDUCTION_LINES
            if _sum(checks, name)
        )
    lines.append(f"Bonus ${_sum(checks, 'Bonus'):,.2f}")
    lines.append(f"Employer ${_sum(checks, 'Employer total'):,.2f}")
    hikes = [item for item in checks if item.change_type == "hike"]
    if hikes:
        lines.append(f"Raises {len(hikes)}")
    if others is not None and other_span is not None:
        lines.append(f"Compared with {other_span.label}")
        lines.extend(_compare_lines(gross, _sum(others, "Gross"), other_span, "Gross"))
        lines.extend(_compare_lines(net, _sum(others, "Net pay"), other_span, "Net"))
        lines.extend(_compare_lines(tax, _sum(others, "Total tax"), other_span, "Tax"))
    return "\n".join(lines)


def _format_changes(checks: list[_Check], span: _Span) -> str:
    changed = [item for item in checks if item.change_type in {"hike", "bonus"}]
    if not changed:
        return f"No raises or bonuses in {span.label}."
    lines = [f"Pay changes in {span.label}"]
    for item in changed:
        annual = item.values.get("Annual salary", 0)
        change = item.values.get("Change $", 0)
        lines.append(
            f"{item.pay_date.isoformat()}  {item.change_type}  "
            f"{change:+,.2f} ({item.change_pct:+.1%})  annual ${annual:,.2f}"
        )
    return "\n".join(lines)


def _format_check(item: _Check) -> str:
    gross = item.values.get("Gross", 0)
    return "\n".join((
        f"Paycheck {item.pay_date.isoformat()}  {item.change_type or 'no pay change'}",
        f"Annual salary ${item.values.get('Annual salary', 0):,.2f}",
        f"Gross ${gross:,.2f}  Net ${item.values.get('Net pay', 0):,.2f}  "
        f"Take-home {_safe_rate(item.values.get('Net pay', 0), gross):.1%}",
        f"Federal ${item.values.get('Federal', 0):,.2f}  "
        f"State ${item.values.get('State', 0):,.2f}  "
        f"Local ${item.values.get('Local', 0):,.2f}",
        f"Social Security ${item.values.get('Social Security', 0):,.2f}  "
        f"Medicare ${item.values.get('Medicare', 0):,.2f}  "
        f"Tax ${item.values.get('Total tax', 0):,.2f}",
        f"401K ${item.values.get('401K', 0):,.2f}  "
        f"HSA ${item.values.get('HSA', 0):,.2f}  "
        f"Deductions ${item.values.get('Total deductions', 0):,.2f}",
        f"Bonus ${item.values.get('Bonus', 0):,.2f}  "
        f"Employer ${item.values.get('Employer total', 0):,.2f}",
    ))


def _format_extreme(question: str, checks: list[_Check], span: _Span) -> str:
    metric = _query_metric(question) or "Net pay"
    if metric in {"ANNUAL", "TAKEHOME", "TAXRATE", "RETIREMENT_PCT"}:
        metric = "Net pay"
    reverse = _asks(question, r"highest|largest|biggest|max")
    picked = sorted(checks, key=lambda item: item.values.get(metric, 0), reverse=reverse)[0]
    rank = "Highest" if reverse else "Lowest"
    return (
        f"{rank} {metric.lower()} in {span.label}: "
        f"${picked.values.get(metric, 0):,.2f} on {picked.pay_date.isoformat()}"
    )


def _compare_lines(
    current: float, previous: float, span: _Span, label: str = "",
) -> list[str]:
    name = f"{label} " if label else ""
    return [
        f"{name}{span.label}: ${previous:,.2f}",
        f"{name}change: ${current - previous:+,.2f} ({_safe_rate(current - previous, previous):+.1%})",
    ]


def _sum(checks: list[_Check], header: str) -> float:
    return round(sum(item.values.get(header, 0.0) for item in checks), 2)


def _rate(metric: str, checks: list[_Check]) -> float:
    gross = _sum(checks, "Gross")
    if metric == "TAKEHOME":
        return _safe_rate(_sum(checks, "Net pay"), gross)
    if metric == "TAXRATE":
        return _safe_rate(_sum(checks, "Total tax"), gross)
    return _safe_rate(_sum(checks, "401K"), gross)


def _safe_rate(part: float, whole: float) -> float:
    if not whole:
        return 0.0
    return part / whole


def _check_count(checks: list[_Check]) -> str:
    count = len(checks)
    return f"{count} paycheck" if count == 1 else f"{count} paychecks"


def _in_span(checks: list[_Check], span: _Span) -> list[_Check]:
    return [item for item in checks if span.start <= item.pay_date <= span.end]


def _asks(text: str, pattern: str) -> bool:
    return re.search(rf"\b(?:{pattern})\b", text, re.I) is not None


def _has_period(text: str) -> bool:
    return (
        _query_month(text) is not None
        or re.search(r"\b20\d{2}\b|\bq[1-4]\b", text, re.I) is not None
        or _asks(text, r"this month|last month|this year|last year|ytd|all time")
    )


def _query_metric(question: str) -> str | None:
    for aliases, header in _QUERY_METRICS:
        if any(re.search(rf"\b{re.escape(alias)}\b", question, re.I) for alias in aliases):
            return header
    return None


def _query_month(question: str) -> int | None:
    for month in range(1, 13):
        names = (calendar.month_name[month], calendar.month_abbr[month])
        if any(re.search(rf"\b{name}\b", question, re.I) for name in names if name):
            return month
    return None


def _year_span(year: int) -> _Span:
    return _Span(str(year), date(year, 1, 1), date(year, 12, 31))


def _ytd_span(year: int, today: date) -> _Span:
    end = date(year, today.month, min(today.day, calendar.monthrange(year, today.month)[1]))
    label = str(year) if end.month == 12 and end.day == 31 else f"{year} year to date"
    return _Span(label, date(year, 1, 1), end)


def _month_span(year: int, month: int) -> _Span:
    last_day = calendar.monthrange(year, month)[1]
    return _Span(f"{calendar.month_name[month]} {year}", date(year, month, 1), date(year, month, last_day))


def _quarter_span(year: int, quarter: int) -> _Span:
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    last_day = calendar.monthrange(year, end_month)[1]
    return _Span(
        f"Q{quarter} {year}",
        date(year, start_month, 1),
        date(year, end_month, last_day),
    )


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + offset
    return index // 12, index % 12 + 1


def _shift_span_years(span: _Span, offset: int) -> _Span:
    start = _shift_date_years(span.start, offset)
    end = _shift_date_years(span.end, offset)
    month_end = date(end.year, end.month, calendar.monthrange(end.year, end.month)[1])
    if start.day == 1 and start.month == end.month and end == month_end:
        label = f"{calendar.month_name[start.month]} {start.year}"
    elif start == date(start.year, 1, 1) and end == date(end.year, 12, 31):
        label = str(start.year)
    else:
        label = f"{start.year} through {calendar.month_abbr[end.month]} {end.day}"
    return _Span(label, start, end)


def _shift_date_years(value: date, offset: int) -> date:
    year = value.year + offset
    return date(year, value.month, min(value.day, calendar.monthrange(year, value.month)[1]))


def _cell_date(value: object) -> date | None:
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _cell_amount(value: object) -> float:
    text = str(value).replace(",", "").replace("$", "").replace("%", "").strip()
    if not text or text.startswith("="):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0
