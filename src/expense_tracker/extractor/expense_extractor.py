"""Stage-2a: extract a single :class:`ExpenseEntry` from a chat message."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..llm import LLMClient, LLMResponse, Message
from .categories import CategoryRegistry
from .prompts import build_expense_system_prompt, build_expense_user_prompt
from .schemas import ExpenseEntry


@dataclass
class ExpenseExtractorResult:
    expense: ExpenseEntry
    response: LLMResponse


class ExpenseExtractor:
    """Single LLM call that extracts an expense already known to be one.

    Categories returned by the LLM go through
    :meth:`CategoryRegistry.resolve` so user-typed synonyms collapse to
    canonical names. If the LLM emits a category we can't resolve at
    all, we fall back to :attr:`CategoryRegistry.fallback_category`
    rather than failing the whole turn — the user can correct it on
    the next message.
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: CategoryRegistry,
        default_currency: str,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._default_currency = default_currency.upper()

    def extract(self, user_text: str, *, today: date) -> ExpenseExtractorResult:
        system_prompt = build_expense_system_prompt(
            today=today,
            default_currency=self._default_currency,
            registry=self._registry,
        )
        messages = [
            Message.system(system_prompt),
            Message.user(build_expense_user_prompt(user_text)),
        ]
        raw, resp = self._llm.complete_json(messages=messages, schema=ExpenseEntry)

        # Normalise category to a canonical name. The LLM has been told
        # to pick from the canonical list, but small models occasionally
        # spell or capitalise differently — defence in depth.
        canonical = self._registry.resolve_or_fallback(raw.category)
        if canonical != raw.category:
            raw = raw.model_copy(update={"category": canonical})
        return ExpenseExtractorResult(expense=raw, response=resp)
