# Personal Expense Tracker (chat-driven)

A personal project: chat with a bot ("today I spent 40 bucks on food") and have it
silently log the expense into a Google Sheet that mirrors the layout I already use
manually — one row per day, one column per category, daily totals on the right,
column totals at the bottom. The same bot also answers retrieval questions
("how much did I spend on food in April?" / "what did I spend on 24 Apr?").

> **Status:** Step 4 of 9 complete — Google Sheets is wired. The bot can
> open your spreadsheet, build the master `Transactions` ledger, build any
> monthly tab (live formulas, daily grid, per-category breakdown), and
> build a `YTD <year>` dashboard. Multi-currency conversion (INR → USD via
> Frankfurter.app) is in. The chat → row write path lands in Step 5.

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

## Extractor pipeline (Step 3)

The extractor turns one chat message into one typed action. Two LLM
calls per turn, each with a tightly-scoped prompt:

```
                                                ┌──────────────────────────┐
                          ┌── log_expense ────► │  ExpenseExtractor        │ ─► ExpenseEntry
                          │                     │  (stage-2a, JSON mode)   │
        ┌────────────────┴┐                    └──────────────────────────┘
text ─► │ IntentClassifier │── query_*  ──────► ┌──────────────────────────┐
        │  (stage-1, JSON) │                    │  RetrievalExtractor      │ ─► RetrievalQuery
        └────────────────┬┘                    │  (stage-2b, JSON mode)   │
                          │                     └──────────────────────────┘
                          └── smalltalk / unclear ─► (no stage-2 call) ─► ExtractionResult
```

### Why two stages, not one

A small free model (llama-3.1-8b on Groq) is much more reliable on
two narrow prompts than one wide one. Each stage-2 prompt only sees
the schema relevant to *its* intent, with the user's local TODAY,
default currency, and full canonical-category list embedded. The
classifier's only job is picking the right schema.

### Intents

| Intent | Means | Stage 2 schema |
|---|---|---|
| `log_expense` | "spent 40 on food", "dropped 12 on coffee" | `ExpenseEntry` |
| `query_period_total` | "total this month", "how much in April" | `RetrievalQuery` |
| `query_category_total` | "how much for food in April" | `RetrievalQuery` |
| `query_day` | "what did I spend on 24 Apr" | `RetrievalQuery` |
| `query_recent` | "show last 5 transactions" | `RetrievalQuery` |
| `smalltalk` | "thanks", "hi" | — |
| `unclear` | bot couldn't tell | — |

### Categories — driven by YAML

`expense_tracker/extractor/data/categories.yaml` is the canonical
taxonomy and the single source of truth for both the LLM extractor and
the Sheets builder (column headers, monthly grid columns, breakdown
blocks, YTD grid columns). Each category has a display name, a one-line
hint sent to the LLM, and a list of aliases that resolve to it.
Excerpt:

```yaml
fallback_category: Miscellaneous
categories:
  - name: Food
    hint: dining out, restaurants, takeout, coffee, juice, ice cream
    aliases: [restaurant, takeout, coffee, latte, ice cream, juice, ...]
  - name: Tesla Car
    hint: Tesla charging, FSD, Premium Connectivity, insurance, service
    aliases: [tesla, charging, supercharger, fsd, ...]
```

The bot tells the LLM the canonical names; if the model emits an alias
or the wrong case anyway, the `CategoryRegistry` collapses it back to
canonical. Anything unrecognized falls to `fallback_category`
(`Miscellaneous` by default).

### Time anchoring (the only tricky bit)

Relative phrases like "today" / "yesterday" / "last week" need *your*
clock, not the server's. The orchestrator reads `TIMEZONE` from the
config (default `UTC` — set it to `America/Chicago` or whatever you
actually live in) and passes today's date into every stage-2 prompt
as an explicit anchor. Tests inject a frozen `now` callable so they
pass on any machine.

### What gets persisted

Every call to `Orchestrator.extract` writes:

* **One `ConversationTurn`** to `logs/conversations.jsonl` — the user
  text, classified intent, extracted payload, and `trace_ids` linking
  back to the per-stage LLM call records.
* **One `LLMCallRecord` per stage** to `logs/llm_calls.jsonl` — same
  shape as Step 2.5. All records from one turn share a `session_id`,
  so `jq 'select(.session_id == "x_abc123")'` reconstructs the full
  pipeline.

### Try it

```bash
python -m expense_tracker --extract "spent 40 on coffee yesterday"
python -m expense_tracker --extract "how much did I spend on food in April"
python -m expense_tracker --extract "thanks!"
```

## Google Sheets layer (Step 4)

The Sheets layer is split into a backend protocol, a few formula-driven
builders, and a thin gspread adapter. Three tabs make up the picture:

```
┌─────────────────────────────────────────────────────────────┐
│  Transactions   master ledger — every expense is one row    │
│                 fixed schema (Timestamp / Date / Day /      │
│                 Month / Category / Note / Vendor / Amount / │
│                 Currency / Amount (USD) / FX Rate /         │
│                 Source / Trace ID)                          │
│                 Conditional banding alternates rows by      │
│                 month — visual breaks without inserting     │
│                 separator rows that would break SUMIFS.     │
└─────────────────────────────────────────────────────────────┘
                              ▲
                              │ all formulas point here
                              │
┌─────────────────────────────┴───────────────────────────────┐
│  April 2026, May 2026, …    monthly summary tabs            │
│                             (live formulas, no data of      │
│                             their own)                      │
│                             • title + summary block         │
│                             • daily grid (Date | Day |      │
│                               13 categories | TOTAL)        │
│                             • per-category breakdown by     │
│                               note (top N)                  │
│                                                             │
│  YTD 2026                   year-to-date dashboard          │
│                             • title + year summary          │
│                             • Monthly × Category grid       │
│                             • Top vendors block             │
└─────────────────────────────────────────────────────────────┘
```

### Why this shape

* **One source of truth.** Only the master `Transactions` tab holds
  data. Monthly + YTD views are pure live formulas; rebuilding them is
  destructive only of layout, never of expense history.
* **Multi-year, single spreadsheet.** Tabs are named `April 2026`,
  `YTD 2026`, etc. When 2027 rolls in you tell the bot
  *"set up 2027"* and it adds the new tabs alongside; old data stays
  navigable for year-over-year comparison.
* **YAML for visuals, code for structure.**
  `sheets/data/sheet_format.yaml` controls colors, widths, freeze panes,
  number formats, name patterns. Column order, formula bodies, and the
  region layout live in Python where they're unit-testable.
* **Backend abstraction.** `SheetsBackend` is a Protocol; the real one
  uses gspread, the test one is in-memory. Every layout assertion runs
  against the in-memory backend without hitting Google.
* **Multi-currency.** `currency.py` converts INR (or any 3-letter ISO
  code) to USD via Frankfurter.app, caches the rate on disk, and falls
  back to the most recent cached rate if the API is down.

### Google Cloud setup (one-time)

The bot writes to your Google Sheet through a *service account* — a
robot identity that the Sheet is shared with. Steps:

1. **Create a project** at <https://console.cloud.google.com>.
2. **APIs & Services → Library** → enable **Google Sheets API** and
   **Google Drive API**.
3. **APIs & Services → Credentials → Create Credentials →
   Service account.** Give it a name (e.g. `expense-bot`).
4. On the new account, **Keys → Add key → JSON.** A `.json` file
   downloads — this is your robot's password.
5. Create a `secrets/` folder in the repo (it's gitignored), move the
   JSON there, and set its path in `.env`:

   ```env
   GOOGLE_SERVICE_ACCOUNT_JSON=./secrets/service-account.json
   ```
6. **Share your Google Sheet with the service-account email** (looks
   like `expense-bot@your-project.iam.gserviceaccount.com`) as Editor.
7. Copy the long token between `/spreadsheets/d/` and `/edit` from the
   sheet URL into `.env`:

   ```env
   EXPENSE_SHEET_ID=1AbCdEf...
   ```

Verify it all works:

```bash
python -m expense_tracker --whoami
```

You should see your spreadsheet title, URL, and the service-account
email.

### Sheets CLI

All of these honour `--fake` for offline previews:

```bash
# Inspect the configured spreadsheet
python -m expense_tracker --whoami
python -m expense_tracker --list-sheets

# Build the Transactions master ledger (idempotent)
python -m expense_tracker --init-transactions

# Build / rebuild a single monthly tab
python -m expense_tracker --build-month 2026-04
python -m expense_tracker --rebuild-month 2026-04   # delete + recreate

# Build / rebuild the YTD dashboard
python -m expense_tracker --build-ytd 2026
python -m expense_tracker --rebuild-ytd 2026

# Bulk-set-up an entire year (12 monthly tabs + YTD)
python -m expense_tracker --setup-year 2026
python -m expense_tracker --setup-year 2027 --hide-previous   # tucks 2026 monthlies away

# Preview any of the above offline (no network):
python -m expense_tracker --setup-year 2027 --fake
```

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
│       ├── storage/             # chat / trace history (Step 2.5)
│       │   ├── base.py            ← ChatStore Protocol + record dataclasses
│       │   ├── jsonl_store.py     ← JSONL impl with locking + schema versioning
│       │   └── factory.py         ← get_chat_store()
│       ├── extractor/           # chat → typed action (Step 3)
│       │   ├── schemas.py         ← Intent enum + ExpenseEntry + RetrievalQuery + ExtractionResult
│       │   ├── categories.py      ← CategoryRegistry (alias → canonical)
│       │   ├── prompts.py         ← all prompt templates, one place
│       │   ├── intent_classifier.py
│       │   ├── expense_extractor.py
│       │   ├── retrieval_extractor.py
│       │   ├── orchestrator.py    ← public entry point
│       │   └── data/categories.yaml
│       └── sheets/              # Google Sheets layer (Step 4)
│           ├── backend.py         ← SheetsBackend / WorksheetHandle Protocols + FakeSheetsBackend
│           ├── format.py          ← Pydantic models for sheet_format.yaml
│           ├── transactions.py    ← Transactions schema + init + append helpers
│           ├── currency.py        ← Frankfurter.app FX with on-disk cache
│           ├── month_builder.py   ← formula builders + build_month_tab()
│           ├── ytd_builder.py     ← formula builders + build_ytd_tab()
│           ├── year_builder.py    ← bulk setup_year() + ensure_*_tab()
│           ├── gspread_backend.py ← real backend (lazy gspread import)
│           ├── factory.py         ← get_sheets_backend()
│           ├── exceptions.py      ← SheetsError hierarchy
│           └── data/sheet_format.yaml
└── tests/
    ├── conftest.py              # isolated_env, fake_llm fixtures
    ├── test_config.py
    ├── test_llm_factory.py
    ├── test_llm_fake.py
    ├── test_llm_json_repair.py
    ├── test_llm_traced.py
    ├── test_storage_jsonl.py
    ├── test_extractor_schemas.py
    ├── test_categories.py
    ├── test_intent_classifier.py
    ├── test_expense_extractor.py
    ├── test_retrieval_extractor.py
    ├── test_orchestrator.py
    ├── test_sheets_format.py
    ├── test_sheets_backend_fake.py
    ├── test_sheets_transactions.py
    ├── test_sheets_month_builder.py    # MonthLayout + every formula + end-to-end builds
    ├── test_sheets_ytd_builder.py      # YTDLayout + formulas + end-to-end builds
    ├── test_sheets_year_builder.py     # bulk setup + hide-previous + ensure_*
    ├── test_sheets_currency.py         # cache, identity, API path, stale fallback
    └── test_sheets_factory.py          # config validation + fake/real selection
```

## Roadmap (one commit per step)

1. Scaffold — empty package, build config, gitignore. **(done)**
2. **LLM client** — provider-agnostic protocol, Groq/Ollama/OpenAI/Anthropic backends, retries, JSON mode, fake for tests. **(done)**
2.5. **Chat history & tracing** — `ChatStore` protocol, JSONL impl, transparent tracing wrapper around any LLM client. **(done)**
3. **Extractor** — Intent classification + schema-specific extraction (`ExpenseEntry` / `RetrievalQuery`), category registry, conversation-turn logging. **(done)**
4. **Sheets foundation** — service account auth, master `Transactions` ledger, formula-driven monthly + YTD tabs, multi-currency conversion, `--build-month / --setup-year` CLI. **(done — this commit)**
5. Chat → row writer — connect Orchestrator output to `append_transactions`, with `ensure_month_tab` autovivification.
6. Sheets reader + aggregator — answer retrieval queries by SUMIFS-equivalent reads.
7. Telegram bot — wraps the CLI in a chat front-end.
8. Polish — `--undo`, multi-turn clarification, weekly summaries.

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

# Run the full extractor pipeline on one message:
python -m expense_tracker --extract "spent 40 on coffee yesterday"
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
