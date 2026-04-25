"""Category taxonomy registry.

Loads the category list from ``data/categories.yaml`` (or a user-supplied
override path) and provides:

* :meth:`CategoryRegistry.canonical_names` — the display names that go
  into the Sheet column header.
* :meth:`CategoryRegistry.resolve` — map any alias the LLM emitted to
  its canonical name; returns ``None`` if unresolved (caller decides
  whether to fall back to ``"Other"``).
* :meth:`CategoryRegistry.prompt_block` — formatted text block sent to
  the LLM as part of the extraction prompt.

Why a separate registry rather than hard-coded constants:

* Step 4 will swap ``categories.yaml`` for a file generated from your
  actual Sheet column headers; we want zero code changes on that day.
* Personalising the alias table over time is the lowest-effort,
  highest-payoff way to improve extractor accuracy on your idioms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import get_settings

_DEFAULT_DATA_FILE = Path(__file__).parent / "data" / "categories.yaml"

#: Canonical name used as the catch-all when no alias resolves. Keeping
#: this as a constant (not a Settings field) avoids an extra knob nobody
#: needs to tune.
FALLBACK_CATEGORY: str = "Other"


@dataclass(frozen=True)
class Category:
    name: str
    hint: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CategoryRegistry:
    """Immutable view of the category taxonomy."""

    schema_version: int
    categories: tuple[Category, ...]

    # Pre-computed lookup tables. Built once in :meth:`from_dict` and
    # never mutated, so the registry is safe to share across threads.
    _by_canonical_lower: dict[str, str] = field(default_factory=dict, repr=False)
    _by_alias_lower: dict[str, str] = field(default_factory=dict, repr=False)

    # ─── Construction ────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: dict) -> CategoryRegistry:
        """Build a registry from parsed YAML data."""
        version = int(data.get("schema_version", 1))
        cats: list[Category] = []
        for entry in data.get("categories", []):
            name = str(entry["name"]).strip()
            if not name:
                raise ValueError("category entry missing 'name'")
            hint = str(entry.get("hint", "")).strip()
            raw_aliases = entry.get("aliases", []) or []
            aliases = tuple(str(a).strip().lower() for a in raw_aliases if str(a).strip())
            cats.append(Category(name=name, hint=hint, aliases=aliases))

        # Detect duplicates / conflicts up front rather than at lookup
        # time — keeps user-config errors loud and early.
        canonical_lower = {c.name.lower(): c.name for c in cats}
        if len(canonical_lower) != len(cats):
            raise ValueError("duplicate canonical category names in YAML")

        alias_lower: dict[str, str] = {}
        for c in cats:
            # Canonical name is itself an alias of itself.
            alias_lower[c.name.lower()] = c.name
            for a in c.aliases:
                if a in alias_lower and alias_lower[a] != c.name:
                    raise ValueError(
                        f"alias {a!r} maps to both {alias_lower[a]!r} and {c.name!r}"
                    )
                alias_lower[a] = c.name

        if FALLBACK_CATEGORY not in canonical_lower.values():
            raise ValueError(
                f"category YAML must define a canonical {FALLBACK_CATEGORY!r} entry"
            )

        return cls(
            schema_version=version,
            categories=tuple(cats),
            _by_canonical_lower=canonical_lower,
            _by_alias_lower=alias_lower,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> CategoryRegistry:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    # ─── Lookups ─────────────────────────────────────────────────────────
    def canonical_names(self) -> list[str]:
        return [c.name for c in self.categories]

    def resolve(self, label: str | None) -> str | None:
        """Return the canonical name for *label*, or ``None`` if unknown.

        Matching is case-insensitive against canonical names and aliases.
        Whitespace and surrounding punctuation are stripped.
        """
        if not label:
            return None
        cleaned = label.strip().strip("\"'.,;:!?").lower()
        if not cleaned:
            return None
        return self._by_alias_lower.get(cleaned)

    def resolve_or_fallback(self, label: str | None) -> str:
        """Resolve *label* or return :data:`FALLBACK_CATEGORY`."""
        return self.resolve(label) or FALLBACK_CATEGORY

    # ─── Prompt rendering ────────────────────────────────────────────────
    def prompt_block(self) -> str:
        """Compact block listing every canonical category with its hint.

        Format chosen to be cheap on tokens — one line per category,
        canonical name first, hint after a separator. The LLM should
        return one of the canonical names verbatim; aliases on our side
        cover everything else.
        """
        lines: list[str] = ["Allowed categories (use the canonical name on the left):"]
        for c in self.categories:
            if c.hint:
                lines.append(f"  - {c.name}  —  {c.hint}")
            else:
                lines.append(f"  - {c.name}")
        return "\n".join(lines)


# ─── Module-level loader (cached) ───────────────────────────────────────

@lru_cache(maxsize=1)
def _load_default_registry() -> CategoryRegistry:
    return CategoryRegistry.from_yaml(_DEFAULT_DATA_FILE)


def get_registry() -> CategoryRegistry:
    """Return the registry configured for this process.

    Uses the YAML at ``Settings.EXTRACTOR_CATEGORIES_FILE`` when set,
    otherwise the bundled default. Cached per-path so repeated calls
    don't re-read the YAML.
    """
    cfg = get_settings()
    override = cfg.EXTRACTOR_CATEGORIES_FILE
    if not override:
        return _load_default_registry()
    return _load_registry_from_path(override)


@lru_cache(maxsize=8)
def _load_registry_from_path(path: str) -> CategoryRegistry:
    return CategoryRegistry.from_yaml(path)


def reset_registry_cache_for_tests() -> None:
    """Drop cached registries so tests with a fresh override pick up changes."""
    _load_default_registry.cache_clear()
    _load_registry_from_path.cache_clear()
