"""All prompt templates the extractor uses, in one place.

Why a dedicated file:

* **Iteration speed.** Prompt-engineering tweaks are the fastest path
  to better extraction quality. Keeping them out of business-logic
  files makes diffs readable and review easy.
* **Unit-testable.** Each builder is a pure function over its inputs,
  trivially snapshot-testable.
* **One vocabulary.** All prompts share the same date format, anchor
  phrasing, and JSON-only instructions, so the model gets a consistent
  picture of what we want.
"""

from __future__ import annotations

from datetime import date

from .categories import CategoryRegistry

# ─── Stage 1: intent classification ─────────────────────────────────────

INTENT_SYSTEM = """You classify short personal-expense chat messages into one of these intents:

- log_expense          — user is recording a new spend ("spent 40 on food")
- query_period_total   — user asks total over a time window ("total this month")
- query_category_total — user asks total within a category over a window ("food in April")
- query_day            — user asks about ONE specific day ("what did I spend on 24 April")
- query_recent         — user asks for last N transactions ("show last 5")
- smalltalk            — greetings, thanks, chit-chat
- unclear              — you genuinely cannot tell

Be conservative on confidence: if the message could be either log_expense or
a query, prefer the one most users mean by that phrasing. Use 'unclear' only
when you cannot tell at all — never guess.

Respond ONLY with JSON of the requested shape — never with prose, never with
markdown fences."""


def build_intent_user_prompt(text: str) -> str:
    return f"Message: {text!r}\nClassify it."


# ─── Stage 2a: log_expense extraction ───────────────────────────────────

EXPENSE_SYSTEM_TEMPLATE = """You extract one expense from a short personal-expense chat message.

Output a JSON object with these fields:

- date       — YYYY-MM-DD. Resolve relative phrases against TODAY = {today}.
               If the user gives no date, use TODAY.
- category   — pick ONE of the canonical names below verbatim.
- amount     — positive number. Strip currency symbols and unit words.
               '40 bucks' → 40, '1.5k' → 1500, '₹250' → 250.
- currency   — ISO-4217 code (e.g. INR, USD). If the user did not specify,
               use {default_currency}.
- vendor     — the place / merchant if mentioned, else null.
- note       — brief free-text note if the user mentioned a reason, else null.

{categories_block}

Rules:
- Output VALID JSON only. No prose, no markdown fences.
- Numbers must be JSON numbers, not strings.
- If multiple expenses are mentioned in one message, extract the FIRST one
  (a future version will handle multi-extraction)."""


def build_expense_system_prompt(
    *, today: date, default_currency: str, registry: CategoryRegistry
) -> str:
    return EXPENSE_SYSTEM_TEMPLATE.format(
        today=today.isoformat(),
        default_currency=default_currency.upper(),
        categories_block=registry.prompt_block(),
    )


def build_expense_user_prompt(text: str) -> str:
    return f"Message: {text!r}\nExtract the expense."


# ─── Stage 2b: retrieval-query extraction ───────────────────────────────

RETRIEVAL_SYSTEM_TEMPLATE = """You extract a retrieval query from a personal-expense chat message.

Output a JSON object with these fields:

- intent     — one of:
                 query_period_total, query_category_total,
                 query_day, query_recent
               (the same intent passed to you below — copy it verbatim).
- time_range — object with:
                 start  — YYYY-MM-DD inclusive
                 end    — YYYY-MM-DD inclusive
                 label  — short human phrase, e.g. "April 2026", "last week",
                          "this year"
               Resolve relative phrases against TODAY = {today}.
- category   — canonical name from the list below, or null when the user did
               not narrow to a category.
- vendor     — vendor / merchant name, or null.
- limit      — for 'query_recent' only: integer 1..100. For other intents: null.

{categories_block}

INTENT to use: {intent_value}

Rules — general:
- Output VALID JSON only. No prose, no markdown fences.
- Always emit 'time_range.start' and 'time_range.end' as concrete ISO dates,
  and make sure end >= start.
- 'this month' → first..last day of TODAY's month.
- 'last month' → first..last day of the calendar month BEFORE TODAY's month.
- 'last week' → most recent Monday..Sunday strictly before TODAY.
- 'this year' → Jan 1..Dec 31 of TODAY's year.
- A bare month name (e.g. 'April') means TODAY's year unless the user
  specified a year.

Rules — query_recent (read carefully — this is the most common mistake):
- In "last N transactions", "last N expenses", "show me last 5",
  "previous 3", "most recent 4", the number N is a COUNT — set limit=N.
  It is NOT a number of days. NEVER turn N into a date window.
- For query_recent with NO explicit time phrase, span the WHOLE current
  year: time_range.start = YYYY-01-01, time_range.end = TODAY,
  label = "this year".
- Only construct a narrower window when the user actually gives a time
  phrase ("last 5 in April" → limit=5, label="April YYYY",
  start/end = first/last of April YYYY; "last 3 last week" →
  limit=3, label="last week", start/end = most recent Mon..Sun).
- If the user says only "last 5" or "last 5 expenses" or "show me my
  last 5", treat it as count=5, time_range = whole current year.

Worked examples (for query_recent, with TODAY = {today}):
  user: "show me my last 5 expenses"
    -> {{"intent":"query_recent",
        "time_range":{{"start":"<Jan 1 of TODAY's year>",
                      "end":"<TODAY>",
                      "label":"this year"}},
        "category":null, "vendor":null, "limit":5}}

  user: "last 3 in April"
    -> {{"intent":"query_recent",
        "time_range":{{"start":"<Apr 1 of TODAY's year>",
                      "end":"<Apr 30 of TODAY's year>",
                      "label":"April <YYYY>"}},
        "category":null, "vendor":null, "limit":3}}

  user: "previous 10 food expenses"
    -> {{"intent":"query_recent",
        "time_range":{{"start":"<Jan 1 of TODAY's year>",
                      "end":"<TODAY>",
                      "label":"this year"}},
        "category":"Food", "vendor":null, "limit":10}}"""


def build_retrieval_system_prompt(
    *, today: date, intent_value: str, registry: CategoryRegistry
) -> str:
    return RETRIEVAL_SYSTEM_TEMPLATE.format(
        today=today.isoformat(),
        intent_value=intent_value,
        categories_block=registry.prompt_block(),
    )


def build_retrieval_user_prompt(text: str) -> str:
    return f"Message: {text!r}\nExtract the retrieval query."
