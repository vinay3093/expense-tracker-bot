"""Tests for the JSON-repair utilities."""

from __future__ import annotations

import json

import pytest

from expense_tracker.llm._json_repair import (
    build_schema_grounding,
    extract_json,
    parse_llm_json,
)


def test_extract_unwraps_code_fence_with_lang() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert extract_json(raw) == '{"a": 1}'


def test_extract_unwraps_code_fence_without_lang() -> None:
    raw = '```\n{"a": 1}\n```'
    assert extract_json(raw) == '{"a": 1}'


def test_extract_strips_leading_and_trailing_prose() -> None:
    raw = 'Sure, here is the JSON you asked for: {"a": 1}. Hope that helps!'
    assert extract_json(raw) == '{"a": 1}'


def test_extract_replaces_smart_quotes() -> None:
    raw = "{\u201ccategory\u201d: \u201cFood\u201d}"
    cleaned = extract_json(raw)
    assert json.loads(cleaned) == {"category": "Food"}


def test_extract_idempotent_on_clean_input() -> None:
    raw = '{"a": 1, "b": 2}'
    assert extract_json(raw) == raw


def test_parse_returns_dict() -> None:
    assert parse_llm_json('```json\n{"x": 5}\n```') == {"x": 5}


def test_parse_raises_on_unrecoverable_garbage() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_llm_json("definitely not even close to json")


def test_schema_grounding_contains_schema_keys() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        category: str
        amount: float

    text = build_schema_grounding(M)
    assert "JSON" in text
    assert '"category"' in text
    assert '"amount"' in text
    # And it must be valid JSON-Schema text — at least parseable on the
    # JSON portion after the prose intro.
    schema_start = text.index("{")
    parsed = json.loads(text[schema_start:])
    assert "properties" in parsed
    assert "category" in parsed["properties"]
