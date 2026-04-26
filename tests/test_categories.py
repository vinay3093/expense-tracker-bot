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
    assert "Miscellaneous" in reg.canonical_names()
    assert reg.canonical_names()[0] == "Food"
    assert reg.fallback_category == "Miscellaneous"
    assert reg.schema_version == 1


def test_default_yaml_file_loads_via_get_registry(isolated_env):
    isolated_env(LLM_PROVIDER="fake")
    reg = get_registry()
    names = reg.canonical_names()
    # The user's actual taxonomy.
    for expected in (
        "Groceries",
        "House",
        "Food",
        "Party",
        "Medicines",
        "Shopping",
        "Movies",
        "Saloon",
        "India Expense",
        "Travelling",
        "Miscellaneous",
        "Digital",
        "Tesla Car",
    ):
        assert expected in names, f"missing canonical category {expected!r}"
    assert FALLBACK_CATEGORY == reg.fallback_category == "Miscellaneous"


def test_get_registry_uses_override_path(isolated_env, tmp_path: Path):
    yaml_text = textwrap.dedent(
        """
        schema_version: 1
        fallback_category: Misc
        categories:
          - name: Food
            aliases: [groc]
          - name: Misc
        """
    )
    custom = tmp_path / "cats.yaml"
    custom.write_text(yaml_text)

    isolated_env(
        LLM_PROVIDER="fake",
        EXTRACTOR_CATEGORIES_FILE=str(custom),
    )
    reg = get_registry()
    assert reg.canonical_names() == ["Food", "Misc"]
    assert reg.fallback_category == "Misc"
    assert reg.resolve("groc") == "Food"


# ─── Alias resolution ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("user_input", "expected"),
    [
        # Groceries (supermarket) — opposite of the LangChain default mapping.
        ("groceries", "Groceries"),
        ("Groceries", "Groceries"),
        ("GROCERIES", "Groceries"),
        ("  groceries  ", "Groceries"),
        ("'groceries.'", "Groceries"),
        ("trader joes", "Groceries"),
        ("walmart", "Groceries"),
        # Food = restaurants, dining out, coffee, juice, ice cream.
        ("food", "Food"),
        ("starbucks", "Food"),
        ("dinner", "Food"),
        ("juice", "Food"),
        ("ice cream", "Food"),
        # House = rent, electricity, wifi.
        ("rent", "House"),
        ("electricity", "House"),
        ("wifi", "House"),
        ("digital apartment charge", "House"),
        # Digital = streaming subscriptions.
        ("netflix", "Digital"),
        ("Netflix", "Digital"),
        ("hotstar", "Digital"),
        ("spotify", "Digital"),
        # Tesla Car.
        ("tesla", "Tesla Car"),
        ("supercharger", "Tesla Car"),
        ("fsd", "Tesla Car"),
        # India Expense.
        ("remitly", "India Expense"),
        ("sent to india", "India Expense"),
        # Travelling.
        ("flight", "Travelling"),
        ("hotel", "Travelling"),
        ("airbnb", "Travelling"),
        # Saloon.
        ("haircut", "Saloon"),
        ("facial", "Saloon"),
        # Medicines.
        ("doctor", "Medicines"),
        ("pharmacy", "Medicines"),
        # Movies.
        ("movie", "Movies"),
        ("amc", "Movies"),
        # Party.
        ("alcohol", "Party"),
        ("beer", "Party"),
        # Shopping.
        ("amazon", "Shopping"),
        ("clothes", "Shopping"),
        # Miscellaneous catch-all.
        ("uber", "Miscellaneous"),
        ("parking", "Miscellaneous"),
        ("late fee", "Miscellaneous"),
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


def test_resolve_or_fallback_uses_registry_fallback():
    reg = get_registry()
    assert reg.resolve_or_fallback("crypto-mining-rig") == reg.fallback_category
    assert reg.resolve_or_fallback("groceries") == "Groceries"


def test_resolve_or_fallback_module_constant_matches():
    reg = get_registry()
    assert reg.fallback_category == FALLBACK_CATEGORY


# ─── Validation errors ──────────────────────────────────────────────────

def test_duplicate_canonical_names_raises():
    data = {
        "schema_version": 1,
        "fallback_category": "Other",
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
        "fallback_category": "Other",
        "categories": [
            {"name": "Food", "aliases": ["x"]},
            {"name": "Drinks", "aliases": ["x"]},
            {"name": "Other"},
        ],
    }
    with pytest.raises(ValueError, match="alias 'x' maps to both"):
        CategoryRegistry.from_dict(data)


def test_missing_fallback_category_raises():
    data = {
        "schema_version": 1,
        "fallback_category": "DoesNotExist",
        "categories": [{"name": "Food"}],
    }
    with pytest.raises(ValueError, match="fallback_category 'DoesNotExist' must"):
        CategoryRegistry.from_dict(data)


def test_default_fallback_when_unspecified():
    """When YAML omits fallback_category, default to module constant."""
    data = {
        "schema_version": 1,
        "categories": [
            {"name": "Food"},
            {"name": "Miscellaneous"},  # default fallback name must be present
        ],
    }
    reg = CategoryRegistry.from_dict(data)
    assert reg.fallback_category == "Miscellaneous"


# ─── Prompt rendering ───────────────────────────────────────────────────

def test_prompt_block_lists_canonical_names_and_hints():
    reg = get_registry()
    block = reg.prompt_block()
    assert "Allowed categories" in block
    assert "Groceries" in block
    assert "Tesla Car" in block
    assert "—  supermarket" in block  # hint formatting on Groceries
    # The fallback notice is emitted at the bottom.
    assert "'Miscellaneous'" in block


# ─── Helpers ────────────────────────────────────────────────────────────

def _minimal_yaml_dict() -> dict:
    return {
        "schema_version": 1,
        "fallback_category": "Miscellaneous",
        "categories": [
            {"name": "Food", "aliases": ["groc"]},
            {"name": "Miscellaneous"},
        ],
    }
