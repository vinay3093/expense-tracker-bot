"""Stage-1 intent classification."""

from __future__ import annotations

from dataclasses import dataclass

from ..llm import LLMClient, LLMResponse, Message
from .prompts import INTENT_SYSTEM, build_intent_user_prompt
from .schemas import IntentClassification


@dataclass
class IntentClassifierResult:
    """Pair of (parsed classification, raw LLMResponse).

    Splitting these out instead of returning just the classification
    lets the orchestrator collect ``request_id`` for the trace bundle.
    """

    classification: IntentClassification
    response: LLMResponse


class IntentClassifier:
    """Wraps a single LLM call that decides what the user wants."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def classify(self, user_text: str) -> IntentClassifierResult:
        messages = [
            Message.system(INTENT_SYSTEM),
            Message.user(build_intent_user_prompt(user_text)),
        ]
        parsed, resp = self._llm.complete_json(
            messages=messages, schema=IntentClassification
        )
        return IntentClassifierResult(classification=parsed, response=resp)
