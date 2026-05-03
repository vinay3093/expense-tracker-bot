"""Wire the Telegram :class:`Application` from settings.

Two factories live here:

* :func:`build_application` — heavy lift; constructs a real
  ``telegram.ext.Application`` connected to BotFather via long-polling
  and registers our text + command handlers. Imports the SDK lazily so
  the rest of the package stays SDK-free.
* :func:`build_processor` — just the :class:`MessageProcessor` (auth +
  pipeline). Used by the SDK factory above and by tests that want to
  exercise message handling without spinning up a network app.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..config import Settings, get_settings
from ..pipeline.chat import ChatPipeline
from ..pipeline.correction import CorrectionLogger
from ..pipeline.factory import get_chat_pipeline
from ..pipeline.summary import SummaryEngine
from .auth import Authorizer, parse_allowed_users
from .bot import (
    CorrectionProcessor,
    MessageProcessor,
    SummaryProcessor,
    make_edit_handler,
    make_last_handler,
    make_start_handler,
    make_summary_handler,
    make_text_handler,
    make_undo_handler,
    make_whoami_handler,
)
from .health_server import maybe_start_health_server

if TYPE_CHECKING:
    from telegram.ext import Application

_log = logging.getLogger(__name__)


class TelegramConfigError(RuntimeError):
    """Raised when the Telegram settings are missing or unusable."""


def build_processor(
    settings: Settings | None = None,
    *,
    pipeline: ChatPipeline | None = None,
    fake: bool = False,
) -> MessageProcessor:
    """Build a :class:`MessageProcessor` from settings.

    ``fake=True`` is forwarded to :func:`get_chat_pipeline`, giving an
    in-memory Sheets backend — handy when smoke-testing the Telegram
    glue without writing to the real spreadsheet.
    """
    cfg = settings or get_settings()
    authorizer = Authorizer(parse_allowed_users(cfg.TELEGRAM_ALLOWED_USERS))
    if authorizer.empty:
        _log.warning(
            "TELEGRAM_ALLOWED_USERS is empty — every message will be refused. "
            "DM the bot once and use /whoami to discover your user ID, then "
            "add it to .env."
        )
    pipeline = pipeline or get_chat_pipeline(cfg, fake=fake)
    return MessageProcessor(authorizer=authorizer, pipeline=pipeline)


def build_correction_processor(
    settings: Settings | None = None,
    *,
    pipeline: ChatPipeline | None = None,
    fake: bool = False,
) -> CorrectionProcessor:
    """Build a :class:`CorrectionProcessor` for /last, /undo, /edit.

    Reuses the chat pipeline's :class:`CorrectionLogger` when given so
    we don't double-construct Sheets clients. Falls back to
    :func:`get_chat_pipeline` to produce one — which guarantees the
    same auth + sheet configuration the rest of the bot uses.
    """
    cfg = settings or get_settings()
    authorizer = Authorizer(parse_allowed_users(cfg.TELEGRAM_ALLOWED_USERS))
    chat_pipeline = pipeline or get_chat_pipeline(cfg, fake=fake)
    corrector: CorrectionLogger | None = chat_pipeline.corrector
    return CorrectionProcessor(authorizer=authorizer, corrector=corrector)


def build_summary_processor(
    settings: Settings | None = None,
    *,
    pipeline: ChatPipeline | None = None,
    engine: SummaryEngine | None = None,
    fake: bool = False,
) -> SummaryProcessor:
    """Build a :class:`SummaryProcessor` for /summary.

    Pulls the :class:`SummaryEngine` from the chat pipeline's existing
    :class:`RetrievalEngine` so all three Telegram processors share
    the same Sheets client, FX cache, and parsing semantics.
    """
    cfg = settings or get_settings()
    authorizer = Authorizer(parse_allowed_users(cfg.TELEGRAM_ALLOWED_USERS))
    if engine is not None:
        return SummaryProcessor(authorizer=authorizer, engine=engine)
    chat_pipeline = pipeline or get_chat_pipeline(cfg, fake=fake)
    retriever = chat_pipeline.retriever
    if retriever is None:
        return SummaryProcessor(authorizer=authorizer, engine=None)
    return SummaryProcessor(
        authorizer=authorizer,
        engine=SummaryEngine(retrieval_engine=retriever),
    )


def build_application(
    settings: Settings | None = None,
    *,
    pipeline: ChatPipeline | None = None,
    fake: bool = False,
) -> Application:
    """Construct a configured ``telegram.ext.Application`` ready to run.

    Caller is responsible for invoking ``.run_polling()`` (or another
    runner). We don't call it here so tests can build the app, inspect
    its handler list, and tear it down without ever touching the
    network.
    """
    cfg = settings or get_settings()

    token_secret = cfg.TELEGRAM_BOT_TOKEN
    if token_secret is None:
        raise TelegramConfigError(
            "TELEGRAM_BOT_TOKEN is not set. Get a token from @BotFather, "
            "put it in .env as TELEGRAM_BOT_TOKEN=..., and try again."
        )
    token = token_secret.get_secret_value().strip()
    if not token:
        raise TelegramConfigError("TELEGRAM_BOT_TOKEN is set but empty.")

    # Late import — the dep is optional (`pip install -e ".[telegram]"`).
    try:
        from telegram.ext import (
            ApplicationBuilder,
            CommandHandler,
            MessageHandler,
            filters,
        )
    except ImportError as exc:  # pragma: no cover — exercised only without dep
        raise TelegramConfigError(
            "python-telegram-bot is not installed. "
            'Run: pip install -e ".[telegram]"'
        ) from exc

    # Build the chat pipeline once and share it across processors so we
    # don't construct two parallel Sheets clients (which would also
    # double up on quota costs and FX cache state).
    chat_pipeline = pipeline or get_chat_pipeline(cfg, fake=fake)
    processor = build_processor(cfg, pipeline=chat_pipeline, fake=fake)
    correction_processor = build_correction_processor(
        cfg, pipeline=chat_pipeline, fake=fake,
    )
    summary_processor = build_summary_processor(
        cfg, pipeline=chat_pipeline, fake=fake,
    )

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", make_start_handler()))
    app.add_handler(CommandHandler("help", make_start_handler()))
    app.add_handler(CommandHandler("whoami", make_whoami_handler()))
    app.add_handler(CommandHandler("last", make_last_handler(correction_processor)))
    app.add_handler(CommandHandler("undo", make_undo_handler(correction_processor)))
    app.add_handler(CommandHandler("edit", make_edit_handler(correction_processor)))
    app.add_handler(CommandHandler("summary", make_summary_handler(summary_processor)))
    # Text messages that aren't commands flow through the chat pipeline.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, make_text_handler(processor))
    )
    return app


def run_polling(
    settings: Settings | None = None,
    *,
    fake: bool = False,
) -> None:
    """Build the application and start long-polling Telegram.

    Blocks until the process is interrupted (Ctrl-C). Long-polling is
    deliberately chosen over webhooks: it works from a laptop / Pi
    without a public URL or TLS cert, and is plenty for personal load.

    When ``TELEGRAM_HEALTH_PORT`` is set, a tiny HTTP health endpoint
    starts on that port in a daemon thread before polling begins.
    Required by hosts (Hugging Face Spaces, Render, Railway, ...) that
    expect every container to expose a listening port; harmless on
    laptop / VM deploys where the env var stays unset.
    """
    cfg = settings or get_settings()
    health = maybe_start_health_server(cfg.TELEGRAM_HEALTH_PORT)
    if health is not None:
        _log.info(
            "Health endpoint enabled on port %d — keep-alive pings + "
            "platform probes will hit GET /.",
            cfg.TELEGRAM_HEALTH_PORT,
        )

    app = build_application(cfg, fake=fake)
    _log.info(
        "Telegram bot starting (long-polling). Allowed users: %s",
        sorted(parse_allowed_users(cfg.TELEGRAM_ALLOWED_USERS))
        or "<none — bot will refuse everyone>",
    )
    # We bypass Application.run_polling() entirely under hosted
    # containers because empirically (Hugging Face Spaces with PTB
    # 21.6 + python:3.11-slim + tini PID 1) it hangs *inside* the
    # initialise() step with no traceback and no PTB log output.
    # `stop_signals=None` alone wasn't enough.  The manual bootstrap
    # below gives us a log line per phase so any future hang is
    # pinpointed instead of hidden behind a single black-box call.
    import asyncio

    async def _serve_forever() -> None:
        _log.info("PTB bootstrap: awaiting Application.initialize() ...")
        await app.initialize()
        _log.info("PTB bootstrap: initialize() complete — calling start() ...")
        await app.start()
        _log.info(
            "PTB bootstrap: start() complete — beginning updater long-poll "
            "(drop_pending_updates=True, allowed=['message']) ..."
        )
        await app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True,
        )
        _log.info(
            "PTB bootstrap: updater is polling Telegram — bot is now LIVE. "
            "DM the bot from any allow-listed Telegram account.",
        )
        # Block the coroutine forever; the platform (HF / Docker /
        # k8s) will SIGTERM us when it wants the container to stop,
        # which propagates through asyncio.run() → KeyboardInterrupt
        # → finally-block in run_polling() below.
        await asyncio.Event().wait()

    try:
        asyncio.run(_serve_forever())
    finally:
        # Best-effort graceful shutdown so we close the HTTPX session
        # and unregister the bot's getUpdates long-poll on the
        # Telegram side.  Wrapped because at SIGTERM the loop may
        # already be torn down — we don't care about errors here.
        _log.info("PTB bootstrap: shutting down updater + application ...")
        try:
            asyncio.run(_graceful_stop(app))
        except Exception as exc:
            _log.warning("PTB shutdown raised %s — container exiting anyway.", exc)


async def _graceful_stop(app: Application) -> None:
    """Try to stop the updater and application cleanly on SIGTERM.

    Each step is wrapped because on a hard kill the underlying asyncio
    loop may have entered a bad state (loop already closed, transport
    half-shut).  We log per phase so partial shutdowns are debuggable.
    """
    if app.updater is not None and app.updater.running:
        try:
            await app.updater.stop()
            _log.info("PTB shutdown: updater stopped.")
        except Exception as exc:
            _log.warning("PTB shutdown: updater.stop() failed: %s", exc)
    if app.running:
        try:
            await app.stop()
            _log.info("PTB shutdown: application stopped.")
        except Exception as exc:
            _log.warning("PTB shutdown: application.stop() failed: %s", exc)
    try:
        await app.shutdown()
        _log.info("PTB shutdown: application.shutdown() complete.")
    except Exception as exc:
        _log.warning("PTB shutdown: application.shutdown() failed: %s", exc)


__all__ = [
    "TelegramConfigError",
    "build_application",
    "build_correction_processor",
    "build_processor",
    "build_summary_processor",
    "run_polling",
]
