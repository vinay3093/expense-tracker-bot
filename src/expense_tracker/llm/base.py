"""Provider-agnostic LLM interface.

Every concrete provider (Groq, Ollama, OpenAI, Anthropic, fake) implements
the :class:`LLMClient` protocol below. The rest of the application talks
*only* to this protocol — never to a specific provider's SDK. Switching
providers is therefore a single env-var change and zero code changes
upstream.

Two methods exist deliberately:

* :meth:`LLMClient.complete`        — free-form text out, for one-off chat.
* :meth:`LLMClient.complete_json`   — structured Pydantic model out, for
                                       expense extraction / intent routing.

Both return a :class:`LLMResponse` envelope alongside (or instead of) the
parsed result, so callers always have access to token counts and latency
for observability.
"""

from __future__ import annotations

import uuid
from typing import ClassVar, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant"]


class Message(BaseModel):
    """One conversational turn, provider-agnostic.

    Convenience constructors (:meth:`system`, :meth:`user`, :meth:`assistant`)
    keep call sites short and readable::

        msgs = [
            Message.system("You are a helpful expense extractor."),
            Message.user("I spent 40 bucks on food today"),
        ]
    """

    role: Role
    content: str

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls(role="assistant", content=content)


class LLMResponse(BaseModel):
    """Provider-agnostic envelope for one LLM completion.

    ``content`` is the raw text the model produced. Token-count fields are
    best-effort: not every provider returns them, so they may be ``None``
    (e.g. some Ollama versions). ``latency_ms`` is wall-clock time for the
    request itself, measured by the client.
    """

    content: str
    provider: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: float
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])


T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class LLMClient(Protocol):
    """Protocol every concrete provider client implements.

    ``runtime_checkable`` lets tests and the factory verify protocol
    conformance with ``isinstance(x, LLMClient)`` if they want to.
    """

    provider_name: ClassVar[str]
    """Short stable identifier, e.g. 'groq', 'ollama', 'openai'."""

    model: str
    """The exact model id this client is calling."""

    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Free-form text completion. Returns the raw text response."""
        ...

    def complete_json(
        self,
        messages: list[Message],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[T, LLMResponse]:
        """Structured completion typed by a Pydantic model.

        Implementations must:

        1. Force the underlying model into JSON mode (provider-specific —
           ``response_format={"type": "json_object"}`` for OpenAI/Groq,
           ``format="json"`` for Ollama, etc.).
        2. Inject the JSON schema into the system prompt for grounding,
           because most JSON modes only enforce *validity*, not *shape*.
        3. Validate the parsed JSON against ``schema``. On validation
           failure they MUST raise
           :class:`~expense_tracker.llm.exceptions.LLMBadResponseError`
           so the caller can decide whether to re-prompt.

        Returns a ``(parsed_model, raw_response)`` tuple so callers have
        both the typed payload AND the observability metadata.
        """
        ...
