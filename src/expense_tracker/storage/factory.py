"""Factory for the configured :class:`ChatStore` backend.

Mirrors :mod:`expense_tracker.llm.factory`: one entry point, reads
:class:`Settings`, returns a protocol-typed instance. Today only
``jsonl`` exists. Adding ``sqlite`` / ``duckdb`` later is a one-line
branch here plus a new module — application code never changes.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import ChatStore
from .jsonl_store import JsonlChatStore


def get_chat_store(settings: Settings | None = None) -> ChatStore:
    """Return a fresh chat store for the configured backend."""
    cfg = settings or get_settings()
    backend = cfg.CHAT_STORE_BACKEND

    if backend == "jsonl":
        return JsonlChatStore(log_dir=cfg.LOG_DIR)

    # Defensive — Settings.CHAT_STORE_BACKEND is a Literal so this branch
    # is unreachable in well-typed code.
    raise ValueError(  # pragma: no cover
        f"Unknown CHAT_STORE_BACKEND={backend!r}. Expected one of: jsonl"
    )
