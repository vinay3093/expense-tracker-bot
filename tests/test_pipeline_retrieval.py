"""Unit tests for :class:`RetrievalEngine`.

Covers the read-side of the chat pipeline: ledger parsing, time-window
+ category + vendor filtering, aggregation, recent-N slicing, and
graceful failure handling.

The tests use a :class:`FakeSheetsBackend` populated through the real
:func:`init_transactions_tab` + :func:`append_transactions` helpers, so
the schema (column order + types) matches what the engine expects to
read from a real Sheet.
"""

from __future__ import annotations

from datetime import date

import pytest

from expense_tracker.extractor.categories import get_registry
from expense_tracker.extractor.schemas import Intent, RetrievalQuery, TimeRange
from expense_tracker.pipeline.retrieval import (
    LedgerRow,
    RetrievalAnswer,
    RetrievalEngine,
    RetrievalError,
    _parse_ledger_row,
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


def _engine_with_seed(rows: list[dict]) -> RetrievalEngine:
    """Build an engine wired to a pre-populated FakeSheetsBackend."""
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
    return RetrievalEngine(backend=backend, sheet_format=fmt, registry=registry)


def _april_query(
    intent: Intent,
    *,
    category: str | None = None,
    vendor: str | None = None,
    limit: int | None = None,
    label: str = "April 2026",
    start: date = date(2026, 4, 1),
    end: date = date(2026, 4, 30),
) -> RetrievalQuery:
    return RetrievalQuery(
        intent=intent,
        time_range=TimeRange(start=start, end=end, label=label),
        category=category,
        vendor=vendor,
        limit=limit,
    )


# ─── Empty / missing ledger ────────────────────────────────────────────


def test_missing_transactions_tab_returns_empty_answer():
    """No tab at all → empty answer (not an error)."""
    backend = FakeSheetsBackend(title="Test", spreadsheet_id="sid")
    fmt = get_sheet_format()
    engine = RetrievalEngine(
        backend=backend, sheet_format=fmt, registry=get_registry(),
    )

    answer = engine.answer(_april_query(Intent.QUERY_PERIOD_TOTAL))

    assert isinstance(answer, RetrievalAnswer)
    assert answer.transaction_count == 0
    assert answer.total_usd == 0.0
    assert answer.matched_rows == []
    assert answer.by_category == {}
    assert answer.largest is None


def test_empty_ledger_returns_empty_answer():
    """Tab exists but no data rows → empty answer."""
    engine = _engine_with_seed([])

    answer = engine.answer(_april_query(Intent.QUERY_PERIOD_TOTAL))

    assert answer.transaction_count == 0
    assert answer.total_usd == 0.0


# ─── Period-total aggregation ──────────────────────────────────────────


def test_period_total_sums_only_in_window():
    """Out-of-window rows must not contribute."""
    engine = _engine_with_seed([
        # In window
        {"date": date(2026, 4, 5),  "category": "Food", "amount": 12.50},
        {"date": date(2026, 4, 18), "category": "Food", "amount": 45.00},
        # Out of window
        {"date": date(2026, 3, 30), "category": "Food", "amount": 100.00},
        {"date": date(2026, 5, 1),  "category": "Food", "amount": 200.00},
    ])

    answer = engine.answer(_april_query(Intent.QUERY_PERIOD_TOTAL))

    assert answer.transaction_count == 2
    assert answer.total_usd == pytest.approx(57.50)
    assert answer.by_category == {"Food": 57.50}
    assert answer.by_day == {date(2026, 4, 5): 12.50, date(2026, 4, 18): 45.0}


def test_period_total_groups_by_category():
    """Multiple categories surface with their per-category totals."""
    engine = _engine_with_seed([
        {"date": date(2026, 4, 5),  "category": "Food",      "amount": 12.50},
        {"date": date(2026, 4, 8),  "category": "Groceries", "amount": 88.00},
        {"date": date(2026, 4, 18), "category": "Food",      "amount": 45.00},
        {"date": date(2026, 4, 22), "category": "Tesla Car", "amount": 240.00},
    ])

    answer = engine.answer(_april_query(Intent.QUERY_PERIOD_TOTAL))

    assert answer.transaction_count == 4
    assert answer.total_usd == pytest.approx(385.50)
    assert answer.by_category == {
        "Food": 57.50,
        "Groceries": 88.00,
        "Tesla Car": 240.00,
    }


def test_largest_row_picked_by_amount_usd():
    """``largest`` is the single biggest USD row in the window."""
    engine = _engine_with_seed([
        {"date": date(2026, 4, 5),  "category": "Food", "amount": 12.50},
        {"date": date(2026, 4, 18), "category": "Food", "amount": 45.00,
         "vendor": "Chipotle"},
        {"date": date(2026, 4, 22), "category": "Food", "amount": 30.00},
    ])

    answer = engine.answer(_april_query(Intent.QUERY_PERIOD_TOTAL))

    assert answer.largest is not None
    assert answer.largest.amount_usd == pytest.approx(45.0)
    assert answer.largest.vendor == "Chipotle"


# ─── Category filtering ────────────────────────────────────────────────


def test_category_total_filters_to_named_category():
    engine = _engine_with_seed([
        {"date": date(2026, 4, 5),  "category": "Food",      "amount": 12.50},
        {"date": date(2026, 4, 8),  "category": "Groceries", "amount": 88.00},
        {"date": date(2026, 4, 18), "category": "Food",      "amount": 45.00},
    ])

    answer = engine.answer(
        _april_query(Intent.QUERY_CATEGORY_TOTAL, category="Food"),
    )

    assert answer.transaction_count == 2
    assert answer.total_usd == pytest.approx(57.50)
    assert answer.by_category == {"Food": 57.50}


def test_category_alias_resolves_to_canonical_name():
    """LLM may pass an alias like 'groceries' — engine canonicalises."""
    engine = _engine_with_seed([
        {"date": date(2026, 4, 5), "category": "Groceries", "amount": 88.00},
        {"date": date(2026, 4, 8), "category": "Food",      "amount": 12.00},
    ])

    answer = engine.answer(
        _april_query(Intent.QUERY_CATEGORY_TOTAL, category="groceries"),
    )

    assert answer.transaction_count == 1
    assert answer.total_usd == pytest.approx(88.00)


def test_unknown_category_yields_zero_matches_not_fallback():
    """Querying an unknown label must not silently rewrite to fallback.

    A typo'd category should match zero rows (so the user sees
    "no expenses found"), not get rerouted to "Miscellaneous".
    """
    engine = _engine_with_seed([
        {"date": date(2026, 4, 5),  "category": "Miscellaneous", "amount": 10.00},
        {"date": date(2026, 4, 18), "category": "Food",          "amount": 45.00},
    ])

    answer = engine.answer(
        _april_query(Intent.QUERY_CATEGORY_TOTAL, category="Tooomtcoats"),
    )

    assert answer.transaction_count == 0
    assert answer.total_usd == 0.0


# ─── Vendor filtering ──────────────────────────────────────────────────


def test_vendor_filter_is_case_insensitive_substring_match():
    engine = _engine_with_seed([
        {"date": date(2026, 4, 5),  "category": "Food", "amount": 12.50,
         "vendor": "Starbucks"},
        {"date": date(2026, 4, 8),  "category": "Food", "amount": 6.50,
         "vendor": "starbucks reserve"},
        {"date": date(2026, 4, 18), "category": "Food", "amount": 45.00,
         "vendor": "Chipotle"},
    ])

    q = _april_query(Intent.QUERY_PERIOD_TOTAL, vendor="STARBUCKS")
    answer = engine.answer(q)

    assert answer.transaction_count == 2
    assert answer.total_usd == pytest.approx(19.0)


# ─── Day-detail ────────────────────────────────────────────────────────


def test_day_detail_returns_only_that_day():
    engine = _engine_with_seed([
        {"date": date(2026, 4, 24), "category": "Food",      "amount": 12.50,
         "vendor": "Cafe"},
        {"date": date(2026, 4, 25), "category": "Food",      "amount": 40.00,
         "vendor": "Starbucks"},
        {"date": date(2026, 4, 25), "category": "Groceries", "amount": 35.00,
         "vendor": "Costco"},
        {"date": date(2026, 4, 26), "category": "Saloon",    "amount": 12.50},
    ])

    q = RetrievalQuery(
        intent=Intent.QUERY_DAY,
        time_range=TimeRange(
            start=date(2026, 4, 25), end=date(2026, 4, 25),
            label="Sat 25 Apr 2026",
        ),
    )
    answer = engine.answer(q)

    assert answer.transaction_count == 2
    assert answer.total_usd == pytest.approx(75.0)
    assert {r.category for r in answer.matched_rows} == {"Food", "Groceries"}


# ─── Recent-N slicing ──────────────────────────────────────────────────


def test_query_recent_returns_last_n_newest_first():
    engine = _engine_with_seed([
        {"date": date(2026, 4, d), "category": "Food", "amount": 10.0 + d,
         "vendor": f"V{d}"}
        for d in (5, 8, 12, 18, 24, 26)
    ])

    q = _april_query(Intent.QUERY_RECENT, limit=3)
    answer = engine.answer(q)

    # Aggregates are over the FULL window…
    assert answer.transaction_count == 6
    # …but only the top N are echoed back.
    assert len(answer.matched_rows) == 3
    assert [r.date.day for r in answer.matched_rows] == [26, 24, 18]


def test_query_recent_default_limit_is_five():
    """RetrievalQuery may have ``limit=None`` for QUERY_RECENT — engine
    falls back to a sensible default of 5."""
    engine = _engine_with_seed([
        {"date": date(2026, 4, d), "category": "Food", "amount": 10.0 + d}
        for d in range(2, 12)  # 10 rows in April
    ])

    q = _april_query(Intent.QUERY_RECENT, limit=None)
    answer = engine.answer(q)

    assert answer.transaction_count == 10
    assert len(answer.matched_rows) == 5


# ─── Multi-currency aggregation ────────────────────────────────────────


def test_aggregations_use_amount_usd_not_amount():
    """A 499 INR row that converted to $5.99 must contribute $5.99,
    not 499."""
    engine = _engine_with_seed([
        {"date": date(2026, 4, 5),  "category": "Digital",
         "amount": 499.0, "currency": "INR",
         "amount_usd": 5.99, "fx_rate": 0.012},
        {"date": date(2026, 4, 18), "category": "Digital",
         "amount": 9.99, "currency": "USD",
         "amount_usd": 9.99, "fx_rate": 1.0},
    ])

    answer = engine.answer(_april_query(Intent.QUERY_PERIOD_TOTAL))

    assert answer.transaction_count == 2
    assert answer.total_usd == pytest.approx(15.98)


# ─── Skipped row handling ─────────────────────────────────────────────


def test_unparseable_date_increments_skipped_count_does_not_crash():
    """One bad row must not black-hole the rest of the answer."""
    engine = _engine_with_seed([
        {"date": date(2026, 4, 5), "category": "Food", "amount": 10.0},
    ])

    # Inject a row with a malformed Date cell directly into the fake.
    backend = engine._backend  # type: ignore[attr-defined]
    ws = backend.get_worksheet("Transactions")
    ws.append_rows([[
        "not-a-date", "Mon", "April", 2026, "Food", "", "", 8.0,
        "USD", 8.0, 1.0, "manual", "", "",
    ]])

    answer = engine.answer(_april_query(Intent.QUERY_PERIOD_TOTAL))

    assert answer.transaction_count == 1
    assert answer.total_usd == pytest.approx(10.0)
    assert answer.skipped_rows == 1


# ─── Underlying SheetsError surfaces as RetrievalError ─────────────────


def test_sheets_error_during_read_wraps_into_retrieval_error():
    """Read failures bubble up as :class:`RetrievalError`, not raw
    ``SheetsError`` — chat pipeline catches one type."""
    fmt = get_sheet_format()

    class _BoomBackend:
        def has_worksheet(self, _name):
            return True

        def get_worksheet(self, _name):
            raise SheetsError("boom")

    engine = RetrievalEngine(
        backend=_BoomBackend(),  # type: ignore[arg-type]
        sheet_format=fmt,
        registry=get_registry(),
    )

    with pytest.raises(RetrievalError) as excinfo:
        engine.answer(_april_query(Intent.QUERY_PERIOD_TOTAL))

    assert "boom" in str(excinfo.value)
    assert isinstance(excinfo.value.cause, SheetsError)


# ─── _parse_ledger_row pinned-shape tests ──────────────────────────────


def test_parse_ledger_row_returns_none_for_empty_date():
    """Rows with no Date cell are blank → drop silently (return None)."""
    out = _parse_ledger_row([], sheet_row=2)
    assert out is None


def test_parse_ledger_row_handles_missing_optional_cells():
    """Note / Vendor / Trace ID parse to ``None`` when blank, not ``""``.
    Easier to format in the reply layer ("(Costco: lunch)" vs "(  : )").
    """
    row_values = [
        "2026-04-24", "Fri", "April", 2026, "Food", "", "",
        12.5, "USD", 12.5, 1.0, "chat", "", "",
    ]
    parsed = _parse_ledger_row(row_values, sheet_row=2)
    assert isinstance(parsed, LedgerRow)
    assert parsed.note is None
    assert parsed.vendor is None
    assert parsed.trace_id is None


def test_parse_ledger_row_recovers_year_from_date_when_year_missing():
    """If Year cell is blank or junk, derive from Date so older
    schemas still parse cleanly."""
    row_values = [
        "2026-04-24", "Fri", "April", "", "Food", "", "",
        12.5, "USD", 12.5, 1.0, "chat", "", "",
    ]
    parsed = _parse_ledger_row(row_values, sheet_row=2)
    assert isinstance(parsed, LedgerRow)
    assert parsed.year == 2026


def test_to_action_dict_carries_query_metadata():
    """The action dict mirrors the query so audit log says exactly what
    was asked."""
    engine = _engine_with_seed([
        {"date": date(2026, 4, 5),  "category": "Food", "amount": 12.50},
    ])
    q = _april_query(Intent.QUERY_CATEGORY_TOTAL, category="Food")

    action = engine.answer(q).to_action_dict()

    assert action["type"] == "sheets_query"
    assert action["status"] == "ok"
    assert action["intent"] == "query_category_total"
    assert action["category"] == "Food"
    assert action["start"] == "2026-04-01"
    assert action["end"] == "2026-04-30"
    assert action["label"] == "April 2026"
    assert action["total_usd"] == 12.50
    assert action["transaction_count"] == 1
