# Personal Expense Tracker (chat-driven)

A personal project: chat with a bot ("today I spent 40 bucks on food") and have it
silently log the expense into a Google Sheet that mirrors the layout I already use
manually — one row per day, one column per category, daily totals on the right,
column totals at the bottom. The same bot also answers retrieval questions
("how much did I spend on food in April?" / "what did I spend on 24 Apr?").

> **Status:** scaffold only. No logic yet. Each feature will be added in its
> own commit so the build is easy to follow end-to-end.

## Why this exists

Right now I track expenses manually in Google Sheets every month. It works
but it's friction-heavy: open the sheet, find the right cell, type the
number, re-check the totals. I want the friction gone — I want to *talk*
to my tracker like a friend ("dropped 12 on coffee") and have the spreadsheet
update itself.

## High-level architecture

```
┌────────────────┐    text     ┌──────────────────┐   structured    ┌──────────────┐
│  Chat client   │ ──────────► │  LLM extractor   │ ──────────────► │  Sheets      │
│  (CLI first,   │             │  (Groq / Ollama, │  ExpenseEntry   │  writer      │
│  Telegram      │             │  JSON-mode)      │  {date,         │  (gspread)   │
│  later)        │ ◄────────── │                  │   category,     │              │
└────────────────┘   reply     └──────────────────┘   amount, note} └──────────────┘
                                       ▲
                                       │ retrieval query
                                       │
                                ┌──────────────────┐
                                │  Sheets reader   │
                                │  + aggregator    │
                                └──────────────────┘
```

Two flows share the same parser:

- **Logging flow** — extract `{date, category, amount, note}` → append to sheet → confirm.
- **Retrieval flow** — extract `{intent, time_range, category}` → read sheet → reduce → answer.

## Tech choices (tentative)

| Layer | Pick | Rationale |
|---|---|---|
| Language | Python 3.10+ | Best LLM ecosystem, fast iteration. |
| LLM | **Groq** (free tier, OpenAI-compatible API, very fast) with **Ollama** as a 100%-offline fallback | Both free, both can be swapped behind a thin client interface. |
| Schema validation | Pydantic v2 | Forces the LLM into a typed shape; rejects garbage cleanly. |
| Storage | Google Sheets via `gspread` + service account | Mirrors my existing manual layout, so I don't have to migrate years of history. |
| Front-end (eventually) | Telegram bot | Phone-friendly without building an app; webhooks are free. |
| Local dev front-end (now) | CLI | Lets us test parser + sheet writes without any chat infra. |

## Repository layout (will grow)

```
expense-tracker/
├── README.md
├── .gitignore
├── .env.example
├── pyproject.toml
└── src/
    └── expense_tracker/
        ├── __init__.py
        └── __main__.py     # `python -m expense_tracker` — smoke test
```

## Roadmap (one commit per step)

1. ✅ **Scaffold** — empty package, build config, gitignore. *(this commit)*
2. ⬜ **LLM client** — minimal Groq/Ollama wrapper behind a swappable interface.
3. ⬜ **Expense extractor** — Pydantic schema + extraction prompt + tests with synthetic chats.
4. ⬜ **Sheets writer** — `gspread` integration that mirrors my existing monthly layout.
5. ⬜ **CLI** — `expense add "spent 40 on food"` end-to-end.
6. ⬜ **Retrieval extractor** — same extractor pattern but for query intents.
7. ⬜ **Sheets reader + aggregator** — answers "how much did I spend on food in April?".
8. ⬜ **Telegram bot** — wraps the CLI in a chat front-end.
9. ⬜ **Move to personal GitHub** — graduate the project out of this sandbox.

## Running it (today)

Nothing to run yet — only the scaffold smoke test:

```bash
cd expense-tracker
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m expense_tracker
# → "expense_tracker scaffold OK"
```
