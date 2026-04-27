"""CLI entry point — ``python -m expense_tracker`` / ``expense ...``.

Four flavours of commands live here:

* **LLM diagnostics** (Steps 1-3): ``--ping-llm``, ``--extract``.
* **Sheets tools** (Step 4): ``--whoami``, ``--list-sheets``,
  ``--init-transactions``, ``--build-month``, ``--rebuild-month``,
  ``--build-ytd``, ``--rebuild-ytd``, ``--setup-year``.
* **Chat pipeline** (Step 5): ``--chat`` — one full turn, end to end.
  Classifies intent, extracts the typed payload, writes the row to the
  spreadsheet (when intent=log_expense), and prints the bot's reply.
* **Telegram bot** (Step 7): ``--telegram`` — long-poll Telegram and
  route every text message through the same chat pipeline.

All Sheets / chat commands honour ``--fake`` for offline experimentation
— the layout runs end-to-end against an in-memory backend so you can
preview behaviour without touching your real spreadsheet.
"""

from __future__ import annotations

import argparse
import calendar
import sys
from typing import Any, NoReturn

from pydantic import BaseModel

from . import __version__
from .config import Settings, get_settings
from .llm import LLMError, Message, get_llm_client


class _PingResult(BaseModel):
    """Tiny schema used to exercise JSON-mode in ``--ping-llm --json``."""

    greeting: str
    is_alive: bool


# ─── Argparse ──────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="expense",
        description=(
            "Personal expense tracker. LLM smoke tests + chat-driven "
            "Google Sheets logger."
        ),
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"expense_tracker {__version__}",
    )

    # ─── LLM diagnostics ───────────────────────────────────────────────
    g_llm = p.add_argument_group("LLM diagnostics")
    g_llm.add_argument(
        "--ping-llm",
        action="store_true",
        help="Send a tiny prompt to the configured LLM and print the response.",
    )
    g_llm.add_argument(
        "--json",
        action="store_true",
        help="With --ping-llm: force JSON mode and validate the response.",
    )
    g_llm.add_argument(
        "--extract",
        metavar="TEXT",
        help=(
            "Run the full extractor pipeline on TEXT and print the "
            "structured ExtractionResult."
        ),
    )

    # ─── Sheets tools ──────────────────────────────────────────────────
    g_sheets = p.add_argument_group(
        "Google Sheets",
        description=(
            "Inspect the configured spreadsheet and build / rebuild tabs. "
            "Add --fake to any command to run against an in-memory backend "
            "(no network)."
        ),
    )
    g_sheets.add_argument(
        "--fake",
        action="store_true",
        help="Use the in-memory FakeSheetsBackend for any Sheets command.",
    )
    g_sheets.add_argument(
        "--whoami",
        action="store_true",
        help="Show the spreadsheet title, URL, and authorised service-account email.",
    )
    g_sheets.add_argument(
        "--list-sheets",
        action="store_true",
        help="List all tabs in the configured spreadsheet.",
    )
    g_sheets.add_argument(
        "--init-transactions",
        action="store_true",
        help="Create the Transactions master ledger tab if missing.",
    )
    g_sheets.add_argument(
        "--reinit-transactions",
        action="store_true",
        help=(
            "Wipe the existing Transactions tab (if any) and recreate it "
            "with the current schema. Destructive: every row is lost. Use "
            "after a column-layout change."
        ),
    )
    g_sheets.add_argument(
        "--inspect-ledger",
        action="store_true",
        help=(
            "Read the Transactions ledger and report any rows the parser "
            "couldn't interpret (the rows hidden behind 'Skipped: N "
            "unparseable row(s)' on retrieval queries). Prints the row "
            "index, reason, and the offending cell values so you can fix "
            "them in Sheets."
        ),
    )
    g_sheets.add_argument(
        "--build-month",
        metavar="YYYY-MM",
        help="Build one monthly tab (e.g. 2026-04). Refuses if it exists.",
    )
    g_sheets.add_argument(
        "--rebuild-month",
        metavar="YYYY-MM",
        help="Like --build-month but deletes & recreates the tab if it exists.",
    )
    g_sheets.add_argument(
        "--build-ytd",
        metavar="YYYY",
        help="Build the YTD <year> dashboard tab. Refuses if it exists.",
    )
    g_sheets.add_argument(
        "--rebuild-ytd",
        metavar="YYYY",
        help="Like --build-ytd but deletes & recreates the tab if it exists.",
    )
    g_sheets.add_argument(
        "--setup-year",
        metavar="YYYY",
        help="Bulk-build all 12 monthly tabs + YTD for the given year.",
    )
    g_sheets.add_argument(
        "--overwrite",
        action="store_true",
        help="With --setup-year: overwrite tabs that already exist.",
    )
    g_sheets.add_argument(
        "--hide-previous",
        action="store_true",
        help="With --setup-year: hide all monthly tabs from the previous year.",
    )

    # ─── Chat pipeline ─────────────────────────────────────────────────
    g_chat = p.add_argument_group(
        "Chat",
        description=(
            "Drive one full conversation turn end-to-end: classify, "
            "extract, write to Sheets (if log_expense), reply. "
            "Add --fake to log into the in-memory backend instead of "
            "your real spreadsheet."
        ),
    )
    g_chat.add_argument(
        "--chat",
        metavar="TEXT",
        help=(
            "Run TEXT through the full chat pipeline and print the bot's "
            "reply along with a structured trace."
        ),
    )

    # ─── Correction (undo / edit) ───────────────────────────────────────
    g_fix = p.add_argument_group(
        "Correction",
        description=(
            "Edit or remove the most-recently logged expense (the "
            "bottom-most row of the Transactions tab). The relevant "
            "monthly tab is auto-recomputed so the daily grid + "
            "summary stay in sync."
        ),
    )
    g_fix.add_argument(
        "--undo",
        action="store_true",
        help="Delete the bottom-most Transactions row and recompute its month.",
    )
    g_fix.add_argument(
        "--edit-amount",
        metavar="AMOUNT",
        type=float,
        help=(
            "Change the amount of the bottom-most row. Re-runs FX so "
            "Amount (USD) stays consistent."
        ),
    )
    g_fix.add_argument(
        "--edit-category",
        metavar="CATEGORY",
        help=(
            "Change the category of the bottom-most row. Aliases are "
            "resolved (e.g. 'groceries' -> 'Groceries')."
        ),
    )

    # ─── Telegram front-end ────────────────────────────────────────────
    g_tg = p.add_argument_group(
        "Telegram",
        description=(
            "Run a Telegram bot that routes every incoming message "
            "through the chat pipeline. Requires TELEGRAM_BOT_TOKEN + "
            "TELEGRAM_ALLOWED_USERS in .env. Long-polls Telegram, so no "
            "public URL or webhook is needed."
        ),
    )
    g_tg.add_argument(
        "--telegram",
        action="store_true",
        help="Start the Telegram bot (Ctrl-C to stop).",
    )

    return p


# ─── LLM commands ──────────────────────────────────────────────────────

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


# ─── Sheets command helpers ────────────────────────────────────────────

def _open_backend(cfg: Settings, *, fake: bool):
    """Construct a backend, translating typed errors into CLI exits."""
    from .sheets import SheetsError, get_sheets_backend

    try:
        return get_sheets_backend(cfg, fake=fake)
    except SheetsError as exc:
        print(f"\n[sheets config error] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)


def _categories_or_exit() -> list[str]:
    """Load canonical category names from the registry."""
    from .extractor import get_registry

    try:
        return get_registry().canonical_names()
    except Exception as exc:
        print(f"\n[categories error] {exc}", file=sys.stderr)
        sys.exit(2)


def _parse_year_month(value: str, *, label: str) -> tuple[int, int]:
    """Accept ``YYYY-MM`` and return ``(year, month)``."""
    parts = value.strip().split("-")
    if len(parts) != 2:
        print(
            f"\n[arg error] {label} must look like YYYY-MM (e.g. 2026-04), got {value!r}",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        year = int(parts[0])
        month = int(parts[1])
    except ValueError:
        print(f"\n[arg error] {label}: year/month must be integers", file=sys.stderr)
        sys.exit(2)
    if not (1900 <= year <= 2200):
        print(f"\n[arg error] {label}: year out of range: {year}", file=sys.stderr)
        sys.exit(2)
    if not (1 <= month <= 12):
        print(f"\n[arg error] {label}: month must be 1..12, got {month}", file=sys.stderr)
        sys.exit(2)
    return year, month


def _parse_year(value: str, *, label: str) -> int:
    try:
        year = int(value.strip())
    except ValueError:
        print(f"\n[arg error] {label}: year must be an integer", file=sys.stderr)
        sys.exit(2)
    if not (1900 <= year <= 2200):
        print(f"\n[arg error] {label}: year out of range: {year}", file=sys.stderr)
        sys.exit(2)
    return year


# ─── Sheets commands ───────────────────────────────────────────────────

def _cmd_whoami(*, fake: bool) -> int:
    cfg = get_settings()
    backend = _open_backend(cfg, fake=fake)
    print(f"Spreadsheet : {backend.title}")
    url = getattr(backend, "url", None)
    if url:
        print(f"URL         : {url}")
    print(f"Sheet ID    : {backend.spreadsheet_id}")
    email = getattr(backend, "service_account_email", None)
    if email:
        print(f"Robot email : {email}")
    print(f"Backend     : {'fake' if fake else 'gspread'}")
    return 0


def _cmd_list_sheets(*, fake: bool) -> int:
    cfg = get_settings()
    backend = _open_backend(cfg, fake=fake)
    titles = [w.title for w in backend.list_worksheets()]
    print(f"Spreadsheet : {backend.title}")
    print(f"Tabs ({len(titles)}):")
    if not titles:
        print("  (none)")
    for t in titles:
        print(f"  - {t}")
    return 0


def _cmd_init_transactions(*, fake: bool) -> int:
    from .sheets import SheetsError, get_sheet_format, init_transactions_tab

    cfg = get_settings()
    backend = _open_backend(cfg, fake=fake)
    fmt = get_sheet_format()

    try:
        ws = init_transactions_tab(backend, fmt)
    except SheetsError as exc:
        print(f"\n[sheets error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"Transactions tab ready: {ws.title!r} in {backend.title!r}")
    print(f"  rows={ws.row_count}, cols={ws.col_count}")
    return 0


def _cmd_reinit_transactions(*, fake: bool) -> int:
    """Wipe and recreate the Transactions tab. Destructive."""
    from .sheets import SheetsError, get_sheet_format
    from .sheets.transactions import reinit_transactions_tab

    cfg = get_settings()
    backend = _open_backend(cfg, fake=fake)
    fmt = get_sheet_format()
    name = fmt.transactions.sheet_name
    existed = backend.has_worksheet(name)

    print(f"Reinit Transactions tab: {name!r} in {backend.title!r}")
    print(f"  existed before : {existed}")
    if existed and not fake:
        print("  warning        : this wipes every row in the tab.")

    try:
        ws = reinit_transactions_tab(backend, fmt)
    except SheetsError as exc:
        print(f"\n[sheets error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"  rebuilt        : rows={ws.row_count}, cols={ws.col_count}")
    return 0


def _cmd_inspect_ledger(*, fake: bool) -> int:
    """Surface every row of the master ledger that the parser skipped.

    Each retrieval query reports a count like ``Skipped: 1 unparseable
    row(s)``; this command tells you *which* rows so you can clean them
    up in the Sheets UI.
    """
    from .pipeline import RetrievalError, get_retrieval_engine
    from .sheets import SheetsError

    cfg = get_settings()
    backend = _open_backend(cfg, fake=fake)
    engine = get_retrieval_engine(settings=cfg, backend=backend)

    try:
        report = engine.inspect_ledger()
    except (RetrievalError, SheetsError) as exc:
        print(f"\n[sheets error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"Ledger     : {report.sheet_name!r} in {backend.title!r}")
    print(f"Total rows : {report.total_rows}")
    print(f"  parsed   : {len(report.parsed)}")
    print(f"  skipped  : {len(report.skipped)}")

    if not report.skipped:
        print("\nAll rows parsed cleanly.")
        return 0

    print("\nSkipped rows (fix these in the Sheets UI):")
    for s in report.skipped:
        print(f"  row {s.row_index:>4} :: {s.reason}")
        non_empty = [v for v in s.raw_values if v.strip()]
        preview = " | ".join(non_empty[:6])
        if len(non_empty) > 6:
            preview += " ..."
        if preview:
            print(f"            cells: {preview}")
    print(
        "\nTip: open the spreadsheet, jump to the row index above, "
        "and either fix the Date / Amount (USD) cell or delete the row.",
    )
    return 0


def _cmd_build_month(value: str, *, fake: bool, overwrite: bool) -> int:
    from .sheets import SheetsError, build_month_tab, get_sheet_format

    year, month = _parse_year_month(value, label="--build-month")
    cfg = get_settings()
    backend = _open_backend(cfg, fake=fake)
    fmt = get_sheet_format()
    categories = _categories_or_exit()

    print(f"Building monthly tab: {calendar.month_name[month]} {year}")
    print(f"Categories : {len(categories)} ({', '.join(categories)})")
    print(f"Overwrite  : {overwrite}")

    try:
        ws = build_month_tab(
            backend, fmt,
            year=year, month=month,
            categories=categories,
            overwrite=overwrite,
        )
    except SheetsError as exc:
        print(f"\n[sheets error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"\nDone: {ws.title!r}")
    return 0


def _cmd_build_ytd(value: str, *, fake: bool, overwrite: bool) -> int:
    from .sheets import SheetsError, build_ytd_tab, get_sheet_format

    year = _parse_year(value, label="--build-ytd")
    cfg = get_settings()
    backend = _open_backend(cfg, fake=fake)
    fmt = get_sheet_format()
    categories = _categories_or_exit()

    print(f"Building YTD dashboard: {year}")
    print(f"Categories : {len(categories)}")
    print(f"Overwrite  : {overwrite}")

    try:
        ws = build_ytd_tab(
            backend, fmt,
            year=year,
            categories=categories,
            overwrite=overwrite,
        )
    except SheetsError as exc:
        print(f"\n[sheets error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"\nDone: {ws.title!r}")
    return 0


def _cmd_chat(text: str, *, fake: bool) -> int:
    """Drive one full chat turn end-to-end and pretty-print the result."""
    from .pipeline import get_chat_pipeline
    from .sheets import SheetsError

    cfg = get_settings()
    print(f"Provider : {cfg.LLM_PROVIDER}")
    print(f"Timezone : {cfg.TIMEZONE}")
    print(f"Currency : {cfg.DEFAULT_CURRENCY}")
    print(f"Backend  : {'fake' if fake else 'gspread'}")

    if cfg.LLM_TRACE:
        from .storage import get_chat_store
        from .storage.jsonl_store import JsonlChatStore

        store = get_chat_store(cfg)
        if isinstance(store, JsonlChatStore):
            print(f"Traces   : {store.llm_calls_path}")
            print(f"Turns    : {store.conversations_path}")

    print(f"\nMessage  : {text!r}\nThinking...\n")

    try:
        pipeline = get_chat_pipeline(cfg, fake=fake)
    except (LLMError, SheetsError) as exc:
        print(f"\n[config error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        turn = pipeline.chat(text)
    except LLMError as exc:
        print(f"\n[llm error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"Intent     : {turn.intent.value}")
    print(f"Session    : {turn.session_id}")
    print(f"Trace IDs  : {turn.trace_ids}")
    print(f"OK         : {turn.ok}")

    if turn.log_result is not None:
        lr = turn.log_result
        print("\nWrote to Sheets:")
        print(f"  Tab        : {lr.transactions_tab}")
        print(f"  Monthly    : {lr.monthly_tab}"
              f"{'  (newly created)' if lr.monthly_tab_created else ''}")
        print(f"  Category   : {lr.row.category}")
        print(f"  Amount     : {lr.row.amount} {lr.row.currency} -> "
              f"${lr.row.amount_usd:,.2f} USD")
        print(f"  FX         : rate={lr.row.fx_rate} src={lr.fx_source}")
    elif turn.log_error is not None:
        print(f"\nLog error  : {turn.log_error}")
    elif turn.retrieval_answer is not None:
        ra = turn.retrieval_answer
        q = ra.query
        print("\nRead from Sheets:")
        print(f"  Window     : {q.time_range.label} "
              f"({q.time_range.start} -> {q.time_range.end})")
        if q.category:
            print(f"  Category   : {q.category}")
        if q.vendor:
            print(f"  Vendor q   : {q.vendor!r}")
        print(f"  Total USD  : ${ra.total_usd:,.2f}")
        print(f"  Tx count   : {ra.transaction_count}")
        if ra.skipped_rows:
            print(f"  Skipped    : {ra.skipped_rows} unparseable row(s)")
        if ra.by_category:
            top = sorted(ra.by_category.items(), key=lambda kv: -kv[1])[:5]
            print("  By category:")
            for cat, total in top:
                print(f"    - {cat:<14}: ${total:,.2f}")
    elif turn.retrieval_error is not None:
        print(f"\nRetrieval error: {turn.retrieval_error}")
    elif turn.extraction.expense is not None:
        print("\nExpense (not written):")
        print(turn.extraction.expense.model_dump_json(indent=2))
    elif turn.extraction.query is not None:
        print("\nQuery:")
        print(turn.extraction.query.model_dump_json(indent=2))

    print(f"\nBot reply  : {turn.bot_reply}")
    return 0 if turn.ok else 4


def _cmd_setup_year(
    value: str, *, fake: bool, overwrite: bool, hide_previous: bool,
) -> int:
    from .sheets import SheetsError, get_sheet_format, setup_year

    year = _parse_year(value, label="--setup-year")
    cfg = get_settings()
    backend = _open_backend(cfg, fake=fake)
    fmt = get_sheet_format()
    categories = _categories_or_exit()

    print(f"Setting up year: {year}")
    print(f"Categories      : {len(categories)}")
    print(f"Overwrite       : {overwrite}")
    print(f"Hide previous   : {hide_previous}")
    print()

    try:
        report = setup_year(
            backend, fmt,
            year=year,
            categories=categories,
            overwrite=overwrite,
            hide_previous=hide_previous,
        )
    except SheetsError as exc:
        print(f"\n[sheets error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"Created : {len(report.months_created)} monthly tabs")
    for t in report.months_created:
        print(f"  + {t}")
    if report.months_skipped:
        print(f"\nSkipped : {len(report.months_skipped)} (already existed)")
        for t in report.months_skipped:
            print(f"  · {t}")
    print(
        f"\nYTD     : {report.ytd_tab!r} "
        f"({'rebuilt' if report.ytd_overwritten else 'created or kept'})"
    )
    if report.previous_year_hidden:
        print(f"\nHidden previous-year tabs ({len(report.previous_year_hidden)}):")
        for t in report.previous_year_hidden:
            print(f"  - {t}")
    print(f"\nSummary : {report.short_summary()}")
    return 0


# ─── Correction (undo / edit) ──────────────────────────────────────────

def _format_last_row_oneline(snap: Any) -> str:
    """Compact one-line representation of a :class:`LastRow` snapshot.

    Used by ``--undo`` / ``--edit-*`` to echo what was changed without
    dumping the whole row. Defensive about missing fields — older rows
    written under earlier schemas may have shorter ``values`` lists.
    """
    if snap.is_empty:
        return "(empty Transactions tab)"
    date_v = snap.value("date")
    cat_v = snap.value("category")
    amount_v = snap.value("amount")
    currency_v = snap.value("currency")
    return f"{date_v} | {cat_v} | {amount_v} {currency_v} (row {snap.row_index})"


def _cmd_undo(*, fake: bool) -> int:
    from .pipeline import CorrectionError, get_correction_logger
    from .sheets import SheetsError

    cfg = get_settings()
    print(f"Backend  : {'fake' if fake else 'gspread'}")

    try:
        corrector = get_correction_logger(cfg, fake=fake)
    except (LLMError, SheetsError) as exc:
        print(f"\n[config error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        peek = corrector.peek_last()
    except CorrectionError as exc:
        print(f"\n[sheets error] {exc}", file=sys.stderr)
        return 3

    if peek.is_empty:
        print("\nNothing to undo — Transactions tab is empty.")
        return 0

    print(f"\nAbout to delete : {_format_last_row_oneline(peek)}")

    try:
        result = corrector.undo()
    except CorrectionError as exc:
        print(f"\n[undo error] {exc}", file=sys.stderr)
        return 3

    print(f"Deleted         : {_format_last_row_oneline(result.deleted_row)}")
    if result.monthly_tab and result.monthly_tab_recomputed:
        print(f"Recomputed tab  : {result.monthly_tab}")
    elif result.monthly_tab is None:
        print("Recompute       : skipped (no matching monthly tab)")
    return 0


def _cmd_edit(
    *, fake: bool, amount: float | None, category: str | None,
) -> int:
    from .pipeline import CorrectionError, get_correction_logger
    from .sheets import SheetsError

    cfg = get_settings()
    print(f"Backend  : {'fake' if fake else 'gspread'}")

    try:
        corrector = get_correction_logger(cfg, fake=fake)
    except (LLMError, SheetsError) as exc:
        print(f"\n[config error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        result = corrector.edit(amount=amount, category=category)
    except CorrectionError as exc:
        print(f"\n[edit error] {exc}", file=sys.stderr)
        return 3

    if result.before.is_empty:
        print("\nNothing to edit — Transactions tab is empty.")
        return 0

    print(f"\nBefore          : {_format_last_row_oneline(result.before)}")
    if not result.applied:
        print("(no fields applied — nothing to do)")
        return 0
    pretty_updates = ", ".join(
        f"{k}={v!r}" for k, v in result.applied.items()
    )
    print(f"Applied         : {pretty_updates}")
    if result.monthly_tab and result.monthly_tab_recomputed:
        print(f"Recomputed tab  : {result.monthly_tab}")
    elif result.monthly_tab is None:
        print("Recompute       : skipped (no matching monthly tab)")
    return 0


# ─── Telegram bot ──────────────────────────────────────────────────────

def _cmd_telegram(*, fake: bool) -> int:
    """Start the long-polling Telegram bot.

    Blocks until interrupted. Prints a startup banner so the operator
    knows which spreadsheet, allow-list, and provider this run is using
    — surprisingly easy to forget after a few terminals.
    """
    cfg = get_settings()
    print(f"Provider       : {cfg.LLM_PROVIDER}")
    print(f"Timezone       : {cfg.TIMEZONE}")
    print(f"Currency       : {cfg.DEFAULT_CURRENCY}")
    print(f"Backend        : {'fake' if fake else 'gspread'}")

    try:
        from .telegram_app import (
            TelegramConfigError,
            parse_allowed_users,
            run_polling,
        )
    except ImportError as exc:
        print(
            f"\n[telegram error] python-telegram-bot is not installed: {exc}\n"
            "Install with: pip install -e \".[telegram]\"",
            file=sys.stderr,
        )
        return 2

    try:
        allowed = sorted(parse_allowed_users(cfg.TELEGRAM_ALLOWED_USERS))
    except ValueError as exc:
        print(f"\n[telegram config error] {exc}", file=sys.stderr)
        return 2

    print(f"Allowed users  : {allowed if allowed else '<none>'}")
    if not allowed:
        print(
            "  note         : the bot will refuse every message until you "
            "set TELEGRAM_ALLOWED_USERS in .env. DM the bot once and use "
            "/whoami to see your ID.",
        )

    print("\nStarting long-polling. Press Ctrl-C to stop.\n")
    try:
        run_polling(cfg, fake=fake)
    except TelegramConfigError as exc:
        print(f"\n[telegram config error] {exc}", file=sys.stderr)
        return 2
    return 0


# ─── Dispatch ──────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> NoReturn:  # pragma: no cover
    args = _build_parser().parse_args(argv)

    # LLM commands.
    if args.ping_llm:
        sys.exit(_cmd_ping_llm(json_mode=args.json))
    if args.extract is not None:
        sys.exit(_cmd_extract(args.extract))

    # Sheets commands.
    if args.whoami:
        sys.exit(_cmd_whoami(fake=args.fake))
    if args.list_sheets:
        sys.exit(_cmd_list_sheets(fake=args.fake))
    if args.init_transactions:
        sys.exit(_cmd_init_transactions(fake=args.fake))
    if args.reinit_transactions:
        sys.exit(_cmd_reinit_transactions(fake=args.fake))
    if args.inspect_ledger:
        sys.exit(_cmd_inspect_ledger(fake=args.fake))
    if args.build_month is not None:
        sys.exit(_cmd_build_month(args.build_month, fake=args.fake, overwrite=False))
    if args.rebuild_month is not None:
        sys.exit(_cmd_build_month(args.rebuild_month, fake=args.fake, overwrite=True))
    if args.build_ytd is not None:
        sys.exit(_cmd_build_ytd(args.build_ytd, fake=args.fake, overwrite=False))
    if args.rebuild_ytd is not None:
        sys.exit(_cmd_build_ytd(args.rebuild_ytd, fake=args.fake, overwrite=True))
    if args.setup_year is not None:
        sys.exit(_cmd_setup_year(
            args.setup_year,
            fake=args.fake,
            overwrite=args.overwrite,
            hide_previous=args.hide_previous,
        ))

    # Chat pipeline.
    if args.chat is not None:
        sys.exit(_cmd_chat(args.chat, fake=args.fake))

    # Correction (undo / edit).
    if args.undo:
        sys.exit(_cmd_undo(fake=args.fake))
    if args.edit_amount is not None or args.edit_category is not None:
        sys.exit(_cmd_edit(
            fake=args.fake,
            amount=args.edit_amount,
            category=args.edit_category,
        ))

    # Telegram bot.
    if args.telegram:
        sys.exit(_cmd_telegram(fake=args.fake))

    print(f"expense_tracker scaffold OK (v{__version__})")
    print("LLM     : --ping-llm | --extract \"…\"")
    print("Sheets  : --whoami | --list-sheets | --init-transactions | --reinit-transactions")
    print("          --inspect-ledger")
    print("          --build-month YYYY-MM | --rebuild-month YYYY-MM")
    print("          --build-ytd YYYY      | --rebuild-ytd YYYY")
    print("          --setup-year YYYY [--overwrite] [--hide-previous]")
    print("Chat    : --chat \"spent 40 on coffee yesterday\"")
    print("Fix     : --undo | --edit-amount 50 | --edit-category Groceries")
    print("Telegram: --telegram        (run the bot)")
    print("Add --fake to any Sheets / chat / telegram command to run offline.")
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
