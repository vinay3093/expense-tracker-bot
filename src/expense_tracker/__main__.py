"""CLI entry point — ``python -m expense_tracker`` / ``expense ...``.

Today this is a thin diagnostic tool for the LLM layer. It will grow
into a real "expense add" / "expense query" CLI once Steps 3 and 4 land.

Available commands:

* ``python -m expense_tracker --version``        Show package version.
* ``python -m expense_tracker --ping-llm``       Send a tiny prompt to the
  configured LLM provider and print the response + latency. Best way to
  smoke-test that your ``.env`` is wired correctly.
* ``python -m expense_tracker --ping-llm --json``  Same, but force JSON
  mode and validate the result against a tiny Pydantic schema. Best way
  to verify structured-output reliability of the model you picked.
* ``python -m expense_tracker --extract "spent 40 on coffee yesterday"``
  Run the full extractor pipeline (intent classification + schema-
  specific extraction) and print the resulting :class:`ExtractionResult`.
  Best way to feel out how the bot will read your phrasing.
"""

from __future__ import annotations

import argparse
import sys
from typing import NoReturn

from pydantic import BaseModel

from . import __version__
from .config import get_settings
from .llm import LLMError, Message, get_llm_client


class _PingResult(BaseModel):
    """Tiny schema used to exercise JSON-mode in ``--ping-llm --json``."""

    greeting: str
    is_alive: bool


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="expense",
        description=(
            "Personal expense tracker. Today: scaffold + LLM smoke tests. "
            "Tomorrow: chat-driven Google Sheets logger."
        ),
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"expense_tracker {__version__}",
    )
    p.add_argument(
        "--ping-llm",
        action="store_true",
        help="Send a tiny prompt to the configured LLM and print the response.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="With --ping-llm: force JSON mode and validate the response.",
    )
    p.add_argument(
        "--extract",
        metavar="TEXT",
        help=(
            "Run the full extractor pipeline on TEXT and print the "
            "structured ExtractionResult. Useful for prompt-tuning."
        ),
    )
    return p


def _cmd_ping_llm(json_mode: bool) -> int:
    cfg = get_settings()
    print(f"Provider : {cfg.LLM_PROVIDER}")

    try:
        client = get_llm_client(cfg)
    except LLMError as exc:
        print(f"\n[config error] {exc}", file=sys.stderr)
        return 2

    print(f"Model    : {client.model}")
    print(f"JSON mode: {json_mode}")
    if cfg.LLM_TRACE:
        from .storage import get_chat_store
        from .storage.jsonl_store import JsonlChatStore

        store = get_chat_store(cfg)
        if isinstance(store, JsonlChatStore):
            print(f"Tracing  : {store.llm_calls_path}")
        else:
            print(f"Tracing  : {cfg.CHAT_STORE_BACKEND}")
    print("Sending tiny prompt...\n")

    try:
        if json_mode:
            parsed, resp = client.complete_json(
                messages=[
                    Message.system(
                        "You are a friendly liveness probe. Respond ONLY with "
                        "JSON of the requested shape — never with prose."
                    ),
                    Message.user(
                        "Set greeting to a short hello, set is_alive to true."
                    ),
                ],
                schema=_PingResult,
            )
            print(f"Parsed   : {parsed.model_dump_json()}")
        else:
            resp = client.complete(
                messages=[
                    Message.system("You are a friendly liveness probe."),
                    Message.user("Reply with a one-sentence hello."),
                ],
            )
            print(f"Reply    : {resp.content.strip()}")
    except LLMError as exc:
        print(f"\n[llm error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"Latency  : {resp.latency_ms:.1f} ms")
    if resp.total_tokens is not None:
        print(
            f"Tokens   : prompt={resp.prompt_tokens} "
            f"completion={resp.completion_tokens} "
            f"total={resp.total_tokens}"
        )
    print(f"Request  : {resp.request_id}")
    return 0


def _cmd_extract(text: str) -> int:
    """Run the extractor pipeline on a single message and pretty-print."""
    cfg = get_settings()
    print(f"Provider : {cfg.LLM_PROVIDER}")
    print(f"Timezone : {cfg.TIMEZONE}")
    print(f"Currency : {cfg.DEFAULT_CURRENCY}")

    try:
        from .extractor import Orchestrator

        orch = Orchestrator.from_settings(cfg)
    except LLMError as exc:
        print(f"\n[config error] {exc}", file=sys.stderr)
        return 2

    if cfg.LLM_TRACE:
        from .storage import get_chat_store
        from .storage.jsonl_store import JsonlChatStore

        store = get_chat_store(cfg)
        if isinstance(store, JsonlChatStore):
            print(f"Traces   : {store.llm_calls_path}")
            print(f"Turns    : {store.conversations_path}")

    print(f"\nMessage  : {text!r}\nExtracting...\n")

    try:
        result = orch.extract(text)
    except LLMError as exc:
        print(f"\n[llm error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"Intent     : {result.intent.value}  (confidence={result.confidence:.2f})")
    print(f"Reasoning  : {result.reasoning}")
    print(f"Session    : {result.session_id}")
    print(f"Trace IDs  : {result.trace_ids}")

    if result.expense is not None:
        print("\nExpense:")
        print(result.expense.model_dump_json(indent=2))
    elif result.query is not None:
        print("\nQuery:")
        print(result.query.model_dump_json(indent=2))
    elif result.error is not None:
        print(f"\nError      : {result.error}")
    else:
        print("\n(no actionable payload — smalltalk or unclear)")
    return 0


def main(argv: list[str] | None = None) -> NoReturn:  # pragma: no cover
    args = _build_parser().parse_args(argv)

    if args.ping_llm:
        sys.exit(_cmd_ping_llm(json_mode=args.json))
    if args.extract is not None:
        sys.exit(_cmd_extract(args.extract))

    print(f"expense_tracker scaffold OK (v{__version__})")
    print("Try: `python -m expense_tracker --ping-llm` to test your LLM config.")
    print('  or: `python -m expense_tracker --extract "spent 40 on coffee"`.')
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
