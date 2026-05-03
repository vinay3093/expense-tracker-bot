"""Build a wired :class:`ChatPipeline` from :class:`Settings`.

Every public factory here returns one of the high-level pipeline
objects (``ChatPipeline``, ``ExpenseLogger``, ``CorrectionLogger``,
``RetrievalEngine``, ``SummaryEngine``) wired to the
:class:`LedgerBackend` selected by ``settings.STORAGE_BACKEND``.

Importing this module pulls the LLM, storage, extractor, and ledger
adapter layers; keep the import path lazy in tests that don't need
them.

Why one factory module, not five
--------------------------------
Wiring is the messiest part of the system.  Concentrating it here
means:

* The "which arguments does ``ExpenseLogger`` take?" knowledge lives
  in exactly one place.
* Adding a new dependency (e.g. an audit logger, a metrics exporter)
  is a single edit, not a hunt across CLI + Telegram + tests.
* Tests can mock the high-level outputs without re-implementing the
  wiring graph.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..extractor.categories import get_registry
from ..extractor.orchestrator import Orchestrator
from ..ledger.base import LedgerBackend
from ..ledger.factory import get_ledger_backend
from ..ledger.sheets.currency import CurrencyConverter, get_converter
from .chat import ChatPipeline
from .correction import CorrectionLogger
from .logger import ExpenseLogger
from .retrieval import RetrievalEngine
from .summary import SummaryEngine


def get_chat_pipeline(
    settings: Settings | None = None,
    *,
    fake: bool = False,
    ledger: LedgerBackend | None = None,
    converter: CurrencyConverter | None = None,
    orchestrator: Orchestrator | None = None,
) -> ChatPipeline:
    """Wire a :class:`ChatPipeline` using settings + the layer factories.

    Every dependency is overridable for tests / advanced callers.
    ``fake=True`` returns an in-memory fake for the active edition
    (Sheets fake worksheet for the Sheets edition; SQLite-in-memory
    is the test analogue for the Postgres edition).
    """
    cfg = settings or get_settings()
    ledger = ledger or get_ledger_backend(cfg, fake=fake)
    registry = get_registry()
    converter = converter or get_converter(
        primary_currency=cfg.DEFAULT_CURRENCY,
        log_dir=cfg.LOG_DIR,
        timeout_s=cfg.SHEETS_TIMEOUT_S,
    )
    orchestrator = orchestrator or Orchestrator.from_settings(cfg)

    expense_logger = ExpenseLogger(
        ledger=ledger,
        registry=registry,
        converter=converter,
        timezone=cfg.TIMEZONE,
        source="chat",
    )

    correction_logger = CorrectionLogger(
        ledger=ledger,
        registry=registry,
        converter=converter,
    )

    retrieval_engine = RetrievalEngine(
        ledger=ledger,
        registry=registry,
    )

    return ChatPipeline(
        orchestrator=orchestrator,
        expense_logger=expense_logger,
        retrieval_engine=retrieval_engine,
        correction_logger=correction_logger,
    )


def get_retrieval_engine(
    settings: Settings | None = None,
    *,
    fake: bool = False,
    ledger: LedgerBackend | None = None,
) -> RetrievalEngine:
    """Wire a standalone :class:`RetrievalEngine` for ad-hoc CLI use.

    The engine never invokes the LLM; it just reads + filters +
    aggregates the master ledger, so no orchestrator / converter is
    needed.
    """
    cfg = settings or get_settings()
    ledger = ledger or get_ledger_backend(cfg, fake=fake)
    registry = get_registry()
    return RetrievalEngine(
        ledger=ledger,
        registry=registry,
    )


def get_summary_engine(
    settings: Settings | None = None,
    *,
    fake: bool = False,
    ledger: LedgerBackend | None = None,
    retrieval_engine: RetrievalEngine | None = None,
) -> SummaryEngine:
    """Wire a standalone :class:`SummaryEngine` for ``--summary`` /
    ``/summary`` callers.

    Builds a :class:`RetrievalEngine` under the hood (or accepts an
    existing one) so the focal-window + prior-window reads share the
    same ledger parsing semantics.
    """
    cfg = settings or get_settings()
    ledger = ledger or get_ledger_backend(cfg, fake=fake)
    retrieval_engine = retrieval_engine or get_retrieval_engine(
        cfg, fake=fake, ledger=ledger,
    )
    return SummaryEngine(retrieval_engine=retrieval_engine)


def get_correction_logger(
    settings: Settings | None = None,
    *,
    fake: bool = False,
    ledger: LedgerBackend | None = None,
    converter: CurrencyConverter | None = None,
) -> CorrectionLogger:
    """Wire a standalone :class:`CorrectionLogger` for ad-hoc CLI use."""
    cfg = settings or get_settings()
    ledger = ledger or get_ledger_backend(cfg, fake=fake)
    registry = get_registry()
    converter = converter or get_converter(
        primary_currency=cfg.DEFAULT_CURRENCY,
        log_dir=cfg.LOG_DIR,
        timeout_s=cfg.SHEETS_TIMEOUT_S,
    )
    return CorrectionLogger(
        ledger=ledger,
        registry=registry,
        converter=converter,
    )


__all__ = [
    "get_chat_pipeline",
    "get_correction_logger",
    "get_retrieval_engine",
    "get_summary_engine",
]
