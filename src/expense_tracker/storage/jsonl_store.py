"""JSONL-backed implementation of :class:`ChatStore`.

Why JSONL is the right call for a personal-scale bot:

* **Append-only** — one open(append)+write+fsync cycle per record.
  Crash-safe up to the granularity of a single line.
* **Human-readable** — ``cat``, ``rg``, ``jq``, ``pandas.read_json(...,
  lines=True)``, and DuckDB's ``read_json_auto`` all work out of the
  box. No schema migration tool needed.
* **Backup-trivial** — ``cp logs/*.jsonl /elsewhere/``.
* **Schema-versioned** — every line carries ``schema_version``, so the
  reader can refuse / migrate older records cleanly when shapes evolve.

When we eventually need full-text search or analytics under load, we
add a second :class:`ChatStore` impl (SQLite + FTS5, DuckDB view, or a
vector store) and swap the factory — application code never sees the
change.

Concurrency model: one bot instance, multiple threads. We protect
appends with a per-instance ``threading.Lock``. Cross-process writes
are out of scope.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from .base import (
    SCHEMA_VERSION,
    ConversationTurn,
    LLMCallRecord,
)

LLM_CALLS_FILENAME = "llm_calls.jsonl"
CONVERSATIONS_FILENAME = "conversations.jsonl"


class JsonlChatStore:
    """Append-only JSONL implementation of the :class:`ChatStore` protocol.

    Files live under ``log_dir`` and are created on first write. Both
    files are gitignored by the project's top-level ``.gitignore``
    (which covers ``logs/`` and ``*.log``).
    """

    schema_version: int = SCHEMA_VERSION

    def __init__(self, log_dir: str | Path) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._llm_calls_path = self._log_dir / LLM_CALLS_FILENAME
        self._conversations_path = self._log_dir / CONVERSATIONS_FILENAME
        # One lock per file so an LLM trace and a turn write don't block
        # each other unnecessarily (in practice they're written in
        # different threads when the bot is responsive).
        self._llm_lock = threading.Lock()
        self._turn_lock = threading.Lock()

    # ─── Public paths (handy in CLI / docs) ──────────────────────────────
    @property
    def llm_calls_path(self) -> Path:
        return self._llm_calls_path

    @property
    def conversations_path(self) -> Path:
        return self._conversations_path

    # ─── Writers ─────────────────────────────────────────────────────────
    def append_llm_call(self, record: LLMCallRecord) -> None:
        with self._llm_lock:
            self._append(self._llm_calls_path, record.model_dump_json())

    def append_turn(self, turn: ConversationTurn) -> None:
        with self._turn_lock:
            self._append(self._conversations_path, turn.model_dump_json())

    # ─── Readers ─────────────────────────────────────────────────────────
    def iter_llm_calls(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Iterator[LLMCallRecord]:
        for line in self._iter_lines(self._llm_calls_path):
            rec = LLMCallRecord.model_validate_json(line)
            if since is not None and rec.ts < since:
                continue
            if until is not None and rec.ts > until:
                continue
            yield rec

    def iter_turns(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Iterator[ConversationTurn]:
        for line in self._iter_lines(self._conversations_path):
            turn = ConversationTurn.model_validate_json(line)
            if since is not None and turn.ts < since:
                continue
            if until is not None and turn.ts > until:
                continue
            yield turn

    # ─── Internals ───────────────────────────────────────────────────────
    @staticmethod
    def _append(path: Path, line_json: str) -> None:
        """Append one JSON line, fsync, return.

        We open in append-text mode (atomic for writes < PIPE_BUF, which
        any reasonable single line is) and call ``fsync`` so a power
        loss right after this returns can't lose the record.
        """
        # ``json.dumps`` from pydantic doesn't include a newline.
        with open(path, "a", encoding="utf-8") as f:
            f.write(line_json)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:  # pragma: no cover — some FSes (tmpfs) reject fsync
                pass

    @staticmethod
    def _iter_lines(path: Path) -> Iterator[str]:
        """Yield non-empty lines, tolerating a missing file (empty store)."""
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line:
                    yield line
