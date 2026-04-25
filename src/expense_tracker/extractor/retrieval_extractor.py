"""Stage-2b: extract a :class:`RetrievalQuery` from a chat message."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..llm import LLMClient, LLMResponse, Message
from .categories import CategoryRegistry
from .prompts import build_retrieval_system_prompt, build_retrieval_user_prompt
from .schemas import Intent, RetrievalQuery


@dataclass
class RetrievalExtractorResult:
    query: RetrievalQuery
    response: LLMResponse


class RetrievalExtractor:
    """Single LLM call that extracts a retrieval query.

    The intent has already been picked by stage 1 — we pass it down so
    the LLM doesn't second-guess it (and so the same prompt template
    serves all four query variants).
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: CategoryRegistry,
    ) -> None:
        self._llm = llm
        self._registry = registry

    def extract(
        self,
        user_text: str,
        *,
        intent: Intent,
        today: date,
    ) -> RetrievalExtractorResult:
        system_prompt = build_retrieval_system_prompt(
            today=today,
            intent_value=intent.value,
            registry=self._registry,
        )
        messages = [
            Message.system(system_prompt),
            Message.user(build_retrieval_user_prompt(user_text)),
        ]
        raw, resp = self._llm.complete_json(messages=messages, schema=RetrievalQuery)

        # Force-align intent: stage 1 is authoritative. If the LLM
        # rewrote it (which sometimes happens when the user phrasing
        # is ambiguous), we override here so downstream dispatch is
        # consistent with the classifier's decision.
        if raw.intent != intent:
            raw = raw.model_copy(update={"intent": intent})

        # Normalise category — same defence-in-depth as the expense path.
        if raw.category is not None:
            canonical = self._registry.resolve(raw.category)
            if canonical is None:
                # Couldn't resolve — drop it rather than write 'Other',
                # since 'no category filter' is more useful than
                # silently filtering on a fallback bucket.
                raw = raw.model_copy(update={"category": None})
            elif canonical != raw.category:
                raw = raw.model_copy(update={"category": canonical})

        return RetrievalExtractorResult(query=raw, response=resp)
