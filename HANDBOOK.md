# Expense Tracker Bot — Project Handbook

> **Last updated:** 2026-04-27 · **Code version:** Step 7.1 (commit `fc66657`)
>
> A complete top-to-bottom guide to this project: what it is, how every
> external service was set up, what every line of configuration does,
> and how the code is organized. If something here is wrong or outdated,
> the source code wins — but every change should ship with an update to
> this document so it never falls behind reality.

---

## Table of contents

1. [What this project is](#1-what-this-project-is)
2. [How it works in 3 minutes](#2-how-it-works-in-3-minutes)
3. [Setup from scratch — external services](#3-setup-from-scratch--external-services)
   - 3.1 [Prerequisites](#31-prerequisites)
   - 3.2 [Google Cloud project + APIs + service account](#32-google-cloud-project--apis--service-account)
   - 3.3 [Google Sheet — create, share, get the ID](#33-google-sheet--create-share-get-the-id)
   - 3.4 [Groq API key — free LLM](#34-groq-api-key--free-llm)
   - 3.5 [Telegram bot — BotFather, token, allow-list](#35-telegram-bot--botfather-token-allow-list)
4. [Setup from scratch — local install](#4-setup-from-scratch--local-install)
   - 4.1 [Clone + Python venv + dependencies](#41-clone--python-venv--dependencies)
   - 4.2 [`.env` walkthrough — every variable explained](#42-env-walkthrough--every-variable-explained)
   - 4.3 [First sheet build](#43-first-sheet-build)
   - 4.4 [First chat test](#44-first-chat-test)
   - 4.5 [Connecting Telegram](#45-connecting-telegram)
5. [Tech stack reference](#5-tech-stack-reference)
6. [Code architecture — module by module](#6-code-architecture--module-by-module)
7. [CLI reference — every flag](#7-cli-reference--every-flag)
8. [Telegram reference — every command](#8-telegram-reference--every-command)
9. [Data model — what gets stored where](#9-data-model--what-gets-stored-where)
10. [Multi-currency — how FX works](#10-multi-currency--how-fx-works)
11. [Logs & observability](#11-logs--observability)
12. [Self-healing & corrections](#12-self-healing--corrections)
13. [Daily workflow cookbook](#13-daily-workflow-cookbook)
14. [Troubleshooting](#14-troubleshooting)
15. [Maintenance — backup, year rollover, schema migration](#15-maintenance--backup-year-rollover-schema-migration)
16. [Roadmap](#16-roadmap)
17. [Glossary](#17-glossary)

---

## 1. What this project is

A chat-driven personal expense tracker that:

- Accepts plain English from a phone or laptop ("spent 40 bucks on coffee
  today", "1500 INR for groceries yesterday at Costco").
- Uses an LLM to extract structured fields (date, category, amount,
  currency, vendor, note).
- Converts foreign-currency amounts to USD using a free FX API.
- Writes one row per expense to a Google Sheet that mirrors the layout
  the operator already used manually — one row per day, one column per
  category, daily totals on the right, monthly totals at the bottom.
- Auto-creates new monthly tabs and the year-to-date dashboard with
  live formulas, never with copy-pasted data.
- Supports correction commands (`/undo`, `/edit amount X`, `/edit
  category Y`) that target the most recent expense and keep monthly
  totals in sync.
- Runs as either a one-off CLI (`expense --chat "spent 40 on coffee"`)
  or a long-polling Telegram bot (`expense --telegram`).

It is **personal-scale**: one user, one Google Sheet, one Telegram bot.
Multi-tenant features (per-user OAuth, billing, signup) are not in
scope — see §16 for what would need to change to commercialize this.

### Design philosophy

- **One source of truth.** Every expense lives in the master
  `Transactions` ledger. Monthly + YTD tabs are pure formulas; rebuilding
  them is destructive only of layout, never of expense history.
- **Provider-agnostic.** Swap LLM providers (Groq → OpenAI → Anthropic
  → Ollama) by changing one env var. No code changes.
- **Offline-friendly.** Every Sheets-touching command has a `--fake`
  variant that uses an in-memory backend for testing without burning
  API quota.
- **Typed end-to-end.** Pydantic models gate every interface; the LLM
  is forced into JSON mode and the response is validated before it
  ever reaches the spreadsheet.
- **Auditable.** Every LLM call lands in `logs/llm_calls.jsonl`; every
  user turn lands in `logs/conversations.jsonl`. Both are append-only
  JSONL with `schema_version` for forward compatibility.

---

## 2. How it works in 3 minutes

```
            Telegram message              ┌────────────────────────────┐
   You ─────────────────────────────────► │  Telegram bot (long-poll)  │
   ▲                                      │  src/expense_tracker/      │
   │                                      │  telegram_app/             │
   │                                      └──────────────┬─────────────┘
   │                                                     │  Authorize +
   │  Bot reply                                          │  hand off text
   │                                                     ▼
   │                                      ┌────────────────────────────┐
   │                                      │  ChatPipeline              │
   │                                      │  src/expense_tracker/      │
   │                                      │  pipeline/chat.py          │
   │                                      └──────┬──────────────┬──────┘
   │                                             │              │
   │                              classify intent│              │ format
   │                                             ▼              │ reply
   │                                ┌──────────────────┐        │
   │                                │ Intent           │        │
   │                                │ classifier (LLM) │        │
   │                                └────────┬─────────┘        │
   │                                         │                  │
   │                  log_expense?  ─────────┴─── retrieval?    │
   │                       │                          │         │
   │                       ▼                          ▼         │
   │            ┌────────────────────┐    ┌────────────────────┐│
   │            │ Expense extractor  │    │ Retrieval extractor││
   │            │ (LLM, JSON mode)   │    │ (LLM, JSON mode)   ││
   │            └─────────┬──────────┘    └────────┬───────────┘│
   │                      │  ExpenseEntry          │            │
   │                      ▼                        ▼            │
   │            ┌────────────────────┐    ┌────────────────────┐│
   │            │ ExpenseLogger      │    │ Retrieval engine   ││
   │            │ pipeline/logger.py │    │  (Step 6 — pending)││
   │            │  • FX convert      │    └────────────────────┘│
   │            │  • ensure_month_tab│                          │
   │            │  • append_row      │                          │
   │            │  • recompute nudge │                          │
   │            └─────────┬──────────┘                          │
   │                      │                                     │
   │                      ▼                                     │
   │      ┌──────────────────────────────────┐                  │
   │      │  Google Sheets (gspread)         │                  │
   │      │   • Transactions (master ledger) │                  │
   │      │   • April 2026 (monthly summary) │                  │
   │      │   • YTD 2026   (annual rollup)   │                  │
   │      └──────────────────┬───────────────┘                  │
   │                         │                                  │
   └─────────────────────────┴──────────────────────────────────┘
                          confirmation reply
```

**Two LLM calls per turn, not one.** A small free model (Groq's
`llama-3.1-8b-instant`) is more reliable on two narrow prompts than
one wide one. The intent classifier picks the right schema
(`ExpenseEntry` vs `RetrievalQuery`); the second-stage extractor only
sees the schema relevant to that intent.

**Foreign currencies convert on the way in.** "1500 INR" is intercepted
between the extractor and the row writer, looked up against
[Frankfurter.app](https://frankfurter.app/) (free, no key), and
written as both INR (original) and USD (converted) so the monthly
totals always sum in one currency.

**Monthly tabs are pure formulas.** A row for `2026-04-25 / Saloon /
$30` lands only in `Transactions`. The cell `April 2026!I26` is a
SUMIFS that finds it. Rebuild the monthly tab anytime — no data is
lost.

---

## 3. Setup from scratch — external services

This section is the part most personal-handbooks skip. Read it once,
note the IDs into your `.env`, and you'll never touch these UIs
again.

### 3.1 Prerequisites

- A Google account (any free Gmail works).
- A phone with the Telegram app installed (iOS or Android).
- Linux, macOS, or WSL with Python 3.10+ (`python3 --version`).
- ~10 minutes for the external setup, ~5 minutes for the local install.

### 3.2 Google Cloud project + APIs + service account

The bot writes to your Sheet via a **service account** — a robot
identity owned by a Google Cloud project. The flow is:

1. **Create a Google Cloud project.**
   - Go to <https://console.cloud.google.com>.
   - Top-left: project picker → **New Project**.
   - Pick any name, e.g. `expense-bot`. The auto-generated project ID
     (e.g. `expense-bot-481234`) is fine.
   - Hit Create. Wait ~30s for it to finish.

2. **Enable the two APIs the bot needs.**
   - Top-left menu → **APIs & Services → Library**.
   - Search "Google Sheets API" → click → **Enable**.
   - Back to Library → search "Google Drive API" → **Enable**.
   - You only need Drive API to *list* spreadsheets; if you don't care
     about that you can technically skip it, but `expense
     --list-sheets` won't work.

3. **Create the service account.**
   - **APIs & Services → Credentials → Create Credentials → Service
     account**.
   - Name it whatever you want (e.g. `expense-bot`).
   - Skip the "grant access" step (we'll do that manually on the
     sheet itself in §3.3 — narrower scope).
   - Hit Done.

4. **Generate a JSON key for the service account.**
   - Click the service account you just created.
   - **Keys → Add key → Create new key → JSON**.
   - A `.json` file downloads automatically. **This is the robot's
     password — treat it like one.**
   - Note the service-account email — it looks like
     `expense-bot@expense-bot-481234.iam.gserviceaccount.com`. You'll
     need this in §3.3.

5. **Stash the JSON.**
   - Move the downloaded file into the repo at
     `secrets/service-account.json`. (The `secrets/` folder is
     git-ignored.)
   - You'll point `.env`'s `GOOGLE_SERVICE_ACCOUNT_JSON` at this path.

If anything goes wrong, the most common errors and their fixes are in
§14.

### 3.3 Google Sheet — create, share, get the ID

You can use an existing sheet or start fresh. Either way:

1. **Create a Google Sheet** at <https://sheets.new>. Name it
   anything — `Expense Tracker 2026` is a reasonable default.

2. **Share it with the service account.**
   - Click **Share** (top-right).
   - Paste the service-account email from §3.2.4 (e.g.
     `expense-bot@expense-bot-481234.iam.gserviceaccount.com`).
   - Set role to **Editor**.
   - **Untick** "Notify people" — robots don't read email.
   - Hit Share.

3. **Grab the spreadsheet ID from the URL.** Sheet URLs look like:

   ```
   https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit
                                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                       this part is the spreadsheet ID
   ```

   Copy that long token. You'll set `EXPENSE_SHEET_ID=...` in `.env`.

4. **Verify access from the laptop** (after §4 is done):

   ```bash
   expense --whoami
   ```

   You should see your spreadsheet title, URL, and the service-account
   email. If you see "PermissionError" or "spreadsheet not found",
   re-check that you shared the sheet with the *exact* service-account
   email (Google's UI sometimes silently mistypes if you click away
   too early).

### 3.4 Groq API key — free LLM

Groq runs Meta's Llama models on custom hardware. **Free tier is
generous** — for this personal use case (a few hundred messages a
day) you'll never hit a rate limit.

1. Sign up at <https://console.groq.com>.
2. **API Keys → Create API Key**. Give it any name (e.g. `expense-bot`).
3. **Copy the key NOW** — Groq doesn't show it twice. Format:
   `gsk_AbCdEf...xyz`.
4. Paste it into `.env` as `GROQ_API_KEY=...`.

Default model used by this bot: `llama-3.1-8b-instant`. It's small,
fast (~300ms latency per call), and good enough for the two-stage
extraction pattern. You can override with `GROQ_MODEL=...` if Groq
ever deprecates 3.1.

### 3.5 Telegram bot — BotFather, token, allow-list

Telegram bots are created by chatting with another Telegram bot called
**@BotFather**.

1. **Open Telegram → search for `@BotFather`** → start the chat.

2. **Send `/newbot`.** BotFather asks for two things:
   - **Display name** — what shows in the chat header. Anything works.
   - **Username** — must end in `bot` (e.g. `vinay_expense_bot`).
     Must be globally unique on Telegram.

3. **Token.** BotFather replies with a token like
   `8702975628:AAF2Q_sJS0-8T6F-NHzwS4f8eloCL1QIfz8`. Treat this like a
   password: anyone with it can impersonate your bot. Paste it into
   `.env` as `TELEGRAM_BOT_TOKEN=...`.

4. **Bootstrap the allow-list.**

   The bot refuses every message until your numeric Telegram user ID
   is on the allow-list. It's a chicken-and-egg problem: you need to
   know your user ID *before* you can use the bot, but the bot tells
   you your user ID. Two-step solve:

   ```bash
   # On the laptop:
   expense --telegram
   # Bot starts. Logs say "Allowed users: <none — bot will refuse everyone>".
   ```

   On your phone, DM the bot once. Send: `/whoami`. It will reply with
   your numeric ID, e.g. `Your Telegram user ID is 8684705854`.

   Stop the bot with **Ctrl-C**. Edit `.env`:

   ```
   TELEGRAM_ALLOWED_USERS=8684705854
   ```

   Restart `expense --telegram`. Your DMs now flow through the
   pipeline.

   To allow multiple users, comma-separate: `TELEGRAM_ALLOWED_USERS=8684705854,123456789`.

   **Empty value = nobody allowed**, which is the safe default.
   Unauthorized DMs get a polite refusal that includes their user ID,
   never invoke the LLM (zero cost), and never touch your sheet.

---

## 4. Setup from scratch — local install

### 4.1 Clone + Python venv + dependencies

```bash
# Clone (or `cd` into your existing checkout):
git clone <repo-url> ~/Documents/personal_github/expense-tracker-bot
cd ~/Documents/personal_github/expense-tracker-bot

# Create a project-local virtualenv (Python 3.10+ required):
python3 -m venv .venv
source .venv/bin/activate

# Install with dev + telegram extras (covers everything):
pip install -e ".[dev]"
```

The `-e` (editable) install means changes you make to `src/` take
effect without reinstalling.

Install minimal subset if you don't need the Telegram bot or the dev
tools:

```bash
pip install -e .            # core only — CLI works, no Telegram, no tests
pip install -e ".[telegram]"# adds python-telegram-bot
pip install -e ".[openai]"  # adds OpenAI SDK (optional alternative LLM)
pip install -e ".[anthropic]" # adds Anthropic SDK (optional)
pip install -e ".[all]"     # all optional providers + Telegram
```

### 4.2 `.env` walkthrough — every variable explained

Copy the template:

```bash
cp .env.example .env
```

Open `.env` and fill in. Every variable, in the order it appears:

#### LLM provider

| Variable | Value | Notes |
|---|---|---|
| `LLM_PROVIDER` | `groq` | One of `groq` / `ollama` / `openai` / `anthropic` / `fake`. Groq is free and the default. |
| `GROQ_API_KEY` | `gsk_...` | From §3.4. Required when provider is `groq`. |
| `GROQ_MODEL` | `llama-3.1-8b-instant` | Default. Override only if Groq deprecates this model. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Only used when provider is `ollama`. |
| `OLLAMA_MODEL` | `llama3.1` | The model you've `ollama pull`ed locally. |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | (paid) | For provider=`openai`. Requires the `[openai]` extra. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | (paid) | For provider=`anthropic`. Requires the `[anthropic]` extra. |

#### Common LLM tunables

| Variable | Default | Why you'd change it |
|---|---|---|
| `LLM_TIMEOUT_S` | `30.0` | Increase if your network is slow; the bot retries 3× on transient errors. |
| `LLM_MAX_RETRIES` | `3` | Total attempts on rate-limit / 5xx / network errors. |
| `LLM_TEMPERATURE` | `0.1` | Extraction wants determinism, not creativity. Don't bump this. |
| `LLM_MAX_TOKENS` | `1024` | Soft cap. Plenty for any single turn. |

#### Locale

| Variable | Default | Why it matters |
|---|---|---|
| `TIMEZONE` | `UTC` | **Set this to your IANA zone** (`America/Chicago`, `Asia/Kolkata`, `Europe/London`, etc.). The LLM resolves "today" / "yesterday" / "last Tuesday" against this clock. Wrong zone = wrong dates. |
| `DEFAULT_CURRENCY` | `USD` | What currency to assume when the user doesn't specify. |
| `EXTRACTOR_CATEGORIES_FILE` | unset | Optional: path to a YAML file overriding the bundled category taxonomy. The bundled one (`src/expense_tracker/extractor/data/categories.yaml`) is what most users want. |

#### Storage / observability

| Variable | Default | Notes |
|---|---|---|
| `LLM_TRACE` | `true` | When true, every LLM round-trip is appended to `logs/llm_calls.jsonl`. Free debugging gold. |
| `LOG_DIR` | `./logs` | Where the JSONL streams go. Relative to the directory you launch the bot from. |
| `CHAT_STORE_BACKEND` | `jsonl` | Today: JSONL. SQLite/DuckDB/vector store can be swapped in later. |

#### Google Sheets

| Variable | Required | Notes |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes | Path to the JSON file from §3.2.4. Default: `./secrets/service-account.json`. |
| `EXPENSE_SHEET_ID` | yes | The spreadsheet ID from §3.3.3. |
| `SHEET_FORMAT_FILE` | optional | Path to YAML overriding the bundled visual format. |
| `SHEETS_TIMEOUT_S` | `30.0` | Per-request timeout. |

#### Telegram bot

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes (for `--telegram`) | From §3.5.3. |
| `TELEGRAM_ALLOWED_USERS` | yes (for `--telegram`) | Comma-separated numeric user IDs. **Empty = nobody allowed**. |

### 4.3 First sheet build

Verify the Google connection:

```bash
expense --whoami
```

Expected output:

```
Spreadsheet : Expense Tracker 2026
URL         : https://docs.google.com/spreadsheets/d/1AbC.../edit
Service acc : expense-bot@expense-bot-481234.iam.gserviceaccount.com
```

Build the master ledger and the current-month tab:

```bash
expense --init-transactions
expense --build-month 2026-04
expense --build-ytd 2026
```

Or do everything at once for the year:

```bash
expense --setup-year 2026
```

You should see these tabs in your sheet:

- `Transactions` — empty save for the header row.
- `January 2026` through `December 2026` — empty daily grids with
  formulas already wired.
- `YTD 2026` — empty monthly × category grid with formulas wired.

Add `--fake` to any of the above to run offline against the in-memory
backend. Useful for previewing layout changes before touching the real
sheet.

### 4.4 First chat test

Without launching the Telegram bot, end-to-end chat works from the CLI:

```bash
expense --chat "spent 12.50 on coffee at starbucks today"
```

Expected: a friendly confirmation, and one row appearing in
`Transactions` + the right cell on `April 2026` lighting up.

Try a foreign-currency one:

```bash
expense --chat "1500 INR for groceries yesterday at costco"
```

Multi-currency conversion runs automatically; check the row — it
should have `1500.00 INR` in Amount and `~$18.00` in Amount (USD).

### 4.5 Connecting Telegram

```bash
expense --telegram
```

The bot starts long-polling. From your phone:

- DM the bot any text → it logs the expense and replies.
- `/start` or `/help` — usage hint.
- `/last` — show the most-recent expense.
- `/undo` — delete the most-recent expense.
- `/edit amount 50` — change the amount on the last row.
- `/edit category Shopping` — change the category on the last row.
- `/whoami` — your numeric Telegram user ID.

**Important caveat:** the bot only runs while `expense --telegram` is
running on the laptop. Close the laptop, the bot stops. To run 24/7
you need a hosted machine — see §16.

---

## 5. Tech stack reference

| Layer | Library / service | Version | Why this and not alternatives |
|---|---|---|---|
| Language | Python | 3.10+ | Best LLM ecosystem; type hints + match statements. |
| Type-safe data | Pydantic | 2.5–3.0 | Validates LLM JSON; runtime + IDE hints. |
| Settings | `pydantic-settings` | 2.1–3.0 | Reads `.env` into a typed `Settings` model. |
| HTTP retries | `tenacity` | 8.5–10.0 | Exponential backoff on every external call. |
| HTTP client | `httpx` | 0.27–1.0 | Used by Ollama + Frankfurter; modern async-friendly. |
| LLM (default) | Groq SDK | 0.13–1.0 | Fast, free, OpenAI-compatible JSON mode. |
| LLM (offline) | Ollama (raw `httpx`) | — | 100% local; no key, no quota. |
| LLM (paid alt) | OpenAI SDK | 1.40–2.0 | Optional via `[openai]` extra. |
| LLM (paid alt) | Anthropic SDK | 0.40–1.0 | Optional via `[anthropic]` extra. |
| Google Sheets | `gspread` | 6.0–7.0 | Mature wrapper for the Sheets API. |
| Auth | `google-auth` | 2.30–3.0 | Service-account JWT exchange. |
| YAML configs | `pyyaml` | 6.0–7.0 | Loads `categories.yaml` + `sheet_format.yaml`. |
| Date parsing | `python-dateutil` | 2.8–3.0 | Fallback NL date parser; LLM does most of it. |
| Telegram bot | `python-telegram-bot` | 21.6–22.0 | Optional; `[telegram]` extra. |
| FX rates | Frankfurter.app | (free, no key) | ECB-derived; no signup required. |
| Tests | pytest + pytest-asyncio | 8+ / 0.23+ | 394 tests, all offline. |
| Linting | ruff | 0.5+ | Fast all-in-one linter + isort + pyupgrade. |

### Why these choices

- **Groq over OpenAI** for default: free tier, OpenAI-compatible API,
  fast (~300 ms/call). Good enough for the two-stage extraction we do.
- **gspread over the raw Sheets REST API**: handles batching, A1 ↔
  R1C1 conversion, and conditional formatting requests cleanly.
- **JSONL over SQLite** for chat history: append-only, atomic at line
  granularity, readable with `cat`/`jq`/`pandas`, ~30 MB / 5 years
  worth of personal use.
- **Pydantic v2 over dataclasses** for LLM output: runtime validation
  is non-negotiable when the source is a probabilistic model.
- **python-telegram-bot** over alternatives: most mature library;
  long-polling (no public URL or webhook setup) is the default.

---

## 6. Code architecture — module by module

```
expense-tracker-bot/
├── README.md                 ← Quick reference + roadmap
├── HANDBOOK.md               ← This file (the deep dive)
├── HANDBOOK.docx             ← Word version (auto-generated)
├── pyproject.toml            ← Build config + deps + ruff + pytest
├── .env.example              ← Template for .env
├── .gitignore                ← Excludes .venv, .env, secrets/, logs/
├── src/expense_tracker/
│   ├── __init__.py           ← Version constant
│   ├── __main__.py           ← argparse CLI entry point
│   ├── config.py             ← pydantic-settings Settings class
│   ├── llm/                  ← Provider-agnostic LLM layer (Step 2)
│   │   ├── base.py             ← Message / LLMResponse / LLMClient Protocol
│   │   ├── exceptions.py       ← LLMError → {Config,Connection,RateLimit,Server,BadResponse}
│   │   ├── factory.py          ← get_llm_client() — wraps with tracer if enabled
│   │   ├── _traced.py          ← TracedLLMClient (writes JSONL)
│   │   ├── _fake.py            ← FakeLLMClient — programmable stub for tests
│   │   ├── _json_repair.py     ← Strip code fences / smart quotes; schema grounding
│   │   ├── groq_client.py      ← Groq SDK wrapper
│   │   ├── ollama_client.py    ← Raw httpx → localhost:11434
│   │   ├── openai_client.py    ← Lazy OpenAI SDK
│   │   └── anthropic_client.py ← Lazy Anthropic SDK
│   ├── storage/              ← Chat / trace history (Step 2.5)
│   │   ├── base.py             ← ChatStore Protocol + record dataclasses
│   │   ├── jsonl_store.py      ← JSONL impl with file locking + schema versioning
│   │   └── factory.py          ← get_chat_store()
│   ├── extractor/            ← Chat → typed action (Step 3)
│   │   ├── schemas.py          ← Intent / ExpenseEntry / RetrievalQuery / ExtractionResult
│   │   ├── categories.py       ← CategoryRegistry (alias → canonical)
│   │   ├── prompts.py          ← All prompt templates
│   │   ├── intent_classifier.py
│   │   ├── expense_extractor.py
│   │   ├── retrieval_extractor.py
│   │   ├── orchestrator.py     ← Public entry point
│   │   └── data/categories.yaml← The 13 canonical categories + aliases
│   ├── sheets/               ← Google Sheets layer (Step 4)
│   │   ├── backend.py          ← SheetsBackend / WorksheetHandle Protocols + FakeSheetsBackend
│   │   ├── format.py           ← Pydantic models for sheet_format.yaml
│   │   ├── transactions.py     ← Transactions schema + init + append + last-row helpers
│   │   ├── currency.py         ← Frankfurter.app FX with on-disk cache
│   │   ├── month_builder.py    ← Formula builders + build_month_tab + force_month_recompute
│   │   ├── ytd_builder.py      ← YTD layout + formulas + build_ytd_tab
│   │   ├── year_builder.py     ← Bulk setup_year + ensure_*_tab
│   │   ├── gspread_backend.py  ← Real backend (lazy gspread import)
│   │   ├── factory.py          ← get_sheets_backend()
│   │   ├── exceptions.py       ← SheetsError hierarchy
│   │   └── data/sheet_format.yaml
│   ├── pipeline/             ← Chat → row writer (Step 5) + corrections (Step 7.1)
│   │   ├── logger.py           ← ExpenseLogger: FX + ensure_tab + append + recompute nudge
│   │   ├── correction.py       ← CorrectionLogger: undo / edit / re-FX / nudge
│   │   ├── reply.py            ← format_reply() — pure user-facing reply builder
│   │   ├── chat.py             ← ChatPipeline + ChatTurn
│   │   ├── factory.py          ← get_chat_pipeline / get_correction_logger
│   │   └── exceptions.py       ← PipelineError / ExpenseLogError / CorrectionError
│   └── telegram_app/         ← Telegram front-end (Step 7)
│       ├── auth.py             ← parse_allowed_users + Authorizer (no SDK imports)
│       ├── bot.py              ← MessageProcessor + CorrectionProcessor + handler factories
│       └── factory.py          ← build_application + run_polling
└── tests/                    ← 394 tests, all offline
    ├── conftest.py             ← isolated_env / fake_llm fixtures
    └── test_*.py               ← One per module, plus integration tests
```

### Module guide — bottom-up

#### `config.py` — Settings

A single `Settings` class loads every environment variable into a
typed model with `SecretStr` fields for keys. Validation happens at
import time, so a missing or malformed key fails fast with a clear
message.

#### `llm/` — provider-agnostic LLM client

`LLMClient` is a Protocol with two methods:
- `complete(messages, ...) -> LLMResponse` — free-form text out.
- `complete_json(messages, schema, ...) -> (parsed, LLMResponse)` —
  forces the model into JSON mode, injects the schema as a system
  grounding hint, parses + validates with Pydantic, raises
  `LLMBadResponseError` on unparseable output.

Every concrete client (Groq / Ollama / OpenAI / Anthropic / Fake)
implements this protocol. Switching providers is one env var.

`TracedLLMClient` is a decorator that wraps any client and writes one
line to `logs/llm_calls.jsonl` per round-trip, with prompt, response,
tokens, latency, and a `trace_id`. Trace failures *never* break the
user's chat — the wrapper logs a warning and returns the original
response unchanged.

#### `extractor/` — chat → typed action

Two-stage pipeline:

1. **`IntentClassifier`** picks one of:
   - `log_expense`
   - `query_period_total`
   - `query_category_total`
   - `query_day`
   - `query_recent`
   - `smalltalk`
   - `unclear`

2. **Stage 2** depending on intent:
   - `log_expense` → `ExpenseExtractor` → `ExpenseEntry`
   - any `query_*` → `RetrievalExtractor` → `RetrievalQuery`
   - `smalltalk` / `unclear` → no stage 2 call.

`Orchestrator.extract(text)` runs both stages and returns an
`ExtractionResult`. Every call also writes one `ConversationTurn` to
`logs/conversations.jsonl` with `trace_ids` linking back to the LLM
call records.

`CategoryRegistry` collapses LLM-emitted aliases ("groceries",
"shampoo", "tesla supercharge") back to canonical category names
("Groceries", "Shopping", "Tesla Car"). Defined in YAML; full taxonomy
in §9.

#### `sheets/` — Google Sheets layer

- `SheetsBackend` Protocol abstracts the spreadsheet client.
- `GspreadBackend` is the real implementation; `FakeSheetsBackend` is
  the in-memory test version.
- `transactions.py` defines the master ledger schema (14 columns,
  see §9), the `init_transactions_tab` setup, the `append_transactions`
  writer, and the `LastRow` helpers (`get_last_row`, `delete_last_row`,
  `update_last_row_fields`) used by `/undo` and `/edit`.
- `month_builder.py` builds monthly summary tabs entirely from
  formulas (SUMIFS / COUNTIFS / MAXIFS / EOMONTH) referencing
  `Transactions`. The `force_month_recompute` function rewrites
  summary + total formulas to bust Sheets' stale-formula cache after
  API-driven writes.
- `ytd_builder.py` builds the YTD dashboard (Monthly × Category
  grid + top-vendors block).
- `year_builder.py` orchestrates "set up an entire year" — 12 monthly
  tabs + 1 YTD.
- `currency.py` looks up FX rates via Frankfurter.app, caches them on
  disk in JSON, and falls back to the most recent cached rate if the
  API is down.

#### `pipeline/` — chat → row writer + corrections

- `ExpenseLogger.log(entry)` is the chat-side counterpart to
  `append_transactions`. It runs FX, ensures the right monthly tab
  exists, builds a `TransactionRow`, appends it, and triggers a
  recompute nudge on the affected monthly tab.
- `CorrectionLogger.{peek_last,undo,edit}` operates on the
  bottom-most `Transactions` row. Amount edits re-run FX so
  `Amount (USD)` stays consistent. Both `undo` and `edit` end with the
  same recompute nudge.
- `format_reply(turn)` is a pure function that produces the
  user-facing reply string for any `ChatTurn`. The same string is
  what the CLI prints and what Telegram sends.
- `ChatPipeline.chat(text)` orchestrates one full turn:
  classify → extract → log/error/skip → format reply → persist.

#### `telegram_app/` — Telegram front-end

- `Authorizer` checks the caller's user ID against
  `TELEGRAM_ALLOWED_USERS` before any LLM call.
- `MessageProcessor.process(user_id, text)` is pure-Python (no
  Telegram SDK imports) — easy to unit-test. It auth-gates, runs the
  chat pipeline on a worker thread, and returns a reply string.
- `CorrectionProcessor.process_{last,undo,edit}` is the parallel for
  `/last` / `/undo` / `/edit`. Same auth + same return shape.
- `bot.py` exposes `make_*_handler` factories that wrap the
  processors in `async def(update, context)` callbacks the Telegram
  SDK expects.
- `build_application(settings)` wires all of the above into a
  long-polling `telegram.ext.Application`.

### Test layout

Every module has a focused test file. Highlights:

- `test_pipeline_chat.py` — end-to-end pipeline run against
  `FakeLLMClient` + `FakeSheetsBackend`. No network, no flakes.
- `test_pipeline_correction.py` — every undo/edit branch including
  alias resolution, INR re-conversion, recompute-failure resilience.
- `test_telegram_correction.py` — `CorrectionProcessor` auth gating,
  reply formatting, `/edit` arg parser (single + multi-word
  categories, unparseable amounts).

Run them: `pytest`. All 394 tests are offline.

---

## 7. CLI reference — every flag

The CLI entry point is `expense` (or `python -m expense_tracker`). Every
flag below honours `--fake` to run offline.

### LLM smoke tests

```bash
expense --ping-llm           # one round-trip; prints reply + latency + tokens
expense --ping-llm --json    # same, but forces JSON mode against a tiny schema
expense --extract "spent 40 on coffee yesterday"
expense --extract "thanks!"
expense --extract "how much did I spend on food in April"
```

### Sheets management

```bash
expense --whoami             # show spreadsheet title + URL + service-account email
expense --list-sheets        # list every spreadsheet the service account can see
expense --init-transactions  # create the master Transactions tab (idempotent)
expense --reinit-transactions# wipe + recreate Transactions (after a schema change)
expense --build-month 2026-04
expense --rebuild-month 2026-04   # delete + recreate
expense --build-ytd 2026
expense --rebuild-ytd 2026
expense --setup-year 2026                       # 12 monthly tabs + YTD
expense --setup-year 2027 --hide-previous       # tucks 2026 monthlies away
expense --setup-year 2027 --overwrite           # destructive: rebuilds all 13 tabs
```

### Chat pipeline

```bash
expense --chat "spent 12.50 on coffee at starbucks today"
expense --chat "paid 499 RS for netflix"
expense --chat "thanks!"
expense --chat "how much did I spend on food in april?"   # answered in Step 6
```

### Correction (undo / edit) — Step 7.1

```bash
expense --undo                              # delete the bottom-most Transactions row
expense --edit-amount 50                    # change amount; FX re-runs automatically
expense --edit-category Shopping            # change category (aliases resolved)
expense --edit-amount 50 --edit-category Shopping   # combine
```

### Telegram bot

```bash
expense --telegram        # start the long-polling bot (Ctrl-C to stop)
expense --telegram --fake # offline mode — useful for testing the bot loop
```

---

## 8. Telegram reference — every command

```
[any plain text]            → log an expense
/start                      → show usage hint
/help                       → same as /start
/whoami                     → reply with caller's numeric Telegram user ID (works for everyone)
/last                       → show the most-recent expense, no changes
/undo                       → delete the most-recent expense; recompute monthly tab
/edit amount 50             → change the amount of the last row; re-run FX
/edit amount 50.25          → decimals OK
/edit category Food         → change the category of the last row
/edit category India Expense→ multi-word categories supported
```

All commands except `/whoami` require the caller's user ID to be in
`TELEGRAM_ALLOWED_USERS`. Unauthorized callers get a polite refusal
that includes their user ID, never trigger an LLM call, and never
touch the sheet.

---

## 9. Data model — what gets stored where

### 9.1 Master ledger: `Transactions` tab

One row per expense. Fixed schema, never resorted.

| # | Column | Key | Type | Example |
|--:|---|---|---|---|
| A | Date | `date` | ISO date | `2026-04-25` |
| B | Day | `day` | 3-letter | `Sat` |
| C | Month | `month` | full name | `April` |
| D | Year | `year` | 4-digit int | `2026` |
| E | Category | `category` | canonical name | `Saloon` |
| F | Note | `note` | string | `haircut` |
| G | Vendor | `vendor` | string | `Supercuts` |
| H | Amount | `amount` | number | `30.00` |
| I | Currency | `currency` | ISO 4217 | `USD` |
| J | Amount (USD) | `amount_usd` | number | `30.00` |
| K | FX Rate | `fx_rate` | number | `1.0` |
| L | Source | `source` | `chat` / `cli` / etc. | `chat` |
| M | Trace ID | `trace_id` | string | `tr_a1b2c3` |
| N | Timestamp | `timestamp` | ISO datetime | `2026-04-25T14:30:00` |

Design notes:

- **Date (column A) is the leftmost.** When you scan the sheet by eye,
  the most useful column is on the left.
- **Year is its own column.** Letting Sheets infer year from Date
  works in formulas, but having an explicit `Year` column makes the
  `YTD 2026 / YTD 2027` filters trivial.
- **Timestamp is far right** (column N), distinct from Date (column
  A). Date = the expense's calendar day; Timestamp = the moment the
  bot wrote the row. The gap surfaces backdated entries ("yesterday"
  said on the next day).
- **Conditional row banding** alternates by month, not by parity.
  Visual breaks without inserting separator rows that would break
  SUMIFS.

### 9.2 Monthly tab: `April 2026`, `May 2026`, …

100% formula-driven. Every cell references `Transactions`. Layout:

```
A1: "April 2026"   (title)
B4: =SUMIFS('Transactions'!J:J, 'Transactions'!D:D, 2026,
                                'Transactions'!C:C, "April")    ← monthly total USD
B5: =COUNTIFS(...)                                              ← number of transactions
B6: =B4 / DAY(EOMONTH(...))                                     ← avg / day
B7: =MAXIFS(...)                                                ← largest single

A9..A39: 1..30 (or 31, or 28/29) one row per day
B9..B39: =TEXT(A9, "ddd")                                       ← day-of-week
C..O   : 13 category columns, each =SUMIFS over Transactions
P9..P39: =SUM(C9:O9)                                            ← daily total
P40    : =SUM(P9:P39)                                           ← grand total
C40..O40: =SUM(Cx:Cy)                                           ← per-category totals

(Below the daily grid, per-category breakdowns by note —
 top-N notes per category by amount.)
```

Each category column is a SUMIFS that defaults to 0. **Visual
emphasis** is conditional: cells with value > 0 get a bold dark blue;
cells with value == 0 get a low-contrast gray. Result: a normal day
fades; a day with real spending pops.

### 9.3 YTD dashboard: `YTD 2026`

Annual rollup. Layout:

```
A1: "YTD 2026"
B4..B7: year-summary block (total / transactions / avg / largest)

(Monthly × Category grid)
A10: "Month"
B10..N10: 13 categories
O10: "Total"
A11..A22: "January" .. "December"
B11..N22: each cell =SUMIFS over Transactions for that (month, category)

(Top vendors block — top N vendors by amount this year)
```

### 9.4 Categories — the 13 canonical names

Defined in `src/expense_tracker/extractor/data/categories.yaml`:

| Canonical name | Hint shown to LLM | Example aliases |
|---|---|---|
| Groceries | grocery shopping at supermarkets, vegetables, fruits | costco, walmart, kroger, fruits, vegetables |
| House | rent, mortgage, utilities, internet, repairs | rent, electric, water, comcast, repairs |
| Food | dining out, restaurants, takeout, coffee, juice | restaurant, takeout, coffee, latte, ice cream |
| Party | parties, drinks, alcohol, entertainment | bar, beer, wine, club, party |
| Medicines | prescriptions, doctor visits, medicines, vitamins | pharmacy, doctor, prescription, medicine |
| Shopping | clothing, electronics, household goods, **personal-care PRODUCTS** (shampoo, soap, toothpaste, deodorant, cosmetics) | clothes, shoes, electronics, **shampoo**, soap, toothpaste, makeup |
| Movies | cinema, streaming, entertainment subscriptions | cinema, movie, netflix, hbo |
| Saloon | salon SERVICES (haircut, facial, manicure, waxing) — NOT physical products | haircut, salon, spa, facial, manicure, waxing |
| India Expense | money spent in India / on Indian-specific things | india, gpay, upi |
| Travelling | flights, hotels, gas while traveling, rental cars | flight, hotel, uber, lyft, gas |
| Miscellaneous | catch-all (default fallback) | misc, other, unknown |
| Digital | software subs, cloud, apps, books, courses | aws, github, gpt, claude |
| Tesla Car | Tesla charging, FSD, Premium Connectivity, insurance | tesla, supercharger, fsd |

If the LLM emits something unrecognized, it falls back to
`Miscellaneous`.

The Saloon vs Shopping distinction was tightened deliberately after
"shampoo" was being miscategorized. The rule: **if you bought a
product, it's Shopping; if you paid for a service, it's Saloon.**

---

## 10. Multi-currency — how FX works

Primary currency is USD. Any other ISO-4217 code (INR, EUR, GBP, JPY,
…) is converted on the way in.

### Flow

1. LLM extracts `{amount: 1500, currency: "INR", date: 2026-04-25}`.
2. `CurrencyConverter.convert(amount=1500, from_currency="INR",
   on_date=2026-04-25)`:
   - Check on-disk JSON cache (`.fx_cache.json` by default).
   - Cache miss → hit Frankfurter:
     `https://api.frankfurter.app/2026-04-25?from=INR&to=USD`.
   - Cache hit → use the cached rate.
   - API down + no cache → return `CurrencyError`. The chat layer
     catches this and replies with a graceful "couldn't convert" so
     the user can retry later.
3. The row gets written with both `Amount = 1500.00 INR` and
   `Amount (USD) = ~$18.00 USD` and `FX Rate = 0.012`.

### Why Frankfurter

- Free, no API key, no signup, no rate limit relevant for personal
  use.
- ECB-derived (European Central Bank), so rates are reliable
  reference rates.
- Returns historical rates by date — important for backdated
  entries.

### Caching semantics

- One JSON file. Keyed by `(date, from, to)`. Never grows beyond a
  few hundred KB even after years of use.
- Stale rates are *fine* for personal use — the ECB updates daily,
  and you're not arbitraging.
- Edits via `/edit amount X` re-run the conversion against the
  *original row's date and currency*. So fixing a typo doesn't
  silently change the FX rate — the historical rate stays consistent.

---

## 11. Logs & observability

Two append-only JSONL files under `logs/` (configurable via `LOG_DIR`):

| File | One line per | Written by |
|---|---|---|
| `logs/llm_calls.jsonl` | LLM round-trip | `TracedLLMClient` (auto, when `LLM_TRACE=true`) |
| `logs/conversations.jsonl` | User-bot turn | `Orchestrator.persist_turn` |

A single user message can produce multiple LLM calls (intent
classification + extraction + reply), so they're 1-to-many.
`ConversationTurn.trace_ids` links a turn back to the LLM calls that
produced it — debugging a wrong answer is one `jq` away:

```bash
jq 'select(.session_id == "s_abc123")' logs/conversations.jsonl logs/llm_calls.jsonl
```

### Schemas (excerpt)

```jsonl
# logs/llm_calls.jsonl
{"schema_version":1,"ts":"2026-04-25T20:09:00Z","trace_id":"tr_a1b2c3",
 "provider":"groq","model":"llama-3.1-8b-instant","json_mode":true,
 "schema_name":"_PingResult","messages":[...],"response":"...",
 "prompt_tokens":225,"completion_tokens":17,"total_tokens":242,
 "latency_ms":304.4,"outcome":"ok"}

# logs/conversations.jsonl
{"schema_version":1,"ts":"2026-04-25T20:09:00Z","session_id":"s_x9y8",
 "user_text":"spent 40 on food today","intent":"log_expense",
 "extracted":{"date":"2026-04-25","category":"Food","amount":40},
 "action":{"type":"sheets_append","sheet":"April 2026","row":25,"status":"ok"},
 "bot_reply":"Logged $40 to Food on Sat 25 Apr.",
 "trace_ids":["tr_a1b2c3"]}
```

Every line carries `schema_version` so future readers can migrate
cleanly. Trace failures **never** break the user's chat — the wrapper
logs a warning and returns the LLM response unchanged.

---

## 12. Self-healing & corrections

Two things ship under this banner:

### 12.1 Recompute nudge — fix Google Sheets stale-cache

**The problem.** When you append a row to `Transactions` via the
Sheets API, dependent monthly-tab cells (which are SUMIFS against
`Transactions`) sometimes don't re-evaluate immediately. The data is
correct in `Transactions`; the monthly grid shows yesterday's state.
This is a known Sheets quirk specific to API-driven writes — UI
edits never trigger it.

**The fix.** Every successful log call ends with a "nudge":
`force_month_recompute(year, month, categories)` rewrites the four
summary formulas (B4:B7) and the daily-total row formulas on the
affected monthly tab. Sheets then re-evaluates the entire dependency
graph and the daily grid catches up.

The nudge is **best-effort**. If it fails (rate limit / network
blip), the user-visible operation (the append) is still reported
successful. The failure shows up in logs for follow-up.

### 12.2 `/undo` and `/edit` — fix the most recent expense

The bottom-most row of `Transactions` is, by convention, always the
most recently appended expense. Three operations target it:

#### `/undo`

Deletes the bottom-most row. Returns a snapshot of the deleted row so
the reply can show what disappeared. Triggers the nudge on the
affected monthly tab so totals re-evaluate.

#### `/edit amount X`

Patches the `Amount` column in place. Re-runs FX against the
*original row's currency and date* so `Amount (USD)` and `FX Rate`
stay consistent:

- If the row was `1500 INR` on `2026-04-25`, `/edit amount 2000`
  produces a fresh `Amount = 2000 INR`, looks up the INR→USD rate
  for `2026-04-25` (cached), and writes a new `Amount (USD)`.
- USD identity is special-cased: `Amount = $50` always means
  `Amount (USD) = $50` and `FX Rate = 1.0`.

Triggers the nudge. Rejects non-positive amounts before touching the
sheet.

#### `/edit category Y`

Patches the `Category` column. The new value is canonicalized through
the `CategoryRegistry` so aliases like "groceries" resolve to
"Groceries". Multi-word names like "India Expense" round-trip intact.

Triggers the nudge.

### Why "bottom-most row" instead of "row with ID X"

Personal-scale tracking: the user-facing concept of "the last
expense" is unambiguous and matches what humans naturally remember.
Indexing by row ID would force the user to memorize numbers. The
trade-off: you can only undo/edit the most recent, not arbitrary
historical rows. For non-trivial historical edits, edit the sheet
directly in the Sheets UI.

---

## 13. Daily workflow cookbook

### Logging an expense

Phone: open Telegram, DM the bot, type naturally.

```
spent 40 on coffee today
1500 INR groceries yesterday at Costco
bought a tesla supercharge for $12.50
paid $130 for haircut and beard trim today at supercuts
```

Bot replies inline within ~1s; the row lands in the sheet.

### Fixing a wrong entry

```
You    : got shampoo for my wife which cost 100$
Bot    : Logged $100 to Saloon on Sun 26 Apr.
You    : /edit category Shopping
Bot    : Updated last expense:
          Category : Saloon -> Shopping
         Refreshed `April 2026` so totals stay in sync.
```

Or to delete entirely:

```
/undo
```

### Monthly review (manual, pre-Step 6)

Open the sheet → switch to the current monthly tab. Daily grid shows
exactly what was spent on what day. Summary block at the top shows
total / count / avg / largest. Per-category breakdowns below the grid
show top notes per category.

(Step 6 will bring this same data via chat: "how much for food this
month?" / "what was my biggest expense in April?")

### Year rollover

When 2027 starts:

```bash
expense --setup-year 2027 --hide-previous
```

The `--hide-previous` flag tucks all 2026 monthly tabs into the
hidden-tabs section so the sheet's tab strip stays manageable.
History is preserved; you can unhide any tab from the Sheets UI.

---

## 14. Troubleshooting

### "PermissionError: caller does not have permission" / "spreadsheet not found"

The service account can't see the sheet. Re-do §3.3.2: open the
sheet, **Share**, paste the *exact* service-account email, set Editor,
**untick** Notify, hit Share.

### "TELEGRAM_BOT_TOKEN is set but empty"

You probably copied the variable name without the value. Open `.env`
and confirm there's a real token after the `=`.

### Bot replies "Sorry, you're not authorized" to your own messages

You haven't added your numeric user ID to `TELEGRAM_ALLOWED_USERS`.
The bot's reply includes your ID — copy-paste it into `.env`,
restart `expense --telegram`.

### Chat works but nothing appears on the monthly tab

Two possible causes:

1. **Sheets stale cache.** Should be auto-fixed by the recompute
   nudge as of Step 7.1. If it still happens, run
   `expense --rebuild-month 2026-04` once. If it happens repeatedly,
   open an issue.
2. **Wrong year/month inferred.** The LLM sometimes resolves "April"
   ambiguously near year boundaries. Check the row in `Transactions`
   — its `Year` and `Month` columns are the source of truth.

### "shampoo" still landing under Saloon

This was fixed in Step 7.1 (categories.yaml refinement). If you see
it again, check that `categories.yaml` has the latest aliases under
`Shopping`. The fix was: move all personal-care PRODUCTS to Shopping;
keep only services in Saloon.

### "APIError: [429]: Quota exceeded" while running CLI commands

You're hitting Google Sheets API rate limits (typically 60 reads or
60 writes per minute per user per project). Wait ~60 seconds and
retry. For bulk operations like `--setup-year`, this is expected and
the retries handle it transparently.

### Telegram bot freezes / stops responding

Check the terminal where `expense --telegram` is running. The most
common cause is a transient network failure that
`python-telegram-bot` handles with backoff. If the process has fully
exited, restart it. To run 24/7, see §16.

### "'Year' column displays as 2,026.00 instead of 2026"

Schema-format issue, fixed in Step 5.1. If you see it, run
`expense --reinit-transactions`. **Warning: this wipes the
Transactions tab** — back up first if the existing rows matter.

---

## 15. Maintenance — backup, year rollover, schema migration

### Backups

The Google Sheet *is* the database. Google maintains a full version
history (File → Version history → See version history). For belt and
braces:

- Periodically download a copy: File → Download → Microsoft Excel
  (`.xlsx`).
- The `logs/conversations.jsonl` file is also a complete log of every
  expense ever logged via the bot — append-only, plain text. Save a
  copy with the spreadsheet export.

### Year rollover

```bash
expense --setup-year YYYY --hide-previous
```

Builds 12 monthly tabs + the YTD tab for the new year, hides last
year's monthly tabs to keep the tab strip clean. All historical data
stays intact in `Transactions`.

### Schema migration

If a future change adds a column to `Transactions`:

```bash
expense --reinit-transactions
```

This **wipes** Transactions (header + all rows) and rewrites the
header in the new schema. **Always back up first** — the operation is
destructive by design.

For non-destructive column additions, edit `transactions.py` directly
to append a new column at the end and the existing rows stay valid
(empty value in the new column).

---

## 16. Roadmap

| # | Step | Status |
|--:|---|---|
| 1 | Scaffold | done |
| 2 | LLM client (Groq / Ollama / OpenAI / Anthropic) | done |
| 2.5 | Chat history + tracing (JSONL) | done |
| 3 | Extractor pipeline (intent → extract) | done |
| 4 | Sheets foundation (Transactions + monthly + YTD) | done |
| 5 | Chat → row writer | done |
| 5.1 | Schema reorder + visual emphasis polish | done |
| 6 | **Sheets reader + retrieval queries** | **next** |
| 7 | Telegram bot front-end | done |
| 7.1 | Self-healing + corrections (`/undo`, `/edit`) | done |
| 8 | Polish (multi-turn clarification, weekly summaries) | pending |
| ∞ | Hosting (Oracle Cloud Free Tier) | pending Noah's response |

### What "sellable" would require (out of current scope)

Currently single-user. To turn this into a SaaS:

- **Multi-tenant Google access.** Replace the service-account flow
  with per-user OAuth (each user authorizes the app on their own
  sheet).
- **Web onboarding.** A guided flow that creates the user's sheet,
  connects their Telegram, and writes their `.env` equivalent into a
  per-user config row.
- **Per-user category config.** `categories.yaml` is currently the
  operator's personal taxonomy. Each user would need their own.
- **Billing.** Stripe + license-key gate.

Estimate: ~2 weeks of work, mostly UI + onboarding, not core logic.

---

## 17. Glossary

| Term | Meaning |
|---|---|
| **Service account** | A Google identity that's a robot, not a human. Authenticates via JSON key, no password / 2FA. |
| **Spreadsheet ID** | The long token in a Sheets URL between `/d/` and `/edit`. |
| **BotFather** | Telegram's official bot for creating new bots. Username `@BotFather`. |
| **Bot token** | Telegram bot's password. Format `<digits>:<base64ish>`. |
| **Telegram user ID** | A user's unique numeric ID across Telegram. Different from username. |
| **Allow-list** | The comma-separated `TELEGRAM_ALLOWED_USERS` env var. Empty = nobody allowed. |
| **Long-polling** | Bot pulls updates from Telegram; no public URL needed. Alternative is webhooks. |
| **Frankfurter** | Free FX rate API at `frankfurter.app`. ECB-derived, no key. |
| **Trace ID** | Per-LLM-call random ID. Links a chat turn to the prompts that produced it. |
| **Session ID** | Per-user-message ID. One session ID can span multiple LLM calls (intent + extract + reply). |
| **JSONL** | One JSON object per line. Append-friendly, atomic at line granularity. |
| **SUMIFS / COUNTIFS / MAXIFS** | Google Sheets aggregation formulas. The monthly + YTD tabs are built entirely out of these. |
| **Stale-formula cache** | A Sheets quirk where API writes don't always invalidate dependent formula evaluations. The recompute nudge fixes this. |
| **Recompute nudge** | Bot-side rewrite of summary + total formulas after a Transactions append/edit/delete, forcing Sheets to re-evaluate. |
| **Canonical category** | The display-form of a category (`"Groceries"`). What gets written to the sheet. |
| **Alias** | A user-facing word that resolves to a canonical category (`"costco"` → `"Groceries"`). |

---

*This document is the single source of truth for the project's setup
and architecture. When code changes, update this file in the same
commit. Generated Word version (`HANDBOOK.docx`) is rebuilt from this
markdown — never edit the `.docx` by hand.*
