"""Write apartment rent charges to the rent spreadsheet.

A portal account history becomes one row per month. Each charge type
has its own column. The daily expense ledger is not used.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

from .ledger.sheets.backend import SheetsBackend, col_index_to_letter

_RENT_SHEET_ID = "1Qh_Uq8_wuSf96ZGgJ7yXqI9IEEkgQHKupn-fvrw9b5k"
_RENT_GID = 313232724
_SHARE_WITH = "expense-bot@expense-tracker-bot-494505.iam.gserviceaccount.com"

_HEADERS = (
    "Month",
    "Year",
    "Base rent",
    "Concession",
    "Trash reimbursement",
    "Pest",
    "Deposit waiver",
    "Amenity",
    "Rent service fee",
    "Water",
    "Water period",
    "Sewer",
    "Sewer period",
    "Water/sewer admin",
    "Admin period",
    "Utility trash",
    "Insurance",
    "Insurance period",
    "Asset protection",
    "Late fee",
    "Package lockers",
    "Vacant service",
    "Vacant electric",
    "Other charges",
    "Other credits",
    "Deposit transfer",
    "Payments",
    "Total charges",
    "Net",
)

_AMOUNT_FIELDS = (
    "base_rent",
    "concession",
    "trash_reimb",
    "pest",
    "deposit_waiver",
    "amenity",
    "rent_service_fee",
    "water",
    "sewer",
    "admin",
    "utility_trash",
    "insurance",
    "asset_protection",
    "late_fee",
    "package_lockers",
    "vacant_service",
    "vacant_electric",
    "other_charges",
    "other_credits",
    "deposit_transfer",
    "payments",
)
_PERIOD_FIELDS = ("water_period", "sewer_period", "admin_period", "insurance_period")
_FIELD_HEADER = {
    "month": "Month",
    "year": "Year",
    "base_rent": "Base rent",
    "concession": "Concession",
    "trash_reimb": "Trash reimbursement",
    "pest": "Pest",
    "deposit_waiver": "Deposit waiver",
    "amenity": "Amenity",
    "rent_service_fee": "Rent service fee",
    "water": "Water",
    "water_period": "Water period",
    "sewer": "Sewer",
    "sewer_period": "Sewer period",
    "admin": "Water/sewer admin",
    "admin_period": "Admin period",
    "utility_trash": "Utility trash",
    "insurance": "Insurance",
    "insurance_period": "Insurance period",
    "asset_protection": "Asset protection",
    "late_fee": "Late fee",
    "package_lockers": "Package lockers",
    "vacant_service": "Vacant service",
    "vacant_electric": "Vacant electric",
    "other_charges": "Other charges",
    "other_credits": "Other credits",
    "deposit_transfer": "Deposit transfer",
    "payments": "Payments",
    "total_charges": "Total charges",
    "net": "Net",
}
_HEADER_FIELD = {header: key for key, header in _FIELD_HEADER.items()}
_ALIASES = {
    "base rent": "base_rent",
    "base fee": "base_rent",
    "monthly rent": "base_rent",
    "rent": "base_rent",
    "concession": "concession",
    "trash reimbursement": "trash_reimb",
    "trash reimb": "trash_reimb",
    "pest": "pest",
    "deposit waiver": "deposit_waiver",
    "deposit waiver min": "deposit_waiver",
    "amenity": "amenity",
    "amenity fee": "amenity",
    "rent service fee": "rent_service_fee",
    "water": "water",
    "water bill": "water",
    "water billing": "water",
    "water period": "water_period",
    "sewer": "sewer",
    "sewer billing": "sewer",
    "sewer period": "sewer_period",
    "water/sewer admin": "admin",
    "water and sewer admin": "admin",
    "admin fee": "admin",
    "admin period": "admin_period",
    "utility trash": "utility_trash",
    "insurance": "insurance",
    "insurance period": "insurance_period",
    "asset protection": "asset_protection",
    "late fee": "late_fee",
    "late charges": "late_fee",
    "package lockers": "package_lockers",
    "vacant service": "vacant_service",
    "vacant electric": "vacant_electric",
    "other charges": "other_charges",
    "other credits": "other_credits",
    "deposit transfer": "deposit_transfer",
    "payments": "payments",
    "month": "month",
    "year": "year",
    "total charges": "total_charges",
    "net": "net",
}

_DATE_LINE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_MONEY_END = re.compile(
    r"(?P<open>\(\s*)?\$\s*(?P<dollars>[0-9,]+)\s*\.(?P<cents>\d{2})\s*(?P<close>\))?\s*$"
)
_PERIOD = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4})\s*-\s*(\d{1,2}/\d{1,2}/\d{2,4})"
)
_MONTHS = {name.lower(): num for num, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): num for num, name in enumerate(calendar.month_abbr) if name})

_HELP = (
    "Send the rent statement with the caption rent entry, "
    "or paste it after rent entry. "
    "A single charge also works: rent entry water 45.03 for November 2026."
)


@dataclass
class RentMonth:
    year: int
    month: int
    amounts: dict[str, float] = field(default_factory=dict)
    periods: dict[str, str] = field(default_factory=dict)
    full: bool = False


def pdf_text(data: bytes) -> str:
    """Extract text from a PDF. The bytes are not written to disk."""
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def is_rent_text(text: str) -> bool:
    """True when this message should update the rent sheet, not daily expenses."""
    low = " ".join(text.lower().split())
    if low.startswith("rent entry") or low.startswith("rent:"):
        return True
    if re.search(r"\b(rental car|rent a car|car rental)\b", low):
        return False
    if re.search(r"\b(trash reimb|amenity fee|deposit waiver|water billing|rent service fee)\b", low):
        return True
    return bool(re.search(r"\brent\b", low) and re.search(r"\d", low))


def parse_rent_statement(text: str) -> list[RentMonth]:
    """Group a portal account history into one month per posting date."""
    current: tuple[int, int, int] | None = None
    buckets: dict[tuple[int, int], RentMonth] = {}
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        dated = _DATE_LINE.match(line)
        if dated:
            month, day, year = (int(part) for part in dated.groups())
            current = (year, month, day)
            continue
        found = _MONEY_END.search(line)
        if found is None or current is None:
            continue
        label = line[: found.start()].strip(" -")
        if not label:
            continue
        signed = _signed(found)
        year, month, _day = current
        row = buckets.setdefault((year, month), RentMonth(year=year, month=month, full=True))
        _apply_line(row, label, signed)
    return [buckets[key] for key in sorted(buckets)]


def parse_rent_update(text: str, *, today: tuple[int, int] | None = None) -> RentMonth | None:
    """Read one charge such as 'water 45.03 for November 2026'."""
    low = " ".join(text.lower().replace("rent entry", " ").replace("rent:", " ").split())
    numbers = re.findall(r"\$?\s*([0-9,]+(?:\.\d{1,2})?)", low)
    amounts = [item for item in numbers if not re.fullmatch(r"20\d{2}", item.replace(",", ""))]
    if not amounts:
        return None
    month = _month_in(low)
    year_match = re.search(r"\b(20\d{2})\b", low)
    if month is None or year_match is None:
        today_date = date.today() if today is None else date(today[0], today[1], 1)
        month = month or today_date.month
        year = int(year_match.group(1)) if year_match else today_date.year
    else:
        year = int(year_match.group(1))
    field_name = _short_field(low)
    if field_name is None:
        return None
    value = float(amounts[0].replace(",", ""))
    return RentMonth(year=year, month=month, amounts={field_name: value}, full=False)


def save_rent(book: SheetsBackend, months: list[RentMonth]) -> str:
    """Upsert month rows. A full statement replaces that month's charges."""
    if not months:
        return _HELP
    ws = _select_worksheet(book)
    headers = _ensure_headers(ws)
    columns = _columns(headers)
    stored = _read_stored(ws, columns)
    for month in months:
        key = (month.year, month.month)
        row = stored.get(key, {})
        row["month"] = calendar.month_name[month.month]
        row["year"] = month.year
        if month.full:
            for name in _AMOUNT_FIELDS:
                row[name] = round(month.amounts.get(name, 0.0), 2)
            for name in _PERIOD_FIELDS:
                row[name] = month.periods.get(name, "")
        else:
            for name, value in month.amounts.items():
                row[name] = round(value, 2)
            for name, value in month.periods.items():
                row[name] = value
        _fill_totals(row)
        stored[key] = row
    _write_stored(ws, headers, columns, stored)
    return _summary(months, stored)


def rent_sheet_id(configured: str | None) -> str:
    return configured or _RENT_SHEET_ID


def share_hint() -> str:
    return (
        "I couldn't open the rent spreadsheet. Share it with "
        f"{_SHARE_WITH} as Editor, then send rent entry again."
    )


def _apply_line(row: RentMonth, label: str, signed: float) -> None:
    low = label.lower()
    period = _period_text(label)
    amount = abs(signed)
    if "welcomehome" in low or ("ach payment" in low) or (
        "payment" in low and "fee" not in low and "moved to deposit" not in low
    ):
        _add(row, "payments", amount)
        return
    if "online payment fee" in low:
        _add(row, "other_credits" if signed < 0 else "other_charges", amount)
        return
    if "moved to deposit" in low:
        _add(row, "deposit_transfer", signed)
        return
    if "conc" in low:
        _add(row, "concession", amount)
        return
    if low.startswith("rent service"):
        _add(row, "rent_service_fee", amount)
        return
    if low.startswith("rent"):
        _add(row, "base_rent", signed)
        return
    if low.startswith("trash reimb"):
        _add(row, "trash_reimb", signed)
        return
    if low.startswith("pest"):
        _add(row, "pest", signed)
        return
    if "deposit waiver" in low:
        _add(row, "deposit_waiver", signed)
        return
    if low.startswith("amenity"):
        _add(row, "amenity", signed)
        return
    if low.startswith("water and sewer") or low.startswith("admin fee"):
        _add(row, "admin", signed)
        if period:
            _join_period(row, "admin_period", period)
        return
    if low.startswith("water"):
        _add(row, "water", signed)
        if period:
            _join_period(row, "water_period", period)
        return
    if low.startswith("sewer"):
        _add(row, "sewer", signed)
        if period:
            _join_period(row, "sewer_period", period)
        return
    if low.startswith("trash"):
        _add(row, "utility_trash", signed)
        return
    if "insurance" in low:
        _add(row, "insurance", amount)
        if period:
            _join_period(row, "insurance_period", period)
        return
    if "assetprotect" in low or "asset protect" in low:
        _add(row, "asset_protection", amount)
        return
    if "late charge" in low:
        _add(row, "late_fee", amount)
        return
    if "package locker" in low:
        _add(row, "package_lockers", amount)
        return
    if "vacant service" in low:
        _add(row, "vacant_service", amount)
        return
    if "vacant electric" in low:
        _add(row, "vacant_electric", amount)
        return
    _add(row, "other_credits" if signed < 0 else "other_charges", amount)


def _add(row: RentMonth, name: str, value: float) -> None:
    row.amounts[name] = round(row.amounts.get(name, 0.0) + value, 2)


def _join_period(row: RentMonth, name: str, period: str) -> None:
    current = row.periods.get(name, "")
    row.periods[name] = period if not current or period in current else f"{current}; {period}"


def _period_text(label: str) -> str:
    found = _PERIOD.search(label)
    if found is None:
        return ""
    return f"{found.group(1)}-{found.group(2)}"


def _signed(found: re.Match[str]) -> float:
    value = float(found.group("dollars").replace(",", "") + "." + found.group("cents"))
    if found.group("open") or found.group("close"):
        return -value
    return value


def _short_field(low: str) -> str | None:
    ordered = (
        ("rent service fee", "rent_service_fee"),
        ("base rent", "base_rent"),
        ("base fee", "base_rent"),
        ("trash reimbursement", "trash_reimb"),
        ("trash reimb", "trash_reimb"),
        ("deposit waiver", "deposit_waiver"),
        ("amenity fee", "amenity"),
        ("amenity", "amenity"),
        ("water bill", "water"),
        ("water", "water"),
        ("sewer", "sewer"),
        ("pest", "pest"),
        ("insurance", "insurance"),
        ("late fee", "late_fee"),
        ("concession", "concession"),
        ("rent", "base_rent"),
    )
    for phrase, name in ordered:
        if phrase in low:
            return name
    return None


def _month_in(low: str) -> int | None:
    for name, number in sorted(_MONTHS.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{name}\b", low):
            return number
    return None


def _fill_totals(row: dict[str, object]) -> None:
    charges = 0.0
    for name in _AMOUNT_FIELDS:
        if name in {"concession", "other_credits", "payments"}:
            continue
        charges += float(row.get(name) or 0)
    charges -= float(row.get("concession") or 0)
    charges -= float(row.get("other_credits") or 0)
    paid = float(row.get("payments") or 0)
    row["total_charges"] = round(charges, 2)
    row["net"] = round(charges - paid, 2)


def _select_worksheet(book: SheetsBackend):
    sheets = book.list_worksheets()
    target = next((ws for ws in sheets if getattr(ws, "gid", None) == _RENT_GID), None)
    if target is None and len(sheets) == 1:
        target = sheets[0]
    if target is not None and (_blank(target) or _usable_header(target)):
        return target
    if book.has_worksheet("Rent statement"):
        return book.get_worksheet("Rent statement")
    return book.create_worksheet("Rent statement", rows=80, cols=len(_HEADERS))


def _blank(ws) -> bool:
    values = ws.get_values("A1:C5")
    return not any(cell not in ("", None) for row in values for cell in row)


def _usable_header(ws) -> bool:
    header = _header_cells(ws)
    if not any(header):
        return True
    return any(_norm(cell) in _ALIASES for cell in header)


def _header_cells(ws) -> list[str]:
    values = ws.get_values(f"A1:{col_index_to_letter(len(_HEADERS) + 5)}1")
    if not values:
        return []
    return [str(cell).strip() for cell in values[0]]


def _ensure_headers(ws) -> list[str]:
    header = [cell for cell in _header_cells(ws) if cell]
    if not header:
        ws.update_values(f"A1:{col_index_to_letter(len(_HEADERS) - 1)}1", [list(_HEADERS)])
        return list(_HEADERS)
    norms = {_norm(cell) for cell in header}
    missing = [name for name in _HEADERS if _aliases_for(name).isdisjoint(norms)]
    if missing:
        start = len(header)
        if ws.col_count < start + len(missing):
            ws.resize(cols=start + len(missing))
        end = col_index_to_letter(start + len(missing) - 1)
        begin = col_index_to_letter(start)
        ws.update_values(f"{begin}1:{end}1", [missing])
        header.extend(missing)
    return header


def _aliases_for(header: str) -> set[str]:
    field_name = _HEADER_FIELD[header]
    return {alias for alias, key in _ALIASES.items() if key == field_name}


def _columns(headers: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for index, header in enumerate(headers):
        key = _ALIASES.get(_norm(header))
        if key is not None and key not in found:
            found[key] = index
    return found


def _read_stored(ws, columns: dict[str, int]) -> dict[tuple[int, int], dict[str, object]]:
    if "month" not in columns:
        return {}
    last = col_index_to_letter(max(columns.values()))
    values = ws.get_values(f"A2:{last}")
    stored: dict[tuple[int, int], dict[str, object]] = {}
    for raw in values:
        if not raw or all(cell in ("", None) for cell in raw):
            continue
        row: dict[str, object] = {}
        for key, index in columns.items():
            row[key] = raw[index] if index < len(raw) else ""
        month = _month_in(str(row.get("month", "")).lower())
        year_raw = row.get("year")
        year_in_month = re.search(r"(20\d{2})", str(row.get("month", "")))
        try:
            year = int(float(str(year_raw))) if str(year_raw).strip() else None
        except ValueError:
            year = None
        if year is None and year_in_month:
            year = int(year_in_month.group(1))
        if month is None or year is None:
            continue
        stored[(year, month)] = row
    return stored


def _write_stored(ws, headers: list[str], columns: dict[str, int], stored: dict) -> None:
    ordered = [stored[key] for key in sorted(stored)]
    width = max([len(headers), *(index + 1 for index in columns.values())])
    end = col_index_to_letter(width - 1)
    current = ws.get_values(f"A2:{end}")
    by_key: dict[tuple[int, int], list[object]] = {}
    for raw in current:
        padded = list(raw) + [""] * (width - len(raw))
        month = _month_in(str(padded[columns["month"]]).lower()) if "month" in columns else None
        year_text = str(padded[columns["year"]]) if "year" in columns else ""
        try:
            year = int(float(year_text)) if year_text.strip() else None
        except ValueError:
            year = None
        if month is not None and year is not None:
            by_key[(year, month)] = padded[:width]
    grid: list[list[object]] = []
    for key in sorted(stored):
        cells = list(by_key.get(key, [""] * width))
        if len(cells) < width:
            cells.extend([""] * (width - len(cells)))
        for name, index in columns.items():
            cells[index] = stored[key].get(name, "")
        grid.append(cells[:width])
    ws.update_values(f"A2:{end}{1 + len(grid)}", grid)


def _summary(months: list[RentMonth], stored: dict[tuple[int, int], dict[str, object]]) -> str:
    keys = sorted({(month.year, month.month) for month in months})
    first = f"{calendar.month_name[keys[0][1]]} {keys[0][0]}"
    last_key = keys[-1]
    last = f"{calendar.month_name[last_key[1]]} {last_key[0]}"
    latest = stored[last_key]
    span = first if first == last else f"{first} through {last}"
    return (
        f"Saved rent for {span} on the rent sheet. "
        f"{last}: charges ${float(latest.get('total_charges') or 0):,.2f}, "
        f"paid ${float(latest.get('payments') or 0):,.2f}."
    )


def _norm(value: str) -> str:
    return " ".join(value.lower().split())
