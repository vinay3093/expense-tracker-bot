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
from ..pipeline.factory import get_chat_pipeline
from .auth import Authorizer, parse_allowed_users
from .bot import (
    MessageProcessor,
    make_start_handler,
    make_text_handler,
    make_whoami_handler,
)

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

    processor = build_processor(cfg, pipeline=pipeline, fake=fake)

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", make_start_handler()))
    app.add_handler(CommandHandler("help", make_start_handler()))
    app.add_handler(CommandHandler("whoami", make_whoami_handler()))
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
    """
    app = build_application(settings, fake=fake)
    _log.info(
        "Telegram bot starting (long-polling). Allowed users: %s",
        sorted(parse_allowed_users((settings or get_settings()).TELEGRAM_ALLOWED_USERS))
        or "<none — bot will refuse everyone>",
    )
    app.run_polling(allowed_updates=["message"])


__all__ = [
    "TelegramConfigError",
    "build_application",
    "build_processor",
    "run_polling",
]
