"""Chat → row writer pipeline (Step 5).

Glues the extractor (Step 3) to the Sheets layer (Step 4):

* :class:`ExpenseLogger` — turns one :class:`ExpenseEntry` into one
  appended row in the master ``Transactions`` ledger, with currency
  conversion + lazy monthly-tab provisioning.
* :class:`ChatPipeline` — drives one full user turn end-to-end:
  classify, extract, log (if applicable), reply, persist.
* :func:`get_chat_pipeline` — settings-driven factory used by the CLI.
* :func:`format_reply` — pure function that composes the user-facing
  reply for any extraction outcome.

Public errors:

* :class:`PipelineError` — base class.
* :class:`ExpenseLogError` — the FX / Sheets write chain failed.
* :class:`InconsistentExtractionError` — extractor said
  ``log_expense`` but produced no payload.
"""

from __future__ import annotations

from .chat import ChatPipeline, ChatTurn
from .correction import (
    CorrectionError,
    CorrectionLogger,
    EditResult,
    UndoResult,
)
from .exceptions import (
    ExpenseLogError,
    InconsistentExtractionError,
    PipelineError,
)
from .factory import get_chat_pipeline, get_correction_logger
from .logger import ExpenseLogger, LogResult
from .reply import format_reply

__all__ = [
    "ChatPipeline",
    "ChatTurn",
    "CorrectionError",
    "CorrectionLogger",
    "EditResult",
    "ExpenseLogError",
    "ExpenseLogger",
    "InconsistentExtractionError",
    "LogResult",
    "PipelineError",
    "UndoResult",
    "format_reply",
    "get_chat_pipeline",
    "get_correction_logger",
]
