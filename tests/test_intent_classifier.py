"""Tests for the stage-1 intent classifier."""

from __future__ import annotations

import json

import pytest

from expense_tracker.extractor.intent_classifier import IntentClassifier
from expense_tracker.extractor.schemas import Intent
from expense_tracker.llm.exceptions import LLMBadResponseError


def _queue_intent(fake_llm, intent: Intent, *, confidence: float = 0.9) -> None:
    fake_llm.queue_response(
        json.dumps(
            {
                "intent": intent.value,
                "confidence": confidence,
                "reasoning": "test fixture",
            }
        )
    )


def test_classifies_log_expense(fake_llm):
    _queue_intent(fake_llm, Intent.LOG_EXPENSE)
    clf = IntentClassifier(llm=fake_llm)

    result = clf.classify("spent 40 on coffee yesterday")

    assert result.classification.intent == Intent.LOG_EXPENSE
    assert result.classification.confidence == 0.9
    assert result.response.provider == "fake"


def test_classifies_query_period_total(fake_llm):
    _queue_intent(fake_llm, Intent.QUERY_PERIOD_TOTAL, confidence=0.8)
    clf = IntentClassifier(llm=fake_llm)

    result = clf.classify("how much did I spend in April")

    assert result.classification.intent == Intent.QUERY_PERIOD_TOTAL
    assert result.classification.confidence == 0.8


def test_classifies_query_category_total(fake_llm):
    _queue_intent(fake_llm, Intent.QUERY_CATEGORY_TOTAL)
    clf = IntentClassifier(llm=fake_llm)

    result = clf.classify("how much for food in April")

    assert result.classification.intent == Intent.QUERY_CATEGORY_TOTAL


def test_classifies_query_day(fake_llm):
    _queue_intent(fake_llm, Intent.QUERY_DAY)
    clf = IntentClassifier(llm=fake_llm)

    result = clf.classify("what did I spend on 24 April")

    assert result.classification.intent == Intent.QUERY_DAY


def test_classifies_smalltalk(fake_llm):
    _queue_intent(fake_llm, Intent.SMALLTALK)
    clf = IntentClassifier(llm=fake_llm)

    result = clf.classify("thanks!")

    assert result.classification.intent == Intent.SMALLTALK


def test_invalid_confidence_raises(fake_llm):
    fake_llm.queue_response(
        json.dumps({"intent": "log_expense", "confidence": 2.0, "reasoning": ""})
    )
    clf = IntentClassifier(llm=fake_llm)

    with pytest.raises(LLMBadResponseError):
        clf.classify("spent 40")


def test_unknown_intent_value_raises(fake_llm):
    fake_llm.queue_response(
        json.dumps(
            {"intent": "transfer_money", "confidence": 0.9, "reasoning": ""}
        )
    )
    clf = IntentClassifier(llm=fake_llm)

    with pytest.raises(LLMBadResponseError):
        clf.classify("send 40 to Alice")


def test_user_text_appears_in_prompt(fake_llm):
    _queue_intent(fake_llm, Intent.LOG_EXPENSE)
    clf = IntentClassifier(llm=fake_llm)
    text = "spent 40 on chai"

    clf.classify(text)

    sent_messages = fake_llm.calls[0]
    rendered = "\n".join(m.content for m in sent_messages)
    assert text in rendered
    # System prompt mentions the taxonomy.
    assert "log_expense" in rendered
    assert "smalltalk" in rendered
