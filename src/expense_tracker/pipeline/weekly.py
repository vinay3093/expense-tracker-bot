"""Sunday expense digest: this week, then the month so far.

The first Sunday of a month sends one block, because that week and the
month so far are the same days. Later Sundays send the week on its own
and then the 1st of the month through that Sunday.

A week is Monday through Sunday, clipped to the current month so the
previous month never leaks into the new one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

_SEND_HOUR = 18


class _Spend(Protocol):
    date: date
    category: str
    amount_usd: float


def digest_windows(day: date) -> tuple[date, date, date]:
    """Return ``(week_start, day, month_start)``.

    The week runs Monday through ``day`` and stops at the 1st, so a
    report on the 3rd does not include the previous month.
    """
    monday = day - timedelta(days=day.weekday())
    month_start = day.replace(day=1)
    return max(monday, month_start), day, month_start


def digest_is_due(now: datetime, sent_on: date | None) -> date | None:
    """The Sunday to report, once it is 6pm in ``now``'s timezone.

    ``now`` must be timezone-aware. Returns ``None`` before 6pm, on any
    other day, or when that Sunday was already sent.
    """
    if now.tzinfo is None:
        raise ValueError("digest clock must carry a timezone")
    if now.weekday() != 6 or now.hour < _SEND_HOUR:
        return None
    if sent_on == now.date():
        return None
    return now.date()


def build_digest(sunday: date, rows: Sequence[_Spend]) -> str:
    """Render the report through ``sunday`` from rows dated in that month."""
    week_start, week_end, month_start = digest_windows(sunday)
    month_rows = [row for row in rows if month_start <= row.date <= week_end]
    week_rows = [row for row in month_rows if row.date >= week_start]
    week = _block(f"Week of {_span(week_start, week_end)}", _totals(week_rows))
    if week_start == month_start:
        return week
    month_name = week_end.strftime("%B")
    so_far = _block(
        f"{month_name} so far ({_span(month_start, week_end)})",
        _totals(month_rows),
    )
    return f"{week}\n\n{so_far}"


def next_digest_pause(now: datetime, *, sent_on: date | None) -> float:
    """Seconds until the next check. Short while a send is still owed."""
    if digest_is_due(now, sent_on) is not None:
        return 1.0
    return 15 * 60


def local_now(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))


def _totals(rows: Sequence[_Spend]) -> list[tuple[str, float]]:
    buckets: dict[str, float] = {}
    for row in rows:
        buckets[row.category] = buckets.get(row.category, 0.0) + row.amount_usd
    rounded = [(name, round(amount, 2)) for name, amount in buckets.items()]
    kept = [(name, amount) for name, amount in rounded if amount != 0]
    return sorted(kept, key=lambda item: (-item[1], item[0]))


def _block(title: str, totals: list[tuple[str, float]]) -> str:
    if not totals:
        return f"{title}\nNo expenses."
    grand = round(sum(amount for _, amount in totals), 2)
    width = max(len("Total"), *(len(name) for name, _ in totals))
    lines = [title]
    for name, amount in totals:
        lines.append(f"{name:<{width}}  ${amount:,.2f}")
    lines.append(f"{'Total':<{width}}  ${grand:,.2f}")
    return "\n".join(lines)


def _span(start: date, end: date) -> str:
    if start == end:
        return f"{start.strftime('%b')} {start.day}"
    if start.month == end.month:
        return f"{start.strftime('%b')} {start.day}–{end.day}"
    return f"{start.strftime('%b')} {start.day}–{end.strftime('%b')} {end.day}"
