# Personal Expense Tracker (chat-driven)

A personal project: chat with a bot ("today I spent 40 bucks on food") and have it
silently log the expense into a Google Sheet that mirrors the layout I already use
manually — one row per day, one column per category, daily totals on the right,
column totals at the bottom. The same bot also answers retrieval questions
("how much did I spend on food in April?" / "what did I spend on 24 Apr?").

> **Status:** Step 6 — retrieval queries are live. Logging, correcting,
> *and* asking ("how much did I spend on food in April?", "show last 5
> expenses", "what did I spend on 24 Apr?") all flow through the same
> chat pipeline — over CLI (`expense --chat …`) or Telegram. Foreign
> currencies (e.g. INR) are converted to USD on the way in, re-converted
> on amount edits, and aggregated in USD on the way out. Next up: Step 8
> polish (multi-turn clarification, weekly summaries).

> **Full project handbook:** [`HANDBOOK.md`](./HANDBOOK.md) — the
> zero-to-running guide covering every external setup step (Google
> Cloud project, service-account JSON, sheet sharing, Groq key,
> BotFather, allow-list bootstrap), every `.env` variable, the
> module-by-module code tour, the data model, FX rates, logs, the
> self-healing flow, daily cookbook, and troubleshooting. A
> downloadable Word version is at [`HANDBOOK.docx`](./HANDBOOK.docx)
> (rebuild via `python scripts/build_handbook.py` after edits).

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
│                 fixed schema (Date / Day / Month / Year /   │
│                 Category / Note / Vendor / Amount /         │
│                 Currency / Amount (USD) / FX Rate /         │
│                 Source / Trace ID / Timestamp)              │
│                 Conditional banding alternates rows by      │
│                 month — visual breaks without inserting     │
│                 separator rows that would break SUMIFS.     │
│                                                             │
│                 ``Month`` stores a full month name like     │
│                 "April"; ``Year`` is a 4-digit number.      │
│                 ``Timestamp`` (write time, far right) is    │
│                 distinct from ``Date`` (expense time, far   │
│                 left) — the gap surfaces backdated entries. │
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
* **Visual emphasis: quiet baseline, loud non-zero cells.** Every
  daily-grid cell is a SUMIFS that defaults to 0. Without emphasis the
  grid is a sea of "0.00" — readable but flat. The ``emphasis`` block
  in ``sheet_format.yaml`` defines a low-contrast baseline (light gray)
  for empty cells and a bold + dark/blue conditional rule that fires
  whenever a cell's value is greater than zero. Result: a normal day
  visually fades; a day with real spending pops. Per-category totals
  and the grand-total corner cell carry their own always-on loud
  styles. (Note: Google Sheets' conditional-format API doesn't accept
  font size, so the size of every grid cell is uniform — emphasis is
  via weight + color only.)
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

# Wipe + recreate Transactions (destructive — every row is lost).
# Use after a column-layout change so the bot rebuilds the master
# ledger with the new schema instead of refusing on a header mismatch.
python -m expense_tracker --reinit-transactions

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

## Chat → row writer (Step 5)

Step 5 closes the loop: a chat message becomes a typed `ExpenseEntry`,
the FX layer converts it to USD, the right monthly tab is auto-created
if needed, and one row lands in the master `Transactions` ledger — all
through a single CLI command.

```bash
# Real spreadsheet
expense --chat "spent 12.50 on coffee at starbucks today"

# Multi-currency — INR auto-converts to USD via Frankfurter.app
expense --chat "paid 499 RS for netflix"

# Retrieval queries (Step 6) — answered by reading the master ledger
expense --chat "how much did I spend on food in april?"
expense --chat "how much in total in april 2026?"
expense --chat "show me my last 5 expenses"
expense --chat "what did I spend on 24 April?"

# Smalltalk is classified and politely acknowledged
expense --chat "thanks!"

# Try any of the above offline (in-memory backend, no network)
expense --chat "spent 8 on lunch on may 3" --fake
```

What happens under the hood:

1. **Extractor** (Step 3) classifies the intent and — for
   `log_expense` — extracts an `ExpenseEntry`.
2. **`ExpenseLogger`** (`pipeline/logger.py`) converts the amount to
   USD, ensures the `Transactions` tab exists, ensures the monthly
   summary tab for the entry's date exists, builds a `TransactionRow`,
   and appends it.
3. **`format_reply`** (`pipeline/reply.py`) produces a short, friendly
   confirmation that the CLI prints back. The same string is what the
   Telegram bot sends back (Step 7).
4. **`Orchestrator.persist_turn`** writes one fully-resolved
   `ConversationTurn` to `logs/conversations.jsonl` — including the
   action outcome and the bot's reply, so every chat is auditable.

Failure isolation:

* FX API down → falls back to the most recent cached rate; expense
  still lands.
* Sheets API hiccup → returns a friendly "couldn't write" reply, the
  turn is still persisted with `action.status = "error"` for
  follow-up.
* LLM produces malformed JSON → graceful "couldn't parse" reply, no
  partial write.

## Telegram bot (Step 7)

Step 7 wraps the same `ChatPipeline` in a Telegram front-end so logging
an expense is as effortless as DM'ing the bot from your phone. The
SDK glue is intentionally thin (see `src/expense_tracker/telegram_app/`)
— every text message is shipped through the existing pipeline and the
bot replies with the same string the CLI prints.

### Why long-polling, not webhooks

Long-polling means the bot opens an outbound HTTPS connection to
Telegram and waits for updates. It works from a laptop, a Pi, or a
home server — no public URL, no TLS cert, no port forwarding. Plenty
of throughput for a personal bot. Webhook deployment is reserved for
later if there's a real need.

### One-time bot setup

```bash
# 1. Install the optional Telegram extra:
pip install -e ".[telegram]"

# 2. Create a bot in Telegram:
#    DM @BotFather → /newbot → choose a name + handle
#    BotFather replies with a token like 123456789:ABCdef...
#    Paste it into .env as TELEGRAM_BOT_TOKEN=...

# 3. Discover your Telegram user ID without restarting twice:
expense --telegram          # bot starts but refuses everyone
# In Telegram, DM the bot:  /whoami
# Bot replies with your numeric ID.
# Add it to .env:           TELEGRAM_ALLOWED_USERS=123456789
# Stop with Ctrl-C and restart `expense --telegram`.
```

### Daily use

```bash
expense --telegram
# In Telegram → DM the bot:
#   spent 40 on coffee today
#   1500 INR groceries yesterday at Costco
#   bought a tesla supercharge for $12.50
# Bot replies inline; row lands in your Sheet within ~1s.
```

`/start` and `/help` show a short usage hint. `/whoami` always works
(even for non-allowed users) so you can bootstrap the allow-list.

### Fixing wrong entries (self-healing)

Three commands target the **bottom-most** row of `Transactions` —
i.e. the most recently logged expense — and each one transparently
re-runs the affected monthly tab so totals stay in sync:

```
/last                show the last entry without changing it
/undo                delete the last entry
/edit amount 50      change the amount to 50 (re-runs FX automatically)
/edit category Food  change the category (aliases like "groceries"
                     resolve to canonical "Groceries")
```

Worked example:

```
You    : got shampoo for my wife which cost 100$
Bot    : Logged $100 to Saloon ...    ← bot guessed wrong
You    : /edit category Shopping
Bot    : Updated last expense:
          Category : Saloon -> Shopping
         Refreshed `April 2026` so totals stay in sync.
```

`/edit amount X` re-runs currency conversion against the original
row's currency + date — so editing a 1500 INR row to 2000 INR
produces a fresh `Amount (USD)` and `FX Rate` without you having to
think about it. Editing the currency itself is intentionally not
supported (rare and ambiguous: "convert" or "swap"?). If you need
that, `/undo` and re-log.

The same operations are available from the laptop too, with no
Telegram needed:

```bash
expense --undo
expense --edit-amount 50
expense --edit-category Shopping
expense --edit-amount 50 --edit-category Shopping   # combine
```

Why this works at all: a Google-Sheets quirk leaves cached formula
results stale when API writes hit the underlying data. Both `/undo`
and `/edit` (and every plain log) end with a "nudge" that re-writes
the headline summary + daily-total formulas on the affected monthly
tab. The user-facing recompute is best-effort: if the nudge fails
the user-visible operation is still reported successful, with a log
warning for follow-up.

### Auth model

The allow-list is **explicit** — `TELEGRAM_ALLOWED_USERS` is a
comma-separated list of integer Telegram user IDs and an empty value
means *nobody* is allowed. Unauthorized DMs get a polite refusal that
includes their user ID, never get routed to the LLM (zero cost), and
never touch your Google Sheet. The bot is, by construction, a
private tool.

### Failure handling

* LLM / Sheets exception during chat → user gets a generic "something
  went wrong" reply; the full traceback lands in `logs/` so you can
  debug after the fact.
* Telegram network blips during reply → handled by python-telegram-bot's
  built-in retry; we just log and move on.
* `python-telegram-bot` not installed → `expense --telegram` exits with
  a clear "install the [telegram] extra" message instead of crashing.

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
│       ├── sheets/              # Google Sheets layer (Step 4)
│       │   ├── backend.py         ← SheetsBackend / WorksheetHandle Protocols + FakeSheetsBackend
│       │   ├── format.py          ← Pydantic models for sheet_format.yaml
│       │   ├── transactions.py    ← Transactions schema + init + append helpers
│       │   ├── currency.py        ← Frankfurter.app FX with on-disk cache
│       │   ├── month_builder.py   ← formula builders + build_month_tab()
│       │   ├── ytd_builder.py     ← formula builders + build_ytd_tab()
│       │   ├── year_builder.py    ← bulk setup_year() + ensure_*_tab()
│       │   ├── gspread_backend.py ← real backend (lazy gspread import)
│       │   ├── factory.py         ← get_sheets_backend()
│       │   ├── exceptions.py      ← SheetsError hierarchy
│       │   └── data/sheet_format.yaml
│       ├── pipeline/             # chat orchestration (Steps 5, 6, 7.1)
│       │   ├── logger.py           ← ExpenseLogger + LogResult (FX + ensure_tab + append + recompute nudge)
│       │   ├── retrieval.py        ← RetrievalEngine + RetrievalAnswer (read ledger + filter + aggregate)
│       │   ├── correction.py       ← CorrectionLogger / UndoResult / EditResult (Step 7.1)
│       │   ├── reply.py            ← format_reply() — pure user-facing reply builder
│       │   ├── chat.py             ← ChatPipeline + ChatTurn (orchestrates one turn end-to-end)
│       │   ├── factory.py          ← get_chat_pipeline() / get_correction_logger() / get_retrieval_engine()
│       │   ├── exceptions.py       ← PipelineError / ExpenseLogError / RetrievalError / CorrectionError
│       │   └── __init__.py         ← public API
│       └── telegram_app/         # Telegram bot front-end (Step 7)
│           ├── auth.py             ← parse_allowed_users + Authorizer (no SDK)
│           ├── bot.py              ← MessageProcessor + CorrectionProcessor + async handler factories
│           ├── factory.py          ← build_application() / run_polling()
│           └── __init__.py
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
    ├── test_sheets_factory.py          # config validation + fake/real selection
    ├── test_pipeline_logger.py         # ExpenseLogger: FX, auto-vivify, alias resolve, errors
    ├── test_pipeline_retrieval.py      # RetrievalEngine: parsing, filtering, aggregation, multi-currency
    ├── test_pipeline_correction.py     # CorrectionLogger: undo, edit-amount, edit-category, recompute resilience
    ├── test_pipeline_reply.py          # format_reply: every intent + log/retrieval/error branches
    ├── test_pipeline_chat.py           # ChatPipeline end-to-end (FakeLLM + FakeSheetsBackend, log + retrieval)
    ├── test_telegram_auth.py           # allow-list parser + Authorizer
    ├── test_telegram_processor.py      # MessageProcessor (auth + pipeline orchestration)
    ├── test_telegram_correction.py     # CorrectionProcessor: /last, /undo, /edit + arg parser
    └── test_telegram_handlers.py       # async PTB-handler glue + Application factory
```

## Roadmap (one commit per step)

1. Scaffold — empty package, build config, gitignore. **(done)**
2. **LLM client** — provider-agnostic protocol, Groq/Ollama/OpenAI/Anthropic backends, retries, JSON mode, fake for tests. **(done)**
2.5. **Chat history & tracing** — `ChatStore` protocol, JSONL impl, transparent tracing wrapper around any LLM client. **(done)**
3. **Extractor** — Intent classification + schema-specific extraction (`ExpenseEntry` / `RetrievalQuery`), category registry, conversation-turn logging. **(done)**
4. **Sheets foundation** — service account auth, master `Transactions` ledger, formula-driven monthly + YTD tabs, multi-currency conversion, `--build-month / --setup-year` CLI. **(done)**
5. **Chat → row writer** — connect Orchestrator output to `append_transactions`, with `ensure_month_tab` autovivification, FX conversion, and graceful failure replies. New `expense --chat` CLI command. **(done)**
5.1. **Schema + visual polish** — Transactions reordered (Date | Day | Month | Year | Category | … | Timestamp). ``Month`` is now a human name ("April"), ``Year`` is a 4-digit int. ``Timestamp`` (bot-write time) moved to the far right so backdated entries read clearly. Daily grid + YTD grid now use a "quiet baseline / loud non-zero" emphasis. New ``expense --reinit-transactions`` for safe schema migrations. **(done)**
6. **Sheets reader + retrieval queries** — `RetrievalEngine` reads the master `Transactions` ledger directly (no SUMIFS round-trip, no stale-formula cache), filters by date window + canonical category + vendor, aggregates everything in USD, and surfaces top categories, per-day breakdowns, and the largest matching row. Wired into the same `ChatPipeline` as logging — every `query_*` intent is now answered, in CLI and Telegram, with a typed `RetrievalAnswer`. Unparseable rows are skipped, never crash the turn. **(done — this commit)**
7. **Telegram bot** — wraps the chat pipeline in a Telegram front-end. Long-polling (no public URL needed), explicit per-user-ID allow-list, `/start` / `/help` / `/whoami` commands, and `expense --telegram` CLI to run it. **(done)**
7.1. **Self-healing + corrections** — every log "nudges" the affected monthly tab so the daily grid + summary stay in sync (works around a Google Sheets stale-cache quirk). New `/last`, `/undo`, `/edit amount X`, `/edit category Y` Telegram commands and matching CLI flags (`--undo`, `--edit-amount`, `--edit-category`) target the bottom-most Transactions row. Amount edits re-run FX so `Amount (USD)` stays consistent; category edits canonicalize through the registry. Refined `categories.yaml` to keep personal-care products in `Shopping` (vs salon services). **(done)**
8. Polish — multi-turn clarification, weekly summaries, retrieval over multi-row /undo history.

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
