"""Unit tests for :class:`SummaryEngine` and :func:`format_summary`.

Covers:
* Window arithmetic — week / month / year, including month-end and
  year-over-year edge cases (Mar 31, Feb 29).
* Apples-to-apples comparison: prior window is the same length as the
  focal window, shifted back by one period.
* Aggregation correctness via the underlying :class:`RetrievalEngine`.
* Reply formatting: verbose CLI form + compact Telegram form.
* Failure path: :class:`RetrievalError` from the engine bubbles through
  :meth:`SummaryEngine.summarize`.

All tests use a :class:`FakeSheetsBackend` populated through the real
``init_transactions_tab`` + ``append_transactions`` helpers — same
pattern as ``test_pipeline_retrieval``.
"""

from __future__ import annotations

from datetime import date

import pytest

from expense_tracker.extractor.categories import get_registry
from expense_tracker.pipeline.retrieval import (
    RetrievalEngine,
    RetrievalError,
)
from expense_tracker.pipeline.summary import (
    Summary,
    SummaryEngine,
    SummaryScope,
    _build_current_window,
    _build_prior_window,
    format_summary,
)
from expense_tracker.sheets.backend import FakeSheetsBackend
from expense_tracker.sheets.exceptions import SheetsError
from expense_tracker.sheets.format import get_sheet_format
from expense_tracker.sheets.transactions import (
    TransactionRow,
    append_transactions,
    init_transactions_tab,
)

# ─── Helpers ────────────────────────────────────────────────────────────


def _engine_with(rows: list[dict]) -> SummaryEngine:
    backend = FakeSheetsBackend(title="Test Sheet", spreadsheet_id="sid")
    fmt = get_sheet_format()
    registry = get_registry()
    init_transactions_tab(backend, fmt)

    txn_rows: list[TransactionRow] = []
    for r in rows:
        d: date = r["date"]
        txn_rows.append(
            TransactionRow(
                date=d,
                day=d.strftime("%a"),
                month=d.strftime("%B"),
                year=d.year,
                category=r["category"],
                note=r.get("note"),
                vendor=r.get("vendor"),
                amount=r["amount"],
                currency=r.get("currency", "USD"),
                amount_usd=r.get("amount_usd", r["amount"]),
                fx_rate=r.get("fx_rate", 1.0),
                source="chat",
                trace_id=r.get("trace_id"),
                timestamp=None,
            )
        )
    if txn_rows:
        append_transactions(backend, fmt, txn_rows)
    retrieval = RetrievalEngine(
        backend=backend, sheet_format=fmt, registry=registry,
    )
    return SummaryEngine(retrieval_engine=retrieval)


# ─── Window math ────────────────────────────────────────────────────────


def test_week_window_is_seven_inclusive_days_ending_today():
    label, start, end = _build_current_window(SummaryScope.WEEK, date(2026, 4, 27))
    assert end == date(2026, 4, 27)
    assert start == date(2026, 4, 21)
    assert (end - start).days == 6  # 7 calendar days inclusive
    assert "7 days" in label.lower()


def test_week_prior_window_is_the_seven_days_immediately_before():
    label, start, end = _build_prior_window(SummaryScope.WEEK, date(2026, 4, 27))
    assert end == date(2026, 4, 20)
    assert start == date(2026, 4, 14)
    assert (end - start).days == 6
    assert "previous" in label.lower()


def test_month_window_is_first_to_today_inclusive():
    label, start, end = _build_current_window(SummaryScope.MONTH, date(2026, 4, 27))
    assert start == date(2026, 4, 1)
    assert end == date(2026, 4, 27)
    assert "April 2026" in label


def test_month_prior_window_is_apples_to_apples_thru_same_day():
    """Today is Apr 27 → prior is Mar 1..Mar 27 (same elapsed days)."""
    label, start, end = _build_prior_window(SummaryScope.MONTH, date(2026, 4, 27))
    assert start == date(2026, 3, 1)
    assert end == date(2026, 3, 27)
    assert "March 2026" in label


def test_month_prior_window_caps_to_last_day_of_short_prior_month():
    """Today is Mar 31 → prior month is Feb (28/29 days), so cap to
    the last valid day of Feb rather than emitting an invalid date."""
    # 2026 is not a leap year; Feb has 28 days.
    label, start, end = _build_prior_window(SummaryScope.MONTH, date(2026, 3, 31))
    assert start == date(2026, 2, 1)
    assert end == date(2026, 2, 28)
    assert "February 2026" in label
    # And in a leap year, we cap to Feb 29.
    label, start, end = _build_prior_window(SummaryScope.MONTH, date(2024, 3, 31))
    assert end == date(2024, 2, 29)


def test_year_window_is_jan_first_to_today_inclusive():
    label, start, end = _build_current_window(SummaryScope.YEAR, date(2026, 4, 27))
    assert start == date(2026, 1, 1)
    assert end == date(2026, 4, 27)
    assert "2026" in label


def test_year_prior_window_is_jan_first_thru_same_mmdd_of_prior_year():
    label, start, end = _build_prior_window(SummaryScope.YEAR, date(2026, 4, 27))
    assert start == date(2025, 1, 1)
    assert end == date(2025, 4, 27)
    assert "2025" in label


def test_year_prior_window_handles_feb_29_anchor():
    """A Feb 29 anchor in a leap year falls back to Feb 28 of last year."""
    label, start, end = _build_prior_window(SummaryScope.YEAR, date(2024, 2, 29))
    assert start == date(2023, 1, 1)
    # 2023 is not a leap year → Feb 28.
    assert end == date(2023, 2, 28)
    assert "2023" in label


# ─── Engine end-to-end (focal vs prior aggregation) ────────────────────


TODAY = date(2026, 4, 27)


def test_summarize_week_aggregates_focal_window_only():
    """Focal window includes today minus 6 days; prior excludes those."""
    engine = _engine_with([
        # In focal window (last 7 days)
        {"date": date(2026, 4, 22), "category": "Food", "amount": 12.0},
        {"date": date(2026, 4, 25), "category": "Groceries", "amount": 100.0},
        # In prior window only
        {"date": date(2026, 4, 17), "category": "Food", "amount": 99.0},
        # Outside both (older than prior)
        {"date": date(2026, 4, 1), "category": "House", "amount": 1000.0},
    ])

    summary = engine.summarize(SummaryScope.WEEK, today=TODAY)

    assert summary.scope == SummaryScope.WEEK
    assert summary.today == TODAY
    assert summary.total_usd == pytest.approx(112.0)
    assert summary.transaction_count == 2
    assert summary.prior_total_usd == pytest.approx(99.0)
    assert summary.prior_transaction_count == 1


def test_summarize_month_compares_to_prior_month_thru_same_day():
    engine = _engine_with([
        # Apr 1..27 (focal)
        {"date": date(2026, 4, 5), "category": "Food", "amount": 50.0},
        {"date": date(2026, 4, 27), "category": "Tesla Car", "amount": 200.0},
        # Mar 1..27 (prior)
        {"date": date(2026, 3, 10), "category": "Food", "amount": 25.0},
        # Mar 28..31 → strictly outside the prior window, must be excluded.
        {"date": date(2026, 3, 30), "category": "Food", "amount": 9999.0},
    ])

    summary = engine.summarize(SummaryScope.MONTH, today=TODAY)

    assert summary.total_usd == pytest.approx(250.0)
    assert summary.prior_total_usd == pytest.approx(25.0)
    assert summary.delta_usd == pytest.approx(225.0)
    assert summary.delta_pct is not None
    assert summary.delta_pct == pytest.approx(900.0)


def test_summarize_with_zero_prior_yields_none_pct_but_explains_in_reply():
    engine = _engine_with([
        {"date": date(2026, 4, 25), "category": "Food", "amount": 40.0},
    ])

    summary = engine.summarize(SummaryScope.WEEK, today=TODAY)

    assert summary.prior_total_usd == 0.0
    assert summary.delta_pct is None  # no division by zero
    assert summary.delta_usd == pytest.approx(40.0)
    rendered = format_summary(summary)
    assert "prior was zero" in rendered.lower()


def test_summarize_with_no_data_anywhere_says_no_spending():
    engine = _engine_with([])
    summary = engine.summarize(SummaryScope.WEEK, today=TODAY)
    assert summary.total_usd == 0.0
    assert summary.prior_total_usd == 0.0
    assert summary.transaction_count == 0
    rendered = format_summary(summary)
    assert "no spending" in rendered.lower()


def test_summarize_top_categories_sorted_descending():
    engine = _engine_with([
        {"date": date(2026, 4, 25), "category": "Food", "amount": 50.0},
        {"date": date(2026, 4, 25), "category": "Groceries", "amount": 200.0},
        {"date": date(2026, 4, 25), "category": "Tesla Car", "amount": 100.0},
    ])
    summary = engine.summarize(SummaryScope.WEEK, today=TODAY)
    cats = summary.top_categories(n=3)
    assert [c for c, _ in cats] == ["Groceries", "Tesla Car", "Food"]


def test_summarize_largest_is_the_biggest_focal_row():
    engine = _engine_with([
        {"date": date(2026, 4, 25), "category": "Food", "amount": 50.0},
        {"date": date(2026, 4, 26), "category": "Tesla Car", "amount": 234.56,
         "vendor": "Tesla", "note": "FSD upgrade"},
        # Bigger row but in the prior window — must NOT be the focal largest.
        {"date": date(2026, 4, 17), "category": "House", "amount": 9999.0},
    ])
    summary = engine.summarize(SummaryScope.WEEK, today=TODAY)
    assert summary.largest is not None
    assert summary.largest.amount_usd == pytest.approx(234.56)
    assert summary.largest.category == "Tesla Car"


def test_summarize_propagates_skipped_rows_from_focal_window():
    engine = _engine_with([
        {"date": date(2026, 4, 25), "category": "Food", "amount": 10.0},
    ])
    backend = engine._retriever._backend  # type: ignore[attr-defined]
    ws = backend.get_worksheet("Transactions")
    ws.append_rows([[
        "not-a-date", "Mon", "April", 2026, "Food", "", "", 8.0,
        "USD", 8.0, 1.0, "manual", "", "",
    ]])
    summary = engine.summarize(SummaryScope.WEEK, today=TODAY)
    # 1 valid focal row + 1 unparseable row in the same window.
    assert summary.transaction_count == 1
    assert summary.skipped_rows == 1
    assert "skipped" in format_summary(summary).lower()


def test_summarize_wraps_engine_failure_as_retrieval_error():
    """The CLI catches one error type — same shape as ``answer()``."""
    fmt = get_sheet_format()

    class _BoomBackend:
        title = "Boom"

        def has_worksheet(self, name):
            return True

        def get_worksheet(self, name):
            raise SheetsError("boom")

    retrieval = RetrievalEngine(
        backend=_BoomBackend(),  # type: ignore[arg-type]
        sheet_format=fmt,
        registry=get_registry(),
    )
    engine = SummaryEngine(retrieval_engine=retrieval)

    with pytest.raises(RetrievalError):
        engine.summarize(SummaryScope.WEEK, today=TODAY)


# ─── Reply formatting ──────────────────────────────────────────────────


def _summary_with_data() -> Summary:
    engine = _engine_with([
        {"date": date(2026, 4, 25), "category": "Food", "amount": 12.50,
         "vendor": "Cafe"},
        {"date": date(2026, 4, 26), "category": "Tesla Car", "amount": 200.0,
         "vendor": "Tesla"},
        {"date": date(2026, 4, 17), "category": "Food", "amount": 80.0},
    ])
    return engine.summarize(SummaryScope.WEEK, today=TODAY)


def test_format_summary_verbose_shows_period_total_top_largest_and_delta():
    s = _summary_with_data()
    rendered = format_summary(s, compact=False)
    assert "Summary (week, anchor 2026-04-27)" in rendered
    assert "last 7 days" in rendered
    assert "$212.50" in rendered
    # 2 expenses
    assert "2 expenses" in rendered
    # Top category line + percentages
    assert "Tesla Car $200.00 (94%)" in rendered or \
           "Tesla Car $200.00 (94%)," in rendered
    assert "Biggest" in rendered
    # Comparison line
    assert "previous 7 days" in rendered
    assert "Prior" in rendered
    assert "Delta" in rendered
    # Sign should be "+" since focal > prior
    assert "+$" in rendered.replace("\n", " ")


def test_format_summary_verbose_singular_one_expense():
    """Pluralization carries through the summary path too."""
    engine = _engine_with([
        {"date": date(2026, 4, 25), "category": "Food", "amount": 1.0},
    ])
    s = engine.summarize(SummaryScope.WEEK, today=TODAY)
    rendered = format_summary(s, compact=False)
    assert "1 expense" in rendered
    assert "1 expenses" not in rendered


def test_format_summary_compact_is_one_paragraph_for_telegram():
    s = _summary_with_data()
    rendered = format_summary(s, compact=True)
    # Compact form is a single paragraph (no \n) for phone screens.
    assert "\n" not in rendered
    assert "Last 7 days" in rendered
    assert "$212.50" in rendered
    assert "Top:" in rendered
    assert "Largest" in rendered
    assert "vs prior" in rendered.lower() or "Delta" in rendered


def test_format_summary_compact_mentions_skipped_rows():
    engine = _engine_with([
        {"date": date(2026, 4, 25), "category": "Food", "amount": 10.0},
    ])
    backend = engine._retriever._backend  # type: ignore[attr-defined]
    ws = backend.get_worksheet("Transactions")
    ws.append_rows([[
        "not-a-date", "Mon", "April", 2026, "Food", "", "", 8.0,
        "USD", 8.0, 1.0, "manual", "", "",
    ]])
    s = engine.summarize(SummaryScope.WEEK, today=TODAY)
    rendered = format_summary(s, compact=True)
    assert "skipped" in rendered.lower()
    assert "--inspect-ledger" in rendered


def test_summary_scope_enum_string_values_match_cli_choices():
    """``argparse(choices=…)`` and the Telegram parser both consume
    plain lowercase strings; pin the enum values."""
    assert SummaryScope.WEEK.value == "week"
    assert SummaryScope.MONTH.value == "month"
    assert SummaryScope.YEAR.value == "year"
