"""Build a wired :class:`ChatPipeline` from :class:`Settings`.

Importing this module pulls the LLM, storage, extractor, and Sheets
layers; keep the import path lazy in tests that don't need them.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..extractor.categories import get_registry
from ..extractor.orchestrator import Orchestrator
from ..sheets.backend import SheetsBackend
from ..sheets.currency import CurrencyConverter, get_converter
from ..sheets.factory import get_sheets_backend
from ..sheets.format import SheetFormat, get_sheet_format
from .chat import ChatPipeline
from .logger import ExpenseLogger


def get_chat_pipeline(
    settings: Settings | None = None,
    *,
    fake: bool = False,
    backend: SheetsBackend | None = None,
    sheet_format: SheetFormat | None = None,
    converter: CurrencyConverter | None = None,
    orchestrator: Orchestrator | None = None,
) -> ChatPipeline:
    """Wire a ChatPipeline using settings + the layer factories.

    Every dependency is overridable for tests / advanced callers; pass
    one in to short-circuit its factory call. ``fake=True`` returns an
    in-memory :class:`FakeSheetsBackend` and skips the real network.
    """
    cfg = settings or get_settings()
    fmt = sheet_format or get_sheet_format()
    backend = backend or get_sheets_backend(cfg, fake=fake)
    registry = get_registry()
    converter = converter or get_converter(
        primary_currency=cfg.DEFAULT_CURRENCY,
        log_dir=cfg.LOG_DIR,
        timeout_s=cfg.SHEETS_TIMEOUT_S,
    )
    orchestrator = orchestrator or Orchestrator.from_settings(cfg)

    expense_logger = ExpenseLogger(
        backend=backend,
        sheet_format=fmt,
        registry=registry,
        converter=converter,
        timezone=cfg.TIMEZONE,
        source="chat",
    )

    return ChatPipeline(
        orchestrator=orchestrator,
        expense_logger=expense_logger,
    )


__all__ = ["get_chat_pipeline"]
