"""Typed exceptions raised by the chat → row writer pipeline.

The pipeline sits *above* the LLM, Sheets, and FX layers, so its errors
either:

* re-export an underlying typed error (e.g. SheetsConfigError surfaced
  from get_sheets_backend), or
* signal a *pipeline-level* problem (e.g. the extractor returned a
  log_expense intent but no ExpenseEntry payload).

Callers — the CLI, the Telegram bot router (Step 7), tests — catch the
base :class:`PipelineError` and surface a friendly message; downstream
typed errors continue to bubble up unchanged.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base class for all chat-pipeline failures."""


class ExpenseLogError(PipelineError):
    """Raised when writing an ExpenseEntry to the spreadsheet fails.

    Wraps a lower-level cause (Sheets API failure, FX lookup failure,
    schema mismatch). The chat front-end converts this into a graceful
    user reply and persists an ``action.status == "error"`` turn so the
    failure stays auditable.
    """

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class InconsistentExtractionError(PipelineError):
    """Stage-1 said ``log_expense`` but stage-2 returned no payload.

    Should be impossible if the extractor is healthy — but the chat
    pipeline guards against it so a malformed LLM response degrades to
    "unclear" rather than crashing the bot.
    """


__all__ = [
    "ExpenseLogError",
    "InconsistentExtractionError",
    "PipelineError",
]
