# Personal Expense Tracker (chat-driven)

A personal project: chat with a bot ("today I spent 40 bucks on food") and have it
silently log the expense into a Google Sheet that mirrors the layout I already use
manually — one row per day, one column per category, daily totals on the right,
column totals at the bottom. The same bot also answers retrieval questions
("how much did I spend on food in April?" / "what did I spend on 24 Apr?").

> **Status:** Step 2 + 2.5 of 9 complete — provider-agnostic LLM client
> is in, plus a chat-history & LLM-call tracing layer.
> No Google Sheets wiring yet.

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

## Tech choices

| Layer | Pick | Rationale |
|---|---|---|
| Language | Python 3.10+ | Best LLM ecosystem, fast iteration. |
| LLM | **Groq** (free tier, OpenAI-compatible API, very fast) by default, with **Ollama** as a 100%-offline fallback and OpenAI/Anthropic available behind an opt-in extras install. | One protocol, four backends — switch with a single env var. |
| Schema validation | Pydantic v2 | Forces the LLM into a typed shape; rejects garbage cleanly. |
| Settings | `pydantic-settings` | All config comes from `.env` / env vars, all keys are `SecretStr`. |
| HTTP retries | `tenacity` | Exponential backoff on transient errors (429 / 5xx / network). |
| Storage | Google Sheets via `gspread` + service account *(Step 4)* | Mirrors my existing manual layout, so I don't have to migrate years of history. |
| Front-end (eventually) | Telegram bot | Phone-friendly without building an app; webhooks are free. |
| Local dev front-end (now) | CLI (`--ping-llm`) | Lets us test the LLM layer end-to-end without any chat infra. |

## LLM client architecture (Step 2)

The whole LLM layer sits behind a single `Protocol`, so the rest of the app
never imports a provider SDK directly:

```
expense_tracker/llm/
├── base.py              ← Message, LLMResponse, LLMClient (Protocol)
├── exceptions.py        ← LLMError → {Config, Connection, RateLimit, Server, BadResponse}
├── factory.py           ← get_llm_client() — reads Settings.LLM_PROVIDER, returns a client
├── _json_repair.py      ← strip code fences, smart quotes, prose; build schema-grounding
├── _fake.py             ← FakeLLMClient — programmable stub used by tests
├── groq_client.py       ← official Groq SDK + retries + JSON mode + error mapping
├── ollama_client.py     ← raw httpx → localhost:11434, format=json
├── openai_client.py     ← OpenAI SDK (lazy import — optional dep)
└── anthropic_client.py  ← Anthropic SDK (lazy import — optional dep, no JSON-mode flag)
```

Two methods live on every client:

- **`complete(messages, ...) -> LLMResponse`** — free-form text out.
- **`complete_json(messages, schema, ...) -> (parsed, LLMResponse)`** —
  forces the model into JSON mode, injects the schema as a system-prompt
  grounding hint, parses + validates with Pydantic, raises `LLMBadResponseError`
  if the model emits something unparseable.

Why this shape:

- **Provider swap = one env var.** Set `LLM_PROVIDER=ollama` and the entire stack
  switches without touching application code.
- **Optional providers don't bloat startup.** OpenAI and Anthropic SDKs are
  imported lazily — installing them is opt-in via extras (`pip install -e ".[openai]"`).
- **All errors are typed.** Calling code only ever catches our own
  `LLMError` hierarchy — you never need to import provider SDK exceptions.
- **Retries are uniform.** All clients use `tenacity` with the same policy
  (3 attempts, exponential backoff, only retry transient classes).
- **Tests are offline.** `FakeLLMClient` implements the same protocol with
  a programmable response queue — no network, no flakes, no API spend.

## Storage / observability layer (Step 2.5)

Two append-only JSONL streams sit under `logs/`:

| File | One line per | Written by | Used for |
|---|---|---|---|
| `logs/llm_calls.jsonl` | LLM round-trip | `TracedLLMClient` (auto, when `LLM_TRACE=true`) | Prompt debugging, regression replay, cost tracking |
| `logs/conversations.jsonl` | User-bot turn | application code (Step 3+) | Audit trail, retrieval, few-shot personalization |

Why two separate files: a single user message can produce multiple LLM
calls (intent classification + extraction + reply), so they're 1-to-many.
`ConversationTurn.trace_ids` links a turn back to the LLM calls that
produced it — debugging a wrong answer is one `jq` away.

### Why JSONL, not SQLite/Postgres

Personal-scale volume (~30 MB / 5 years) doesn't justify a DBMS:

* Append-only writes are atomic at line granularity. No corruption risk.
* `cat`, `rg`, `jq`, `pandas.read_json(..., lines=True)`, and DuckDB
  (`SELECT * FROM 'logs/*.jsonl'`) all read these files natively.
* Backup is `cp`. Forget-me is `grep -v`. Schema migration is "add a
  field; readers ignore unknown keys".
* Every line carries `schema_version` so future readers can migrate cleanly.

The `ChatStore` *protocol* (`storage/base.py`) abstracts this. When
scale or query patterns demand it, we add a `SqliteChatStore` /
`DuckDBChatStore` / vector-store impl behind the same protocol — zero
application changes. **Today's choice doesn't paint us into a corner.**

### Schemas (excerpt)

```jsonl
# logs/llm_calls.jsonl
{"schema_version":1,"ts":"2026-04-25T20:09:00Z","trace_id":"tr_a1b2c3",
 "provider":"groq","model":"llama-3.1-8b-instant","json_mode":true,
 "schema_name":"_PingResult","messages":[...],"response":"...",
 "prompt_tokens":225,"completion_tokens":17,"total_tokens":242,
 "latency_ms":304.4,"outcome":"ok"}

# logs/conversations.jsonl  (Step 3+)
{"schema_version":1,"ts":"2026-04-25T20:09:00Z","session_id":"s_x9y8",
 "user_text":"spent 40 on food today","intent":"log_expense",
 "extracted":{"date":"2026-04-25","category":"Food","amount":40},
 "action":{"type":"sheets_append","sheet":"April 2026","row":25,"status":"ok"},
 "bot_reply":"Logged ₹40 to Food on Sat 25 Apr.",
 "trace_ids":["tr_a1b2c3"]}
```

Trace failures **never** break the user's chat — the wrapper logs a
warning and returns the LLM response unchanged.

## Repository layout

```
expense-tracker-bot/
├── README.md
├── .gitignore
├── .env.example
├── pyproject.toml
├── src/
│   └── expense_tracker/
│       ├── __init__.py
│       ├── __main__.py          # `python -m expense_tracker [--ping-llm [--json]]`
│       ├── config.py            # pydantic-settings; all env-driven knobs
│       ├── llm/                 # provider-agnostic LLM layer (Step 2)
│       │   ├── base.py            ← Message / LLMResponse / LLMClient Protocol
│       │   ├── exceptions.py      ← typed error hierarchy
│       │   ├── factory.py         ← get_llm_client() — wraps with tracer if enabled
│       │   ├── _traced.py         ← TracedLLMClient decorator (Step 2.5)
│       │   ├── _fake.py           ← FakeLLMClient — offline tests
│       │   ├── _json_repair.py    ← strip fences / smart quotes / schema grounding
│       │   ├── groq_client.py
│       │   ├── ollama_client.py
│       │   ├── openai_client.py     (lazy SDK)
│       │   └── anthropic_client.py  (lazy SDK)
│       └── storage/             # chat / trace history (Step 2.5)
│           ├── base.py            ← ChatStore Protocol + record dataclasses
│           ├── jsonl_store.py     ← JSONL impl with locking + schema versioning
│           └── factory.py         ← get_chat_store()
└── tests/
    ├── conftest.py              # isolated_env, fake_llm fixtures
    ├── test_config.py
    ├── test_llm_factory.py
    ├── test_llm_fake.py
    ├── test_llm_json_repair.py
    ├── test_llm_traced.py       # tracing wrapper + factory auto-wrap
    └── test_storage_jsonl.py    # JSONL store + concurrency + filters
```

## Roadmap (one commit per step)

1. Scaffold — empty package, build config, gitignore. **(done)**
2. **LLM client** — provider-agnostic protocol, Groq/Ollama/OpenAI/Anthropic backends, retries, JSON mode, fake for tests. **(done)**
2.5. **Chat history & tracing** — `ChatStore` protocol, JSONL impl, transparent tracing wrapper around any LLM client. **(done — this commit)**
3. Expense extractor — Pydantic schema + extraction prompt + tests with synthetic chats. Writes one `ConversationTurn` per user message.
4. Sheets foundation — service account, open spreadsheet, `sheet_format.yaml`-driven month creation.
5. Sheets writer — `gspread` integration that mirrors my existing monthly layout.
6. CLI — `expense add "spent 40 on food"` end-to-end.
7. Retrieval extractor — same extractor pattern but for query intents.
8. Sheets reader + aggregator — answers "how much did I spend on food in April?".
9. Telegram bot — wraps the CLI in a chat front-end.

## Running it

### One-time setup

```bash
cd ~/Documents/personal_github/expense-tracker-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Copy the env template and fill in your Groq key (free tier @ console.groq.com)
cp .env.example .env
# edit .env → set GROQ_API_KEY
```

### Smoke-test the LLM layer

```bash
# Plain text completion:
python -m expense_tracker --ping-llm

# Structured JSON completion (validates against a tiny Pydantic schema):
python -m expense_tracker --ping-llm --json
```

Expected output (Groq, with tracing on):

```
Provider : groq
Model    : llama-3.1-8b-instant
JSON mode: False
Tracing  : ./logs/llm_calls.jsonl
Sending tiny prompt...

Reply    : Hello, I am alive and ready to help!
Latency  : 312.4 ms
Tokens   : prompt=23 completion=11 total=34
Request  : a1b2c3d4e5f6
```

After the call, inspect the trace:

```bash
tail -1 logs/llm_calls.jsonl | python -m json.tool
```

### Switch providers

Edit one line in `.env`:

```bash
LLM_PROVIDER=ollama        # local; needs `ollama serve` + `ollama pull llama3.1`
LLM_PROVIDER=openai        # needs `pip install -e ".[openai]"` and OPENAI_API_KEY
LLM_PROVIDER=anthropic     # needs `pip install -e ".[anthropic]"` and ANTHROPIC_API_KEY
```

No code changes needed — the factory wires the right client.

### Run the tests

```bash
pytest
```

All tests are offline (they use `FakeLLMClient`), so they pass without
any API key set.
