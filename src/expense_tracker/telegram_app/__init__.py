"""Telegram bot front-end for the expense tracker.

Optional package — only imported when the user runs ``expense --telegram``
or installs the ``[telegram]`` extra. The submodules split cleanly:

* :mod:`auth` — pure-Python allow-list logic (no SDK).
* :mod:`bot` — :class:`MessageProcessor` (pure) + the SDK-facing
  handler factories (thin glue).
* :mod:`factory` — wires :class:`Settings` + :class:`ChatPipeline` into
  a runnable ``telegram.ext.Application``.

Top-level re-exports keep the common imports short.
"""

from .auth import AuthDecision, Authorizer, TelegramAuthError, parse_allowed_users
from .bot import (
    MessageProcessor,
    ProcessedMessage,
    SummaryProcessor,
    make_start_handler,
    make_summary_handler,
    make_text_handler,
    make_whoami_handler,
)
from .factory import (
    TelegramConfigError,
    build_application,
    build_processor,
    build_summary_processor,
    run_polling,
)

__all__ = [
    "AuthDecision",
    "Authorizer",
    "MessageProcessor",
    "ProcessedMessage",
    "SummaryProcessor",
    "TelegramAuthError",
    "TelegramConfigError",
    "build_application",
    "build_processor",
    "build_summary_processor",
    "make_start_handler",
    "make_summary_handler",
    "make_text_handler",
    "make_whoami_handler",
    "parse_allowed_users",
    "run_polling",
]
