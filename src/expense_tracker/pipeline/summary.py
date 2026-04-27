"""Period summaries — "how am I doing this week / this month / this year".

Step 8a layer on top of :class:`RetrievalEngine`. A *summary* is a
focused report a human wants to read at a glance (CLI tail, Telegram
DM, future scheduled push):

* The focal window (rolling 7 days, current month-to-date, current
  year-to-date) with total, count, top categories, biggest single
  spend.
* An apples-to-apples comparison against the equivalent prior window.

Why "apples to apples": comparing today's incomplete month against
*last full month* always shows you under-spending and is meaningless.
Comparing month-to-day-N against last month's day-1-through-N actually
tells you whether you're on or off pace.

Implementation note: this module deliberately reuses
:class:`RetrievalEngine.answer` rather than reimplementing aggregation
— so every parsing fix (e.g. locale-tolerant numbers, Step 6.1) and
every future ledger-side improvement automatically applies to
summaries too.

The module is intent-free: it does not consume :class:`ExtractionResult`
or speak to the LLM. The chat pipeline / Telegram / CLI all call
:meth:`SummaryEngine.summarize` directly.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from ..extractor.schemas import Intent, RetrievalQuery, TimeRange
from .retrieval import LedgerRow, RetrievalEngine, RetrievalError


class SummaryScope(str, Enum):
    """Which period the user is asking about.

    ``str``-valued so callers can use the literal strings (``"week"``)
    in argparse / Telegram-arg parsing without an enum-import dance.
    """

    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


@dataclass(frozen=True)
class Summary:
    """Typed summary covering one focal window + one prior window.

    Both windows are independently aggregated by :class:`RetrievalEngine`
    so they share the same parsing semantics. ``delta_usd`` and
    ``delta_pct`` are derived properties to keep the dataclass minimal.
    """

    scope: SummaryScope
    today: date
    """The anchor date used to build the windows. Lets us round-trip
    deterministic tests."""

    # Focal window
    period_label: str
    period_start: date
    period_end: date
    total_usd: float
    transaction_count: int
    by_category: dict[str, float]
    by_day: dict[date, float]
    largest: LedgerRow | None
    skipped_rows: int
    """Rows the parser skipped while reading the focal window. Surface
    in the reply so the operator knows their data is dirty."""

    # Prior window (same scope, shifted back)
    prior_label: str
    prior_start: date
    prior_end: date
    prior_total_usd: float
    prior_transaction_count: int

    @property
    def delta_usd(self) -> float:
        return round(self.total_usd - self.prior_total_usd, 2)

    @property
    def delta_pct(self) -> float | None:
        """``None`` when prior was exactly zero (division would be ∞)."""
        if self.prior_total_usd == 0:
            return None
        return round(
            (self.total_usd - self.prior_total_usd) / self.prior_total_usd * 100.0,
            1,
        )

    def top_categories(self, n: int = 3) -> list[tuple[str, float]]:
        """Top ``n`` (category, total) pairs by spend, descending."""
        items = sorted(self.by_category.items(), key=lambda kv: -kv[1])
        return items[:n]


class SummaryEngine:
    """Build a :class:`Summary` for one of three scopes.

    Stateless across calls; safe to construct once per process and
    reuse. Wraps a :class:`RetrievalEngine` for the actual reads.
    """

    def __init__(
        self,
        *,
        retrieval_engine: RetrievalEngine,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        self._retriever = retrieval_engine
        self._today_provider = today_provider or date.today

    def summarize(
        self, scope: SummaryScope, *, today: date | None = None,
    ) -> Summary:
        """Read both windows, return a :class:`Summary`.

        Args:
            scope: which period (week / month / year) to summarise.
            today: anchor date; defaults to ``today_provider()``. Tests
                inject a fixed anchor for determinism.

        Raises:
            RetrievalError: any underlying ledger-read failure.
        """
        anchor = today or self._today_provider()
        cur_label, cur_start, cur_end = _build_current_window(scope, anchor)
        prior_label, prior_start, prior_end = _build_prior_window(scope, anchor)

        cur = self._retriever.answer(
            _period_query(cur_label, cur_start, cur_end),
        )
        prior = self._retriever.answer(
            _period_query(prior_label, prior_start, prior_end),
        )

        return Summary(
            scope=scope,
            today=anchor,
            period_label=cur_label,
            period_start=cur_start,
            period_end=cur_end,
            total_usd=cur.total_usd,
            transaction_count=cur.transaction_count,
            by_category=cur.by_category,
            by_day=cur.by_day,
            largest=cur.largest,
            skipped_rows=cur.skipped_rows,
            prior_label=prior_label,
            prior_start=prior_start,
            prior_end=prior_end,
            prior_total_usd=prior.total_usd,
            prior_transaction_count=prior.transaction_count,
        )


# ─── Window math ────────────────────────────────────────────────────────


def _build_current_window(
    scope: SummaryScope, today: date,
) -> tuple[str, date, date]:
    """The focal window for *today* under *scope*.

    Conventions:
      * **week**  → rolling 7 days ending today (inclusive both ends).
      * **month** → 1st of *today*'s calendar month through today.
      * **year**  → Jan 1 of *today*'s year through today.
    """
    if scope == SummaryScope.WEEK:
        start = today - timedelta(days=6)
        return ("last 7 days", start, today)
    if scope == SummaryScope.MONTH:
        start = today.replace(day=1)
        return (f"{today.strftime('%B %Y')} so far", start, today)
    if scope == SummaryScope.YEAR:
        start = date(today.year, 1, 1)
        return (f"{today.year} so far", start, today)
    raise ValueError(f"unknown scope: {scope!r}")  # pragma: no cover


def _build_prior_window(
    scope: SummaryScope, today: date,
) -> tuple[str, date, date]:
    """An apples-to-apples prior window that's the same length as the
    focal one, but shifted back by exactly one period.

    Edge cases:
      * Month: if today is Mar 31 and prior month is Feb, cap to the
        last valid day of Feb. Same trick for year-over-year.
      * Year: a Feb 29 anchor in a leap year falls back to Feb 28 of
        the previous year.
    """
    if scope == SummaryScope.WEEK:
        prior_end = today - timedelta(days=7)
        prior_start = prior_end - timedelta(days=6)
        return ("previous 7 days", prior_start, prior_end)
    if scope == SummaryScope.MONTH:
        first_of_current = today.replace(day=1)
        last_of_prior = first_of_current - timedelta(days=1)
        first_of_prior = last_of_prior.replace(day=1)
        target_day = min(today.day, last_of_prior.day)
        prior_end = first_of_prior.replace(day=target_day)
        label = (
            f"{first_of_prior.strftime('%B %Y')} thru day {target_day}"
        )
        return (label, first_of_prior, prior_end)
    if scope == SummaryScope.YEAR:
        prev_year = today.year - 1
        prior_start = date(prev_year, 1, 1)
        last_day_target = calendar.monthrange(prev_year, today.month)[1]
        target_day = min(today.day, last_day_target)
        prior_end = date(prev_year, today.month, target_day)
        label = f"{prev_year} thru {prior_end.strftime('%b')} {prior_end.day}"
        return (label, prior_start, prior_end)
    raise ValueError(f"unknown scope: {scope!r}")  # pragma: no cover


def _period_query(label: str, start: date, end: date) -> RetrievalQuery:
    """Wrap a window in a :class:`RetrievalQuery` so we can hand it to
    :meth:`RetrievalEngine.answer`."""
    return RetrievalQuery(
        intent=Intent.QUERY_PERIOD_TOTAL,
        time_range=TimeRange(start=start, end=end, label=label),
    )


# ─── Reply formatting ───────────────────────────────────────────────────


def format_summary(summary: Summary, *, compact: bool = False) -> str:
    """Render *summary* as a human-readable, newline-formatted string.

    The CLI prints the multi-line form (``compact=False``); Telegram
    prefers a single-paragraph compact form so it fits on a phone
    screen without scrolling.
    """
    if compact:
        return _format_compact(summary)
    return _format_verbose(summary)


def _format_compact(s: Summary) -> str:
    """Single-paragraph form for Telegram."""
    head = (
        f"{s.period_label.capitalize()} "
        f"({s.period_start.strftime('%a %d %b')} - "
        f"{s.period_end.strftime('%a %d %b')}): "
        f"${s.total_usd:,.2f} across "
        f"{_pluralize_expense(s.transaction_count)}."
    )
    parts = [head]

    top = s.top_categories(n=3)
    if top:
        parts.append(
            "Top: " + ", ".join(f"{c} ${v:,.2f}" for c, v in top) + ".",
        )

    if s.largest is not None:
        parts.append(_largest_clause(s.largest))

    parts.append(_delta_clause(s))

    if s.skipped_rows:
        parts.append(
            f"({s.skipped_rows} ledger row(s) skipped — "
            "run --inspect-ledger to see them.)",
        )

    return " ".join(parts)


def _format_verbose(s: Summary) -> str:
    """Multi-line form for the CLI."""
    lines: list[str] = []
    lines.append(f"Summary ({s.scope.value}, anchor {s.today.isoformat()})")
    lines.append(
        f"  Period      : {s.period_label} "
        f"({s.period_start.isoformat()} -> {s.period_end.isoformat()})",
    )
    lines.append(
        f"  Total       : ${s.total_usd:,.2f} across "
        f"{_pluralize_expense(s.transaction_count)}",
    )
    top = s.top_categories(n=3)
    if top and s.total_usd > 0:
        rendered = []
        for cat, val in top:
            pct = val / s.total_usd * 100 if s.total_usd else 0
            rendered.append(f"{cat} ${val:,.2f} ({pct:.0f}%)")
        lines.append("  Top 3       : " + ", ".join(rendered))
    if s.largest is not None:
        lines.append(
            "  Biggest     : "
            f"${s.largest.amount_usd:,.2f} "
            f"{s.largest.category} on "
            f"{s.largest.date.strftime('%a %d %b')}"
            + (f" ({s.largest.vendor})" if s.largest.vendor else ""),
        )
    if s.transaction_count > 0:
        days_with = sum(1 for v in s.by_day.values() if v > 0)
        days_total = (s.period_end - s.period_start).days + 1
        lines.append(
            f"  Days active : {days_with}/{days_total}",
        )
    if s.skipped_rows:
        lines.append(
            f"  Skipped     : {s.skipped_rows} unparseable row(s) "
            "(run --inspect-ledger)",
        )
    lines.append("")
    lines.append(
        f"  vs {s.prior_label} "
        f"({s.prior_start.isoformat()} -> {s.prior_end.isoformat()}):",
    )
    lines.append(f"    Prior     : ${s.prior_total_usd:,.2f}")
    lines.append("    " + _delta_clause(s, indent=True))
    return "\n".join(lines)


def _delta_clause(s: Summary, *, indent: bool = False) -> str:
    """Render the comparison delta in its compact one-line form."""
    head = "Delta" if indent else "vs prior"
    if s.prior_total_usd == 0 and s.total_usd == 0:
        return f"{head}: no spending in either window."
    if s.prior_total_usd == 0:
        return (
            f"{head}: $0.00 -> ${s.total_usd:,.2f} "
            f"(+${s.total_usd:,.2f}, prior was zero)."
        )
    sign = "+" if s.delta_usd >= 0 else "-"
    pct_str = f"{s.delta_pct:+.1f}%" if s.delta_pct is not None else "n/a"
    return (
        f"{head}: ${s.prior_total_usd:,.2f} -> ${s.total_usd:,.2f} "
        f"({sign}${abs(s.delta_usd):,.2f}, {pct_str})."
    )


def _largest_clause(row: LedgerRow) -> str:
    """Mirror :func:`pipeline.reply._largest_clause` but standalone so
    summary doesn't import the chat-reply module."""
    base = (
        f"Largest: ${row.amount_usd:,.2f} "
        f"{row.category} on {row.date.strftime('%a %d %b')}"
    )
    extras: list[str] = []
    if row.vendor:
        extras.append(row.vendor)
    if row.note:
        extras.append(row.note)
    if extras:
        base += " (" + ": ".join(extras) + ")"
    base += "."
    return base


def _pluralize_expense(count: int) -> str:
    """``"1 expense"`` / ``"N expenses"`` — matches the chat-reply tone."""
    return f"{count} expense" + ("" if count == 1 else "s")


__all__ = [
    "RetrievalError",  # re-exported so callers can catch one type
    "Summary",
    "SummaryEngine",
    "SummaryScope",
    "format_summary",
]
