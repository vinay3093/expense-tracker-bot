"""Tests for :class:`SummaryProcessor` — Telegram /summary command.

Mirrors the test style of ``test_telegram_processor`` and
``test_telegram_correction``: pure-Python tests with a stubbed
:class:`SummaryEngine`, no Telegram SDK in scope.

Covers:
* Auth gating — unauthorized users never trigger an engine call.
* Args parsing — empty / week / month / year / aliases / bogus.
* Engine failure — :class:`RetrievalError` becomes a friendly reply.
* Compact reply formatting — output goes through :func:`format_summary`.
* Defensive: when the engine isn't wired the user gets a friendly
  "feature not configured" reply.
"""

from __future__ import annotations

from datetime import date

from expense_tracker.pipeline.retrieval import LedgerRow, RetrievalError
from expense_tracker.pipeline.summary import Summary, SummaryScope
from expense_tracker.telegram_app.auth import Authorizer
from expense_tracker.telegram_app.bot import (
    SummaryProcessor,
    _parse_summary_args,
)

# ─── Test doubles ───────────────────────────────────────────────────────


class _StubEngine:
    """Minimal :class:`SummaryEngine` substitute.

    Records calls so we can assert the engine was (or wasn't) hit, and
    optionally raises a :class:`RetrievalError` to test the failure
    path.
    """

    def __init__(
        self,
        *,
        canned: Summary | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._canned = canned
        self._raises = raises
        self.calls: list[tuple[SummaryScope, date | None]] = []

    def summarize(
        self, scope: SummaryScope, *, today: date | None = None,
    ) -> Summary:
        self.calls.append((scope, today))
        if self._raises is not None:
            raise self._raises
        assert self._canned is not None, "stub needs canned summary"
        return self._canned


def _summary(
    *,
    scope: SummaryScope = SummaryScope.WEEK,
    total: float = 100.0,
    prior_total: float = 50.0,
    transaction_count: int = 3,
) -> Summary:
    return Summary(
        scope=scope,
        today=date(2026, 4, 27),
        period_label="last 7 days",
        period_start=date(2026, 4, 21),
        period_end=date(2026, 4, 27),
        total_usd=total,
        transaction_count=transaction_count,
        by_category={"Food": total},
        by_day={date(2026, 4, 25): total},
        largest=LedgerRow(
            row_index=2,
            date=date(2026, 4, 25),
            day="Sat",
            month="April",
            year=2026,
            category="Food",
            note=None,
            vendor=None,
            amount=total,
            currency="USD",
            amount_usd=total,
            fx_rate=1.0,
            source="chat",
            trace_id=None,
            timestamp=None,
        ),
        skipped_rows=0,
        prior_label="previous 7 days",
        prior_start=date(2026, 4, 14),
        prior_end=date(2026, 4, 20),
        prior_total_usd=prior_total,
        prior_transaction_count=2,
    )


def _processor(
    *,
    allowed_ids: set[int],
    engine: _StubEngine | None,
) -> SummaryProcessor:
    auth = Authorizer(frozenset(allowed_ids))
    return SummaryProcessor(authorizer=auth, engine=engine)  # type: ignore[arg-type]


# ─── Args parsing ──────────────────────────────────────────────────────


def test_empty_args_default_to_week():
    assert _parse_summary_args("") == SummaryScope.WEEK
    assert _parse_summary_args("   ") == SummaryScope.WEEK


def test_explicit_scopes_parse():
    assert _parse_summary_args("week") == SummaryScope.WEEK
    assert _parse_summary_args("month") == SummaryScope.MONTH
    assert _parse_summary_args("year") == SummaryScope.YEAR


def test_scope_aliases_parse():
    """Phone-friendly short forms — easier to type one-handed."""
    assert _parse_summary_args("w") == SummaryScope.WEEK
    assert _parse_summary_args("7d") == SummaryScope.WEEK
    assert _parse_summary_args("monthly") == SummaryScope.MONTH
    assert _parse_summary_args("ytd") == SummaryScope.YEAR
    assert _parse_summary_args("y") == SummaryScope.YEAR


def test_scope_args_are_case_insensitive():
    assert _parse_summary_args("WEEK") == SummaryScope.WEEK
    assert _parse_summary_args(" Month ") == SummaryScope.MONTH


def test_unknown_scope_returns_usage_message():
    out = _parse_summary_args("decade")
    assert isinstance(out, str)
    assert "decade" in out
    assert "Usage" in out


def test_extra_words_after_scope_are_ignored():
    """Forgiving: ``/summary week please`` is still a week summary."""
    assert _parse_summary_args("week please") == SummaryScope.WEEK


# ─── Auth ──────────────────────────────────────────────────────────────


def test_unauthorized_user_does_not_trigger_engine():
    engine = _StubEngine(canned=_summary())
    processor = _processor(allowed_ids={42}, engine=engine)

    reply = processor.process(user_id=7, args_text="week")

    assert engine.calls == []
    assert "not authorized" in reply.lower()


def test_none_user_id_is_denied():
    engine = _StubEngine(canned=_summary())
    processor = _processor(allowed_ids={42}, engine=engine)

    reply = processor.process(user_id=None, args_text="")

    assert engine.calls == []
    assert "authorized" in reply.lower()


# ─── Engine wiring ─────────────────────────────────────────────────────


def test_authorized_default_call_uses_week_scope():
    engine = _StubEngine(canned=_summary(scope=SummaryScope.WEEK, total=212.5))
    processor = _processor(allowed_ids={42}, engine=engine)

    reply = processor.process(user_id=42, args_text="")

    assert len(engine.calls) == 1
    assert engine.calls[0][0] == SummaryScope.WEEK
    # Reply went through format_summary(compact=True) — single paragraph.
    assert "\n" not in reply
    assert "$212.50" in reply
    assert "Last 7 days" in reply


def test_authorized_month_call_passes_month_scope():
    engine = _StubEngine(canned=_summary(scope=SummaryScope.MONTH))
    processor = _processor(allowed_ids={42}, engine=engine)

    processor.process(user_id=42, args_text="month")

    assert engine.calls[0][0] == SummaryScope.MONTH


def test_unconfigured_engine_returns_friendly_message():
    """A processor wired with engine=None (e.g. a dev mode) must reply
    politely instead of crashing."""
    processor = _processor(allowed_ids={42}, engine=None)

    reply = processor.process(user_id=42, args_text="week")

    assert "configured" in reply.lower()
    assert "telegram" in reply.lower()


def test_engine_retrieval_error_becomes_friendly_reply():
    engine = _StubEngine(raises=RetrievalError("read timeout from gspread"))
    processor = _processor(allowed_ids={42}, engine=engine)

    reply = processor.process(user_id=42, args_text="week")

    assert "couldn't read the ledger" in reply.lower()
    assert "read timeout" in reply
    # Must NOT leak Python class names.
    assert "RetrievalError" not in reply


def test_bogus_scope_short_circuits_before_calling_engine():
    """Bad input never burns a Sheets read."""
    engine = _StubEngine(canned=_summary())
    processor = _processor(allowed_ids={42}, engine=engine)

    reply = processor.process(user_id=42, args_text="quarterly")

    assert engine.calls == []
    assert "Usage" in reply
