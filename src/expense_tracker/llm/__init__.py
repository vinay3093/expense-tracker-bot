"""Public API of the ``expense_tracker.llm`` package.

Outside of this package, callers should import from here only::

    from expense_tracker.llm import (
        get_llm_client, Message, LLMResponse, LLMClient, LLMError,
    )

Concrete client classes (Groq, Ollama, OpenAI, Anthropic) are intentionally
NOT re-exported — application code should always go through the factory
so that switching providers is just a config change.
"""

from .base import LLMClient, LLMResponse, Message
from .exceptions import (
    LLMBadResponseError,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
)
from .factory import get_llm_client

__all__ = [
    "LLMBadResponseError",
    "LLMClient",
    "LLMConfigError",
    "LLMConnectionError",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMServerError",
    "Message",
    "get_llm_client",
]
