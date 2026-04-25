"""Tests for the stage-2a expense extractor."""

from __future__ import annotations

import json
from datetime import date

from expense_tracker.extractor.categories import get_registry
from expense_tracker.extractor.expense_extractor import ExpenseExtractor

TODAY = date(2026, 4, 24)


def _build(fake_llm) -> ExpenseExtractor:
    return ExpenseExtractor(
        llm=fake_llm,
        registry=get_registry(),
        default_currency="INR",
    )


def test_extracts_basic_expense(fake_llm):
    fake_llm.queue_response(
        json.dumps(
            {
                "date": "2026-04-24",
                "category": "Food",
                "amount": 40,
                "currency": "INR",
                "vendor": None,
                "note": None,
            }
        )
    )
    ext = _build(fake_llm)

    result = ext.extract("spent 40 on food today", today=TODAY)

    assert result.expense.date == date(2026, 4, 24)
    assert result.expense.category == "Food"
    assert result.expense.amount == 40.0
    assert result.expense.currency == "INR"


def test_normalises_alias_to_canonical(fake_llm):
    """If the LLM emits an alias instead of the canonical name, we collapse it."""
    fake_llm.queue_response(
        json.dumps(
            {
                "date": "2026-04-24",
                "category": "starbucks",  # alias
                "amount": 5,
                "currency": "USD",
                "vendor": "Starbucks",
                "note": None,
            }
        )
    )
    ext = _build(fake_llm)

    result = ext.extract("starbucks $5", today=TODAY)

    assert result.expense.category == "Coffee"
    assert result.expense.currency == "USD"


def test_unknown_category_falls_back_to_other(fake_llm):
    fake_llm.queue_response(
        json.dumps(
            {
                "date": "2026-04-24",
                "category": "spaceship-fuel",
                "amount": 100,
                "currency": "INR",
                "vendor": None,
                "note": None,
            }
        )
    )
    ext = _build(fake_llm)

    result = ext.extract("100 on spaceship fuel", today=TODAY)

    assert result.expense.category == "Other"


def test_currency_uppercased(fake_llm):
    fake_llm.queue_response(
        json.dumps(
            {
                "date": "2026-04-24",
                "category": "Food",
                "amount": 40,
                "currency": "inr",
                "vendor": None,
                "note": None,
            }
        )
    )
    ext = _build(fake_llm)

    result = ext.extract("food 40", today=TODAY)

    assert result.expense.currency == "INR"


def test_today_appears_in_prompt(fake_llm):
    fake_llm.queue_response(
        json.dumps(
            {
                "date": "2026-04-24",
                "category": "Food",
                "amount": 40,
                "currency": "INR",
            }
        )
    )
    ext = _build(fake_llm)

    ext.extract("spent 40 on food", today=TODAY)

    rendered = "\n".join(m.content for m in fake_llm.calls[0])
    assert "2026-04-24" in rendered
    assert "INR" in rendered
    assert "Food" in rendered  # category list embedded


def test_default_currency_appears_in_prompt(fake_llm):
    fake_llm.queue_response(
        json.dumps(
            {
                "date": "2026-04-24",
                "category": "Food",
                "amount": 40,
                "currency": "USD",
            }
        )
    )
    ext = ExpenseExtractor(
        llm=fake_llm,
        registry=get_registry(),
        default_currency="USD",
    )

    ext.extract("food 40", today=TODAY)

    rendered = "\n".join(m.content for m in fake_llm.calls[0])
    assert "USD" in rendered
