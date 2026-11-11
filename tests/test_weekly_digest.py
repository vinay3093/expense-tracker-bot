"""Sunday digest: one block on the first Sunday, two after that."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from expense_tracker.pipeline.weekly import (
    build_digest,
    digest_is_due,
    digest_windows,
)


def _row(day: int, category: str, amount: float, month: int = 11):
    return SimpleNamespace(
        date=date(2026, month, day),
        category=category,
        amount_usd=amount,
    )


def test_first_sunday_of_the_month_is_one_block():
    # 2026-11-01 is a Sunday, so the week and the month are the same day.
    assert digest_windows(date(2026, 11, 1)) == (
        date(2026, 11, 1),
        date(2026, 11, 1),
        date(2026, 11, 1),
    )
    text = build_digest(date(2026, 11, 1), [_row(1, "Food", 12.5)])
    assert text.startswith("Week of Nov 1\n")
    assert "so far" not in text
    assert "Food" in text
    assert "$12.50" in text


def test_second_sunday_shows_the_week_and_the_month():
    assert digest_windows(date(2026, 11, 8)) == (
        date(2026, 11, 2),
        date(2026, 11, 8),
        date(2026, 11, 1),
    )
    text = build_digest(
        date(2026, 11, 8),
        [
            _row(1, "Food", 10),
            _row(3, "Food", 6.4),
            _row(3, "Party", 42),
            _row(8, "Tesla Car", 18.27),
            _row(20, "Food", 99),
        ],
    )
    week, month = text.split("\n\n")
    assert week.startswith("Week of Nov 2–8")
    assert "Food" in week and "$6.40" in week
    assert "Party" in week and "$42.00" in week
    assert "Tesla Car" in week
    assert "$10.00" not in week
    assert "$99.00" not in week
    assert month.startswith("November so far (Nov 1–8)")
    assert "$16.40" in month
    assert "$76.67" in month


def test_new_month_does_not_include_the_previous_month():
    # 2026-12-06 is a Sunday. Monday is Nov 30, which must be clipped off.
    assert digest_windows(date(2026, 12, 6))[0] == date(2026, 12, 1)
    text = build_digest(
        date(2026, 12, 6),
        [_row(30, "Food", 5), _row(3, "Food", 8, month=12)],
    )
    assert "so far" not in text
    assert "$8.00" in text
    assert "$5.00" not in text


def test_empty_week_still_reports_the_month():
    text = build_digest(date(2026, 11, 8), [_row(1, "Groceries", 20)])
    assert "Week of Nov 2–8\nNo expenses." in text
    assert "Groceries" in text
    assert "$20.00" in text


def test_a_wednesday_report_covers_monday_through_today():
    assert digest_windows(date(2026, 11, 11)) == (
        date(2026, 11, 9),
        date(2026, 11, 11),
        date(2026, 11, 1),
    )


def test_digest_waits_until_sunday_evening():
    tz = ZoneInfo("America/Chicago")
    sunday_morning = datetime(2026, 11, 8, 11, 0, tzinfo=tz)
    sunday_evening = datetime(2026, 11, 8, 18, 0, tzinfo=tz)
    monday = datetime(2026, 11, 9, 18, 0, tzinfo=tz)
    assert digest_is_due(sunday_morning, None) is None
    assert digest_is_due(sunday_evening, None) == date(2026, 11, 8)
    assert digest_is_due(sunday_evening, date(2026, 11, 8)) is None
    assert digest_is_due(monday, None) is None
    with pytest.raises(ValueError):
        digest_is_due(datetime(2026, 11, 8, 18, 0), None)
