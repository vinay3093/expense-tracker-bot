"""In-memory fake LLM client for tests.

Use this anywhere you'd otherwise hit a real provider but don't want a
network call. The fake:

* Returns pre-programmed responses in FIFO order via :meth:`queue_response`.
* Falls back to a configurable ``default_text`` once the queue is empty.
* Records every call so tests can assert on the prompts the code under
  test actually sent.

It implements the :class:`~expense_tracker.llm.base.LLMClient` protocol
exactly, so it's a drop-in replacement.
"""

from __future__ import annotations

from typing import ClassVar

from .base import LLMResponse, Message, T
from .exceptions import LLMBadResponseError


class FakeLLMClient:
    """Programmable fake.

    Example::

        client = FakeLLMClient()
        client.queue_response('{"category": "Food", "amount": 40}')
        parsed, _ = client.complete_json([Message.user("...")], schema=Expense)
        assert parsed.category == "Food"
    """

    provider_name: ClassVar[str] = "fake"
    model: str = "fake-model"

    def __init__(self, *, default_text: str = '{"ok": true}') -> None:
        self._queue: list[str] = []
        self._default_text = default_text
        self._calls: list[list[Message]] = []

    # ─── Test setup helpers ──────────────────────────────────────────────
    def queue_response(self, content: str) -> None:
        """Push one response onto the FIFO queue. Returned by the next call."""
        self._queue.append(content)

    @property
    def calls(self) -> list[list[Message]]:
        """All message lists this client was called with, in order."""
        return list(self._calls)

    def reset(self) -> None:
        """Clear queue and recorded calls (useful between test cases)."""
        self._queue.clear()
        self._calls.clear()

    # ─── Protocol implementation ─────────────────────────────────────────
    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self._calls.append(list(messages))
        text = self._queue.pop(0) if self._queue else self._default_text
        return LLMResponse(
            content=text,
            provider=self.provider_name,
            model=self.model,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=0.0,
        )

    def complete_json(
        self,
        messages: list[Message],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[T, LLMResponse]:
        from ._json_repair import parse_llm_json

        resp = self.complete(messages, temperature=temperature, max_tokens=max_tokens)
        try:
            parsed_dict = parse_llm_json(resp.content)
            parsed = schema.model_validate(parsed_dict)
        except Exception as exc:
            raise LLMBadResponseError(
                f"FakeLLMClient produced unparseable JSON: {resp.content!r}"
            ) from exc
        return parsed, resp
