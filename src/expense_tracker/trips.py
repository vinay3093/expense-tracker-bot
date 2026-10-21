"""Trip mode: one master expense row, plus a separate spreadsheet per trip.

The master Transactions tab stays the source of truth. A trip spreadsheet
is a copy of the rows whose Trip cell matches that trip.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

from .ledger.base import TransactionRow
from .ledger.sheets.backend import FakeSheetsBackend, SheetsBackend
from .ledger.sheets.transactions import col_for, index_for

_log = logging.getLogger(__name__)

_TRIPS_TAB = "Trips"
_REGISTRY = (
    "Trip ID", "Name", "Start", "End", "Budget", "Currency",
    "Status", "Spreadsheet ID", "URL",
)
_TRIP_TXN = ("Date", "Category", "Note", "Vendor", "Amount", "Currency", "Amount USD")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PERSONAL = re.compile(r"\b(personal|not the trip|not a trip|no trip)\b", re.I)


@dataclass(frozen=True)
class Trip:
    trip_id: str
    name: str
    start: str
    end: str
    budget: float
    currency: str
    status: str
    spreadsheet_id: str
    url: str
    row_index: int


class WorkbookFactory:
    """Creates and reopens trip spreadsheets. Tests use the in-memory one."""

    def create(self, title: str) -> tuple[SheetsBackend, str]:
        raise NotImplementedError

    def open(self, spreadsheet_id: str) -> SheetsBackend:
        raise NotImplementedError


class InMemoryWorkbookFactory(WorkbookFactory):
    def __init__(self) -> None:
        self._books: dict[str, FakeSheetsBackend] = {}

    def create(self, title: str) -> tuple[SheetsBackend, str]:
        spreadsheet_id = f"trip-{len(self._books) + 1}"
        book = FakeSheetsBackend(spreadsheet_id=spreadsheet_id, title=title)
        self._books[spreadsheet_id] = book
        return book, _url(spreadsheet_id)

    def open(self, spreadsheet_id: str) -> SheetsBackend:
        return self._books[spreadsheet_id]


class GspreadWorkbookFactory(WorkbookFactory):
    def __init__(self, client: object, master: object) -> None:
        self._client = client
        self._master = master

    def create(self, title: str) -> tuple[SheetsBackend, str]:
        from .ledger.sheets.gspread_backend import GspreadSheetsBackend

        sh = self._client.create(title)  # type: ignore[attr-defined]
        try:
            sh.sheet1.update_title("Overview")
        except Exception:
            _log.warning("could not rename the new trip sheet's first tab")
        _share_with_master_editors(sh, self._master)
        book = GspreadSheetsBackend(client=self._client, spreadsheet=sh)
        return book, _url(sh.id)

    def open(self, spreadsheet_id: str) -> SheetsBackend:
        from .ledger.sheets.gspread_backend import GspreadSheetsBackend

        sh = self._client.open_by_key(spreadsheet_id)  # type: ignore[attr-defined]
        return GspreadSheetsBackend(client=self._client, spreadsheet=sh)


def factory_for(master: SheetsBackend) -> WorkbookFactory:
    client = getattr(master, "client", None)
    spreadsheet = getattr(master, "spreadsheet", None)
    if client is None or spreadsheet is None:
        return InMemoryWorkbookFactory()
    return GspreadWorkbookFactory(client, spreadsheet)


class TripService:
    def __init__(self, master: SheetsBackend, books: WorkbookFactory | None = None) -> None:
        self._master = master
        self._books = books or factory_for(master)

    def handle(self, text: str) -> str | None:
        """Return a reply when ``text`` is a trip command, else None."""
        body = text.strip()
        lower = body.lower()
        if lower.startswith("/trip"):
            body = body.split(None, 1)[1] if " " in body else "help"
        elif _DATE.search(body) and lower.startswith("create"):
            body = body
        elif lower in {"end trip", "end the trip", "close trip", "close the trip"}:
            body = "close"
        elif lower in {"trip summary", "trip status"}:
            body = "summary"
        else:
            return None

        cmd, _, rest = body.strip().partition(" ")
        cmd = cmd.lower()
        try:
            if cmd in {"help", ""}:
                return _HELP
            if cmd == "list":
                return self._list()
            if cmd == "active":
                return self._describe(self._active())
            if cmd == "close":
                return self._close(rest.strip() or None)
            if cmd == "summary":
                return self._summary(rest.strip() or None)
            if cmd == "sync":
                return self._sync(rest.strip() or None)
            if cmd == "switch":
                return self._switch(rest.strip())
            if cmd == "create":
                return self._create(rest.strip())
        except ValueError as exc:
            return str(exc)
        return _HELP

    def attach_name(self, user_text: str) -> str | None:
        """Trip name to stamp on a new expense, or None."""
        if _PERSONAL.search(user_text):
            return None
        active = self._active()
        return active.name if active else None

    def project(self, row: TransactionRow) -> str | None:
        """Copy one master row into its trip sheet. None means success."""
        if not row.trip:
            return None
        trip = self._by_name(row.trip)
        if trip is None:
            return f"Saved on the daily sheet. No trip sheet named {row.trip}."
        try:
            self._append_trip_row(trip, row)
        except Exception as exc:
            _log.warning("trip sheet update failed: %s", exc)
            return (
                "Saved on the daily sheet. The trip sheet did not update; "
                "send /trip sync."
            )
        return None

    # ─── commands ──────────────────────────────────────────────────────

    def _create(self, rest: str) -> str:
        name, start, end, budget, currency = _parse_create(rest)
        paused = self._active()
        if paused is not None:
            self._set_status(paused, "paused")
        trip_id = self._new_id(name, start)
        title = f"Trip - {name}"
        book, url = self._books.create(title)
        trip = Trip(
            trip_id=trip_id, name=name, start=start, end=end,
            budget=budget, currency=currency.upper(), status="active",
            spreadsheet_id=getattr(book, "spreadsheet_id", ""),
            url=url, row_index=0,
        )
        _build_trip_workbook(book, trip)
        self._append_registry(trip)
        lines = [
            f"Trip {name} is active ({start} to {end}).",
            f"Budget: {budget:,.2f} {currency.upper()}",
            url,
        ]
        if paused is not None:
            lines.insert(1, f"{paused.name} is paused.")
        return "\n".join(lines)

    def _list(self) -> str:
        trips = self._load()
        if not trips:
            return "No trips yet. " + _CREATE_HINT
        return "\n".join(
            f"{t.status}: {t.name} ({t.start} to {t.end})" for t in trips
        )

    def _close(self, name: str | None) -> str:
        trip = self._by_name(name) if name else self._active()
        if trip is None:
            return "No active trip." if not name else f"No trip named {name}."
        self._set_status(trip, "closed")
        spent, count = self._spent(trip.name)
        return (
            f"Closed {trip.name}. {count} expense(s), "
            f"${spent:,.2f} of {trip.budget:,.2f} {trip.currency}."
        )

    def _summary(self, name: str | None) -> str:
        trip = self._by_name(name) if name else self._active()
        if trip is None:
            return "No active trip." if not name else f"No trip named {name}."
        spent, count = self._spent(trip.name)
        left = trip.budget - spent
        return (
            f"{trip.name} ({trip.status}): {count} expense(s), "
            f"${spent:,.2f} spent, ${left:,.2f} left of "
            f"{trip.budget:,.2f} {trip.currency}.\n{trip.url}"
        )

    def _switch(self, name: str) -> str:
        if not name:
            return "Usage: /trip switch Cali"
        target = self._by_name(name)
        if target is None:
            return f"No trip named {name}."
        if target.status == "closed":
            return f"{target.name} is closed. Create a new trip to keep logging."
        current = self._active()
        if current is not None and current.trip_id != target.trip_id:
            self._set_status(current, "paused")
        self._set_status(target, "active")
        return f"{target.name} is the active trip.\n{target.url}"

    def _sync(self, name: str | None) -> str:
        trip = self._by_name(name) if name else self._active()
        if trip is None:
            return "No active trip." if not name else f"No trip named {name}."
        copied = 0
        for row in self._master_rows(trip.name):
            if not self._already_copied(trip, row):
                self._append_trip_row(trip, row)
                copied += 1
        return f"Synced {copied} expense(s) to {trip.name}."

    def _describe(self, trip: Trip | None) -> str:
        if trip is None:
            return "No active trip. Everyday expenses stay on the daily sheet."
        return f"Active trip: {trip.name} ({trip.start} to {trip.end}).\n{trip.url}"

    # ─── registry + sheets ─────────────────────────────────────────────

    def _ensure_registry(self):
        if not self._master.has_worksheet(_TRIPS_TAB):
            ws = self._master.create_worksheet(_TRIPS_TAB, rows=100, cols=len(_REGISTRY))
            ws.update_values("A1:I1", [_REGISTRY])
        return self._master.get_worksheet(_TRIPS_TAB)

    def _load(self) -> list[Trip]:
        if not self._master.has_worksheet(_TRIPS_TAB):
            return []
        values = self._ensure_registry().get_values("A2:I")
        trips: list[Trip] = []
        for offset, raw in enumerate(values):
            cells = list(raw) + [""] * (9 - len(raw))
            if not str(cells[0]).strip():
                continue
            trips.append(Trip(
                trip_id=str(cells[0]),
                name=str(cells[1]),
                start=str(cells[2]),
                end=str(cells[3]),
                budget=float(cells[4] or 0),
                currency=str(cells[5] or "USD"),
                status=str(cells[6] or "active"),
                spreadsheet_id=str(cells[7]),
                url=str(cells[8]),
                row_index=offset + 2,
            ))
        return trips

    def _active(self) -> Trip | None:
        return next((t for t in self._load() if t.status == "active"), None)

    def _by_name(self, name: str | None) -> Trip | None:
        if not name:
            return None
        wanted = name.strip().lower()
        return next((t for t in self._load() if t.name.lower() == wanted), None)

    def _append_registry(self, trip: Trip) -> None:
        self._ensure_registry().append_rows([[
            trip.trip_id, trip.name, trip.start, trip.end, trip.budget,
            trip.currency, trip.status, trip.spreadsheet_id, trip.url,
        ]])

    def _set_status(self, trip: Trip, status: str) -> None:
        self._ensure_registry().update_values(f"G{trip.row_index}", [[status]])

    def _new_id(self, name: str, start: str) -> str:
        slug = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-") or "TRIP"
        base = f"{slug}-{start[:7]}"
        taken = {t.trip_id for t in self._load()}
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    def _master_rows(self, name: str) -> list[TransactionRow]:
        if not self._master.has_worksheet("Transactions"):
            return []
        ws = self._master.get_worksheet("Transactions")
        last = col_for("trip")
        values = ws.get_values(f"A2:{last}")
        trip_i = index_for("trip")
        out: list[TransactionRow] = []
        for raw in values:
            if trip_i >= len(raw) or str(raw[trip_i]).strip().lower() != name.lower():
                continue
            out.append(_row_from_cells(raw))
        return out

    def _append_trip_row(self, trip: Trip, row: TransactionRow) -> None:
        book = self._books.open(trip.spreadsheet_id)
        ws = book.get_worksheet("Transactions")
        ws.append_rows([[
            row.date.isoformat(), row.category, row.note or "", row.vendor or "",
            row.amount, row.currency, row.amount_usd,
        ]])

    def _already_copied(self, trip: Trip, row: TransactionRow) -> bool:
        book = self._books.open(trip.spreadsheet_id)
        ws = book.get_worksheet("Transactions")
        for raw in ws.get_values("A2:G"):
            cells = list(raw) + [""] * 7
            if (
                str(cells[0]) == row.date.isoformat()
                and str(cells[1]) == row.category
                and float(cells[4] or 0) == float(row.amount)
            ):
                return True
        return False

    def _spent(self, name: str) -> tuple[float, int]:
        rows = self._master_rows(name)
        return sum(r.amount_usd for r in rows), len(rows)


def _build_trip_workbook(book: SheetsBackend, trip: Trip) -> None:
    tx = book.create_worksheet("Transactions", rows=500, cols=len(_TRIP_TXN))
    tx.update_values("A1:G1", [_TRIP_TXN])
    if book.has_worksheet("Overview"):
        ov = book.get_worksheet("Overview")
    else:
        ov = book.create_worksheet("Overview", rows=20, cols=2)
    ov.update_values("A1:B7", [
        ["Trip", trip.name],
        ["Start", trip.start],
        ["End", trip.end],
        ["Budget", trip.budget],
        ["Spent", "=SUM(Transactions!G2:G)"],
        ["Remaining", "=B4-B5"],
        ["Status", "active"],
    ])


def _parse_create(rest: str) -> tuple[str, str, str, float, str]:
    dates = _DATE.findall(rest)
    if len(dates) < 2:
        raise ValueError("Need a name and two dates: /trip create Cali 2026-11-01 2026-11-11 2500")
    start, end = dates[0], dates[1]
    if date.fromisoformat(end) < date.fromisoformat(start):
        raise ValueError("Trip end date is before the start date.")
    name = rest.split(start, 1)[0]
    name = re.sub(r"^(?:a )?trip(?: to| called)?\s+", "", name.strip(), flags=re.I)
    name = re.sub(r"\s+from$", "", name, flags=re.I).strip(" -")
    if not name:
        raise ValueError("Give the trip a name before the dates.")
    tail = rest.split(end, 1)[1]
    numbers = re.findall(r"\d+(?:\.\d+)?", tail)
    budget = float(numbers[0]) if numbers else 0.0
    currency_match = re.search(r"\b([A-Za-z]{3})\b", tail)
    currency = currency_match.group(1) if currency_match else "USD"
    return name, start, end, budget, currency


def _row_from_cells(raw: list) -> TransactionRow:
    def at(key: str) -> str:
        i = index_for(key)
        return str(raw[i]).strip() if i < len(raw) and raw[i] not in ("", None) else ""

    d = date.fromisoformat(at("date"))
    return TransactionRow(
        date=d,
        day=at("day"),
        month=at("month"),
        year=int(at("year") or d.year),
        category=at("category"),
        note=at("note") or None,
        vendor=at("vendor") or None,
        amount=float(at("amount") or 0),
        currency=at("currency") or "USD",
        amount_usd=float(at("amount_usd") or 0),
        fx_rate=float(at("fx_rate") or 1),
        source=at("source") or "chat",
        trip=at("trip") or None,
    )


def _url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def _share_with_master_editors(new_sheet: object, master: object) -> None:
    try:
        perms = master.list_permissions()  # type: ignore[attr-defined]
    except Exception:
        return
    for perm in perms:
        email = perm.get("emailAddress") if isinstance(perm, dict) else None
        if not email or "iam.gserviceaccount.com" in email:
            continue
        try:
            new_sheet.share(email, perm_type="user", role="writer", notify=False)  # type: ignore[attr-defined]
        except Exception:
            _log.warning("could not share trip sheet with %s", email)


_CREATE_HINT = (
    "Create one with /trip create Cali 2026-11-01 2026-11-11 2500"
)
_HELP = (
    "Trip commands:\n"
    "  /trip create Cali 2026-11-01 2026-11-11 2500\n"
    "  /trip active\n"
    "  /trip summary\n"
    "  /trip list\n"
    "  /trip switch Cali\n"
    "  /trip close\n"
    "  /trip sync\n"
    "While a trip is active, normal expenses land on the daily sheet and the trip sheet.\n"
    "Say 'personal' in the message to keep an expense off the trip."
)
