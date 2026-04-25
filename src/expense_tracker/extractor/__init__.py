"""Extractor package — chat text in, structured action out.

Public API:

* :class:`Orchestrator` — the high-level entry point.
* :class:`ExtractionResult`, :class:`ExpenseEntry`, :class:`RetrievalQuery`,
  :class:`TimeRange`, :class:`Intent` — the schemas downstream code reads.
* :class:`CategoryRegistry`, :func:`get_registry` — category taxonomy.

Internal modules (intent_classifier, expense_extractor,
retrieval_extractor, prompts) are intentionally not exported — wire
them through :class:`Orchestrator` so the LLM call shape stays one
place.
"""

from .categories import FALLBACK_CATEGORY, CategoryRegistry, get_registry
from .orchestrator import Orchestrator
from .schemas import (
    ExpenseEntry,
    ExtractionResult,
    Intent,
    IntentClassification,
    RetrievalQuery,
    TimeRange,
    is_query_intent,
)

__all__ = [
    "FALLBACK_CATEGORY",
    "CategoryRegistry",
    "ExpenseEntry",
    "ExtractionResult",
    "Intent",
    "IntentClassification",
    "Orchestrator",
    "RetrievalQuery",
    "TimeRange",
    "get_registry",
    "is_query_intent",
]
