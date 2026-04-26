"""Tests for the stage-2b retrieval-query extractor."""

from __future__ import annotations

import json
from datetime import date

from expense_tracker.extractor.categories import get_registry
from expense_tracker.extractor.retrieval_extractor import RetrievalExtractor
from expense_tracker.extractor.schemas import Intent

TODAY = date(2026, 4, 24)


def _build(fake_llm) -> RetrievalExtractor:
    return RetrievalExtractor(llm=fake_llm, registry=get_registry())


def test_extracts_period_total(fake_llm):
    fake_llm.queue_response(
        json.dumps(
            {
                "intent": "query_period_total",
                "time_range": {
                    "start": "2026-04-01",
                    "end": "2026-04-30",
                    "label": "April 2026",
                },
                "category": None,
                "vendor": None,
                "limit": None,
            }
        )
    )
    ext = _build(fake_llm)

    result = ext.extract(
        "how much did I spend in April",
        intent=Intent.QUERY_PERIOD_TOTAL,
        today=TODAY,
    )

    assert result.query.intent == Intent.QUERY_PERIOD_TOTAL
    assert result.query.time_range.start == date(2026, 4, 1)
    assert result.query.time_range.end == date(2026, 4, 30)
    assert result.query.time_range.label == "April 2026"
    assert result.query.category is None


def test_extracts_category_total(fake_llm):
    fake_llm.queue_response(
        json.dumps(
            {
                "intent": "query_category_total",
                "time_range": {
                    "start": "2026-04-01",
                    "end": "2026-04-30",
                    "label": "April",
                },
                "category": "groceries",  # alias — should normalise
                "vendor": None,
                "limit": None,
            }
        )
    )
    ext = _build(fake_llm)

    result = ext.extract(
        "how much for groceries in April",
        intent=Intent.QUERY_CATEGORY_TOTAL,
        today=TODAY,
    )

    assert result.query.category == "Groceries"


def test_extracts_query_day(fake_llm):
    fake_llm.queue_response(
        json.dumps(
            {
                "intent": "query_day",
                "time_range": {
                    "start": "2026-04-24",
                    "end": "2026-04-24",
                    "label": "24 April 2026",
                },
                "category": None,
                "vendor": None,
                "limit": None,
            }
        )
    )
    ext = _build(fake_llm)

    result = ext.extract(
        "what did I spend on 24 April",
        intent=Intent.QUERY_DAY,
        today=TODAY,
    )

    assert result.query.time_range.start == result.query.time_range.end


def test_extracts_query_recent_with_limit(fake_llm):
    fake_llm.queue_response(
        json.dumps(
            {
                "intent": "query_recent",
                "time_range": {
                    "start": "2026-01-01",
                    "end": "2026-12-31",
                    "label": "this year",
                },
                "category": None,
                "vendor": None,
                "limit": 5,
            }
        )
    )
    ext = _build(fake_llm)

    result = ext.extract(
        "show last 5 transactions",
        intent=Intent.QUERY_RECENT,
        today=TODAY,
    )

    assert result.query.limit == 5


def test_intent_overridden_to_match_stage1(fake_llm):
    """If the LLM rewrites intent in stage 2, stage 1's choice wins."""
    fake_llm.queue_response(
        json.dumps(
            {
                "intent": "query_period_total",  # LLM disagreed
                "time_range": {
                    "start": "2026-04-01",
                    "end": "2026-04-30",
                    "label": "April",
                },
                "category": "Food",
                "vendor": None,
                "limit": None,
            }
        )
    )
    ext = _build(fake_llm)

    result = ext.extract(
        "how much for food in April",
        intent=Intent.QUERY_CATEGORY_TOTAL,
        today=TODAY,
    )

    assert result.query.intent == Intent.QUERY_CATEGORY_TOTAL


def test_unresolved_category_is_dropped_not_fallback(fake_llm):
    """For queries, unresolved category becomes None (= no filter)."""
    fake_llm.queue_response(
        json.dumps(
            {
                "intent": "query_category_total",
                "time_range": {
                    "start": "2026-04-01",
                    "end": "2026-04-30",
                    "label": "April",
                },
                "category": "spaceship-fuel",
                "vendor": None,
                "limit": None,
            }
        )
    )
    ext = _build(fake_llm)

    result = ext.extract(
        "how much for spaceship-fuel in April",
        intent=Intent.QUERY_CATEGORY_TOTAL,
        today=TODAY,
    )

    assert result.query.category is None
