"""Typed schemas the extractor pipeline produces and consumes.

Every LLM extraction call validates against one of these. They're the
contract between "free-form chat" and "structured action the bot can
take" — change them carefully, every downstream consumer (Sheets
writer, retrieval reader, Telegram bot) reads these shapes.

Discriminator design
--------------------
We keep :class:`ExpenseEntry` and :class:`RetrievalQuery` as **separate**
schemas rather than a single discriminated-union type. Reasons:

1. Each gets its own focused extraction prompt — empirically more
   reliable on small models like ``llama-3.1-8b-instant``.
2. Schema-mismatch errors point at exactly one field's type.
3. The high-level :class:`ExtractionResult` carries the discriminator
   (``intent``) and *either* the expense *or* the query payload, never
   both — same effect, simpler code paths.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ─── Intent taxonomy ────────────────────────────────────────────────────

class Intent(str, Enum):
    """What the user is *trying to do* with one chat message.

    Stage 1 of the pipeline classifies into one of these. Stage 2's
    schema is chosen by intent; ``smalltalk`` and ``unclear`` skip
    stage 2 entirely.
    """

    LOG_EXPENSE = "log_expense"
    """Record a new expense. e.g. 'spent 40 on food', 'dropped 12 on coffee'."""

    QUERY_PERIOD_TOTAL = "query_period_total"
    """Sum across a time window. e.g. 'how much in April', 'this month total'."""

    QUERY_CATEGORY_TOTAL = "query_category_total"
    """Sum within a category over a time window. e.g. 'how much for food in April'."""

    QUERY_DAY = "query_day"
    """Detail of one specific day. e.g. 'what did I spend on 24 April'."""

    QUERY_RECENT = "query_recent"
    """Last N transactions, no aggregation. e.g. 'show last 5'."""

    SMALLTALK = "smalltalk"
    """'thanks', 'hello', 'good morning' — bot replies without DB action."""

    UNCLEAR = "unclear"
    """Bot couldn't tell what was being asked — caller should re-prompt."""


_QUERY_INTENTS: frozenset[Intent] = frozenset(
    {
        Intent.QUERY_PERIOD_TOTAL,
        Intent.QUERY_CATEGORY_TOTAL,
        Intent.QUERY_DAY,
        Intent.QUERY_RECENT,
    }
)


def is_query_intent(intent: Intent) -> bool:
    """True for any retrieval-flavoured intent."""
    return intent in _QUERY_INTENTS


# ─── Stage-1 output ─────────────────────────────────────────────────────

class IntentClassification(BaseModel):
    """Output of the stage-1 LLM call."""

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(
        default="",
        description=(
            "One short sentence saying why the LLM picked this intent. "
            "Stored alongside the trace so a human reviewer can audit "
            "borderline classifications."
        ),
    )


# ─── Stage-2 outputs ────────────────────────────────────────────────────

class ExpenseEntry(BaseModel):
    """One expense ready to be appended to the spreadsheet.

    Notes on field choices:

    * ``date`` is concrete (``datetime.date``), already resolved from
      relative phrases like 'yesterday' against ``TODAY`` in the prompt.
    * ``amount`` is a positive float — the sign is implicit ("spent").
      Refunds / income become a separate intent later if needed.
    * ``category`` is the *display name* (e.g. ``"Food"``) — the
      :class:`CategoryRegistry` resolves any alias the user typed.
    * ``vendor`` and ``note`` are deliberately separate: vendor is the
      "where" (Trader Joe's), note is the "why" (birthday gift).
    """

    date: date
    category: str
    amount: float = Field(gt=0.0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    vendor: str | None = None
    note: str | None = None

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper()

    @field_validator("category")
    @classmethod
    def _strip_category(cls, v: str) -> str:
        return v.strip()


class TimeRange(BaseModel):
    """Inclusive ``[start, end]`` window of dates the user asked about.

    ``label`` is the human-readable phrase the LLM produced (e.g.
    'April 2026', 'last week'). The bot echoes this back so the user
    can confirm it parsed their phrasing correctly.
    """

    start: date
    end: date
    label: str = Field(min_length=1)

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: date, info) -> date:
        start = info.data.get("start")
        if start is not None and v < start:
            raise ValueError(f"TimeRange end ({v}) is before start ({start})")
        return v


class RetrievalQuery(BaseModel):
    """One retrieval question, fully resolved.

    Carries an explicit ``intent`` field so callers don't need to
    re-classify; the orchestrator copies it from the stage-1 result.
    """

    intent: Intent
    time_range: TimeRange
    category: str | None = Field(
        default=None,
        description="Canonical category name; ``None`` means 'all categories'.",
    )
    vendor: str | None = None
    limit: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="For QUERY_RECENT only — how many recent rows to show.",
    )


# ─── Top-level result ───────────────────────────────────────────────────

class ExtractionResult(BaseModel):
    """What :class:`Orchestrator.extract` returns to its caller.

    Always populated:
    * ``intent``, ``confidence``, ``reasoning`` (from stage 1).
    * ``user_text`` (the original input).
    * ``trace_ids`` (the LLM call records this turn produced).

    Conditionally populated based on intent:
    * ``expense`` for ``LOG_EXPENSE``.
    * ``query`` for any retrieval intent.
    * Both ``None`` for ``SMALLTALK`` / ``UNCLEAR``.

    ``error`` is set when stage-2 extraction failed even though
    classification succeeded — distinct from ``UNCLEAR`` (which is the
    classifier's own "don't know" answer).
    """

    intent: Intent
    confidence: float
    reasoning: str
    user_text: str

    expense: ExpenseEntry | None = None
    query: RetrievalQuery | None = None

    trace_ids: list[str] = Field(default_factory=list)
    session_id: str | None = None

    error: str | None = None
    """Stage-2 failure message, if any. ``None`` on success."""

    def is_actionable(self) -> bool:
        """True if the result yields a concrete action (log or query)."""
        return self.expense is not None or self.query is not None

    def to_turn_payload(self) -> dict[str, Any]:
        """Project to the dict shape used by ``ConversationTurn.extracted``."""
        if self.expense is not None:
            return {"type": "expense", **self.expense.model_dump(mode="json")}
        if self.query is not None:
            return {"type": "query", **self.query.model_dump(mode="json")}
        return {"type": self.intent.value}
