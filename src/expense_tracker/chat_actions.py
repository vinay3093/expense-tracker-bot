"""Plain-language ledger commands.

These run before the expense parser so "delete expense 12" is never
logged as a new expense. Wording can vary. Slash commands stay as they are.
"""

from __future__ import annotations

import calendar
import re
from datetime import date

from .ledger.sheets.identity import (
    StoredExpense,
    delete_expenses,
    load_expenses,
    update_expense,
)

_MONTHS = {name.lower(): num for num, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): num for num, name in enumerate(calendar.month_abbr) if name})
_LARGE = 200.0
_NUMBER = r"#?\s*(\d+)"


def handle_chat_action(text: str, pipeline: object) -> str | None:
    """Return a reply when the message is a ledger command, else None."""
    cleaned = " ".join(text.strip().split())
    if not cleaned or cleaned.startswith("/"):
        return None
    low = cleaned.lower().rstrip(".!?")

    if _is_daily(low):
        trips = getattr(pipeline, "trips", None)
        if trips is None:
            return "Trips need the Sheets ledger."
        return trips.use_daily()

    switched = _trip_label(low, ("switch to", "resume", "go back to", "back to", "open"))
    if switched is not None:
        return _resume(pipeline, switched)

    ended = _trip_label(low, ("end", "close", "finish", "stop"))
    if ended is not None and ("trip" in low or len(low.split()) <= 4):
        return _close(pipeline, ended)

    if re.search(r"\b(delete|remove|undo)\b", low) and re.search(r"\blast\b", low):
        return _delete_last(pipeline)

    numbers = _expense_numbers(low)
    if numbers and re.search(r"\b(delete|remove)\b", low):
        return _delete_numbers(pipeline, numbers, confirmed=low.startswith("yes"))

    if numbers and re.search(r"\b(what is|what's|whats|show|describe)\b", low):
        return _describe(pipeline, numbers[0])

    edited = _edit(low)
    if edited is not None:
        return _apply_edit(pipeline, *edited)

    shown = _show_target(low)
    if shown is not None:
        kind, value = shown
        if kind == "month":
            return _show_month(pipeline, value[0], value[1])
        return _show_trip(pipeline, value)
    return None


def _is_daily(low: str) -> bool:
    return bool(re.search(
        r"\b(switch to daily|back to daily|daily mode|use daily|only daily|log to daily)\b",
        low,
    ))


def _trip_label(low: str, prefixes: tuple[str, ...]) -> str | None:
    for prefix in prefixes:
        if low.startswith(prefix + " "):
            label = low[len(prefix):].strip()
            label = re.sub(r"^(the|a|an)\s+", "", label)
            label = re.sub(r"\s+trip$", "", label)
            if label in {"daily", "daily expense", "daily expenses"}:
                return None
            return label
    return None


def _expense_numbers(low: str) -> list[int]:
    if not re.search(r"\b(expense|expenses|number|#)\b", low) and not re.search(
        r"\b(delete|remove|change|update|set|what is|show)\b", low
    ):
        return []
    found = [int(n) for n in re.findall(_NUMBER, low)]
    # A bare year like 2026 in "show august 2025" is not an expense number.
    return [n for n in found if n < 1900]


def _edit(low: str) -> tuple[int, float | None, str | None] | None:
    if not re.search(r"\b(change|update|set|edit|make)\b", low):
        return None
    numbers = _expense_numbers(low)
    if len(numbers) != 1:
        return None
    category = re.search(r"\bcategory\s+(?:to\s+)?([a-z][a-z &-]*)", low)
    amount = re.search(r"(?:\$|to\s+|amount\s+(?:to\s+)?)(\d+(?:\.\d{1,2})?)", low)
    if category and "category" in low:
        return numbers[0], None, category.group(1).strip().title()
    if amount:
        return numbers[0], float(amount.group(1)), None
    return None


def _show_target(low: str):
    if not re.search(r"\b(show|list|what did|how much|expenses|cost)\b", low):
        return None
    if re.search(r"\b(for|on)\s+[a-z]", low) and "trip" not in low:
        return None
    month = _month_in(low)
    if month is not None:
        return "month", month
    trip = re.search(
        r"(?:trip|on)\s+([a-z][a-z0-9 .'-]*?)(?:\s+(?:so far|till date|to date|expenses?))?$",
        low,
    )
    if trip:
        return "trip", trip.group(1).strip()
    named = re.search(r"\b([a-z][a-z'-]+(?:\s+[a-z][a-z'-]+){0,2})\s+trip\b", low)
    if named and named.group(1) not in {"the", "a", "my", "this"}:
        return "trip", named.group(1).strip()
    return None


def _month_in(low: str) -> tuple[int, int | None] | None:
    for name, number in sorted(_MONTHS.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{name}\b", low):
            year = re.search(r"\b(20\d{2})\b", low)
            return number, int(year.group(1)) if year else None
    return None


def _resume(pipeline: object, label: str) -> str:
    trips = getattr(pipeline, "trips", None)
    if trips is None:
        return "Trips need the Sheets ledger."
    return trips.resume(label)


def _close(pipeline: object, label: str) -> str:
    trips = getattr(pipeline, "trips", None)
    if trips is None:
        return "Trips need the Sheets ledger."
    return trips.close_named(label)


def _ledger(pipeline: object):
    logger = getattr(pipeline, "_logger", None)
    ledger = getattr(logger, "_ledger", None)
    backend = getattr(ledger, "sheets_backend", None)
    fmt = getattr(ledger, "sheet_format", None)
    if backend is None or fmt is None:
        return None
    return backend, fmt


def _rows(pipeline: object) -> list[StoredExpense]:
    found = _ledger(pipeline)
    if found is None:
        raise ValueError("Expense numbers need the Sheets ledger.")
    return load_expenses(found[0], found[1])


def _delete_last(pipeline: object) -> str:
    try:
        rows = _rows(pipeline)
    except ValueError as exc:
        return str(exc)
    if not rows:
        return "Nothing to delete."
    latest = max(rows, key=lambda item: (item.timestamp, item.expense_no))
    return _delete_numbers(pipeline, [latest.expense_no], confirmed=False)


def _delete_numbers(pipeline: object, numbers: list[int], *, confirmed: bool) -> str:
    found = _ledger(pipeline)
    if found is None:
        return "Expense numbers need the Sheets ledger."
    try:
        rows = load_expenses(found[0], found[1])
    except Exception as exc:
        return f"I couldn't read the ledger. {exc}"
    chosen = [item for item in rows if item.expense_no in set(numbers)]
    missing = set(numbers) - {item.expense_no for item in chosen}
    if missing:
        return f"No expense {', '.join('#' + str(n) for n in sorted(missing))}."
    if not confirmed and any(item.amount >= _LARGE for item in chosen):
        listed = ", ".join(f"#{item.expense_no} ${item.amount:,.2f}" for item in chosen)
        joined = " and ".join(str(n) for n in numbers)
        return f"Delete {listed}? Reply: yes delete {joined}"
    try:
        gone = delete_expenses(found[0], found[1], numbers)
    except ValueError as exc:
        return str(exc)
    trips = getattr(pipeline, "trips", None)
    if trips is not None:
        for item in gone:
            if item.trip:
                try:
                    trip = trips._by_name(item.trip)
                    if trip is not None:
                        from .ledger.base import TransactionRow
                        trips._drop_trip_copy(trip, TransactionRow(
                            date=item.when, day="", month="", year=item.when.year,
                            category=item.category, note=item.note, vendor=item.vendor,
                            amount=item.amount, currency=item.currency,
                            amount_usd=item.amount, fx_rate=1.0, expense_no=item.expense_no,
                        ))
                except Exception:
                    pass
    lines = [_line(item) for item in gone]
    return "Deleted:\n" + "\n".join(lines)


def _describe(pipeline: object, number: int) -> str:
    try:
        rows = _rows(pipeline)
    except ValueError as exc:
        return str(exc)
    found = next((item for item in rows if item.expense_no == number), None)
    if found is None:
        return f"No expense #{number}."
    return _line(found)


def _apply_edit(
    pipeline: object, number: int, amount: float | None, category: str | None,
) -> str:
    found = _ledger(pipeline)
    if found is None:
        return "Expense numbers need the Sheets ledger."
    try:
        before = next(item for item in load_expenses(found[0], found[1]) if item.expense_no == number)
    except StopIteration:
        return f"No expense #{number}."
    except Exception as exc:
        return f"I couldn't read the ledger. {exc}"
    if category is not None:
        registry = getattr(getattr(pipeline, "_logger", None), "_registry", None)
        if registry is not None:
            category = registry.resolve_or_fallback(category)
    try:
        updated = update_expense(
            found[0], found[1], number, amount=amount, category=category,
        )
    except ValueError as exc:
        return str(exc)
    trips = getattr(pipeline, "trips", None)
    if trips is not None and before.trip:
        trips.update_copy(
            before.trip, number,
            amount=amount, category=category, when=before.when.isoformat(),
        )
    return f"Updated #{number}.\n{_line(updated)}"


def _show_month(pipeline: object, month: int, year: int | None) -> str:
    try:
        rows = _rows(pipeline)
    except ValueError as exc:
        return str(exc)
    if year is None:
        years = sorted({item.when.year for item in rows if item.when.month == month})
        year = years[-1] if years else date.today().year
    chosen = [item for item in rows if item.when.month == month and item.when.year == year]
    title = f"{calendar.month_name[month]} {year}"
    return _list(title, chosen)


def _show_trip(pipeline: object, label: str) -> str:
    try:
        rows = _rows(pipeline)
    except ValueError as exc:
        return str(exc)
    name = label
    year = None
    parts = label.split()
    if parts and parts[-1].isdigit() and len(parts[-1]) == 4:
        year = parts[-1]
        name = " ".join(parts[:-1])
    chosen = [
        item for item in rows
        if item.trip.lower() == name.lower()
        and (year is None or str(item.when.year) == year)
    ]
    trips = getattr(pipeline, "trips", None)
    if trips is not None and chosen:
        try:
            trips.stamp_copies(name, chosen)
        except Exception:
            pass
    title = f"{name} {year}".strip() if year else name
    return _list(f"{title} so far", chosen)


def _list(title: str, rows: list[StoredExpense]) -> str:
    if not rows:
        return f"No expenses for {title}."
    total = sum(item.amount for item in rows)
    lines = [_line(item) for item in rows]
    body = "\n".join(lines)
    if len(body) > 3500:
        lines = lines[:40]
        body = "\n".join(lines) + f"\n... {len(rows) - 40} more."
    return f"{title}: {len(rows)} expense(s), ${total:,.2f}\n{body}"


def _line(item: StoredExpense) -> str:
    extra = " — ".join(part for part in (item.vendor, item.note) if part)
    tail = f" {extra}" if extra else ""
    where = f" [{item.trip}]" if item.trip else ""
    return (
        f"#{item.expense_no} {item.when.isoformat()} {item.category} "
        f"${item.amount:,.2f}{tail}{where}"
    )
