"""Chat pipeline — orchestrate one full conversational turn.

Glues the extractor (Step 3) to the Sheets layer (Step 4) and the
retrieval engine (Step 6):

* :class:`ExpenseLogger` — turns one :class:`ExpenseEntry` into one
  appended row in the master ``Transactions`` ledger, with currency
  conversion + lazy monthly-tab provisioning.
* :class:`RetrievalEngine` — reads + filters + aggregates the ledger
  to answer a :class:`RetrievalQuery` (e.g. "how much for food in
  April?").
* :class:`CorrectionLogger` — undo / edit the bottom-most row.
* :class:`ChatPipeline` — drives one full user turn end-to-end:
  classify, extract, log / read (if applicable), reply, persist.
* :func:`get_chat_pipeline` — settings-driven factory used by the CLI.
* :func:`format_reply` — pure function that composes the user-facing
  reply for any extraction outcome.

Public errors:

* :class:`PipelineError` — base class.
* :class:`ExpenseLogError` — the FX / Sheets write chain failed.
* :class:`RetrievalError` — the Sheets read chain failed.
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
from .factory import (
    get_chat_pipeline,
    get_correction_logger,
    get_retrieval_engine,
)
from .logger import ExpenseLogger, LogResult
from .reply import format_reply
from .retrieval import (
    LedgerInspection,
    LedgerRow,
    RetrievalAnswer,
    RetrievalEngine,
    RetrievalError,
    SkippedRow,
)

__all__ = [
    "ChatPipeline",
    "ChatTurn",
    "CorrectionError",
    "CorrectionLogger",
    "EditResult",
    "ExpenseLogError",
    "ExpenseLogger",
    "InconsistentExtractionError",
    "LedgerInspection",
    "LedgerRow",
    "LogResult",
    "PipelineError",
    "RetrievalAnswer",
    "RetrievalEngine",
    "RetrievalError",
    "SkippedRow",
    "UndoResult",
    "format_reply",
    "get_chat_pipeline",
    "get_correction_logger",
    "get_retrieval_engine",
]
