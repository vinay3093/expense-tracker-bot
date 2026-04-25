"""Tests for :mod:`expense_tracker.extractor.categories`."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from expense_tracker.extractor.categories import (
    FALLBACK_CATEGORY,
    CategoryRegistry,
    get_registry,
)

# ─── Building a registry ────────────────────────────────────────────────

def test_default_registry_loads_and_has_fallback():
    reg = CategoryRegistry.from_dict(_minimal_yaml_dict())
    assert "Other" in reg.canonical_names()
    assert reg.canonical_names()[0] == "Food"
    assert reg.schema_version == 1


def test_default_yaml_file_loads_via_get_registry(isolated_env):
    isolated_env(LLM_PROVIDER="fake")
    reg = get_registry()
    names = reg.canonical_names()
    assert "Food" in names
    assert "Restaurants" in names
    assert FALLBACK_CATEGORY in names


def test_get_registry_uses_override_path(isolated_env, tmp_path: Path):
    yaml_text = textwrap.dedent(
        """
        schema_version: 1
        categories:
          - name: Food
            aliases: [groc]
          - name: Other
        """
    )
    custom = tmp_path / "cats.yaml"
    custom.write_text(yaml_text)

    isolated_env(
        LLM_PROVIDER="fake",
        EXTRACTOR_CATEGORIES_FILE=str(custom),
    )
    reg = get_registry()
    assert reg.canonical_names() == ["Food", "Other"]
    assert reg.resolve("groc") == "Food"


# ─── Alias resolution ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("user_input", "expected"),
    [
        ("groceries", "Food"),
        ("Groceries", "Food"),
        ("GROCERIES", "Food"),
        ("  groceries  ", "Food"),
        ("'groceries.'", "Food"),
        ("starbucks", "Coffee"),
        ("uber", "Transport"),
        ("Netflix", "Bills"),
        ("Food", "Food"),
        ("Other", "Other"),
    ],
)
def test_resolve_aliases(user_input: str, expected: str):
    reg = get_registry()
    assert reg.resolve(user_input) == expected


def test_resolve_unknown_returns_none():
    reg = get_registry()
    assert reg.resolve("crypto-mining-rig") is None
    assert reg.resolve("") is None
    assert reg.resolve(None) is None


def test_resolve_or_fallback_uses_fallback_constant():
    reg = get_registry()
    assert reg.resolve_or_fallback("crypto-mining-rig") == FALLBACK_CATEGORY
    assert reg.resolve_or_fallback("groceries") == "Food"


# ─── Validation errors ──────────────────────────────────────────────────

def test_duplicate_canonical_names_raises():
    data = {
        "schema_version": 1,
        "categories": [
            {"name": "Food"},
            {"name": "food"},  # case-insensitive duplicate
            {"name": "Other"},
        ],
    }
    with pytest.raises(ValueError, match="duplicate canonical"):
        CategoryRegistry.from_dict(data)


def test_alias_collision_raises():
    data = {
        "schema_version": 1,
        "categories": [
            {"name": "Food", "aliases": ["x"]},
            {"name": "Drinks", "aliases": ["x"]},
            {"name": "Other"},
        ],
    }
    with pytest.raises(ValueError, match="alias 'x' maps to both"):
        CategoryRegistry.from_dict(data)


def test_missing_other_category_raises():
    data = {
        "schema_version": 1,
        "categories": [{"name": "Food"}],
    }
    with pytest.raises(ValueError, match="must define a canonical 'Other'"):
        CategoryRegistry.from_dict(data)


# ─── Prompt rendering ───────────────────────────────────────────────────

def test_prompt_block_lists_canonical_names_and_hints():
    reg = get_registry()
    block = reg.prompt_block()
    assert "Allowed categories" in block
    assert "Food" in block
    assert "—  groceries" in block  # hint formatting


# ─── Helpers ────────────────────────────────────────────────────────────

def _minimal_yaml_dict() -> dict:
    return {
        "schema_version": 1,
        "categories": [
            {"name": "Food", "aliases": ["groc"]},
            {"name": "Other"},
        ],
    }
