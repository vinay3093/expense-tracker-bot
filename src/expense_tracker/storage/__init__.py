"""Public API of :mod:`expense_tracker.storage`.

External callers should import from here only::

    from expense_tracker.storage import (
        ChatStore, ConversationTurn, LLMCallRecord, get_chat_store,
    )

Concrete classes (e.g. :class:`JsonlChatStore`) are intentionally NOT
re-exported, so application code stays pinned to the protocol.
"""

from .base import (
    SCHEMA_VERSION,
    ChatStore,
    ConversationTurn,
    LLMCallRecord,
)
from .factory import get_chat_store

__all__ = [
    "SCHEMA_VERSION",
    "ChatStore",
    "ConversationTurn",
    "LLMCallRecord",
    "get_chat_store",
]
