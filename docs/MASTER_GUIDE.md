# Expense Tracker Bot — Master Guide

> **Status:** Live in production · **Last updated:** 2026-06-26 ·
> **Author:** built collaboratively by Vinay + AI pair-programmer over multi-week sessions.
>
> This is the single source of truth for *what we built, how it's wired,
> and what every knob does*. It's intentionally exhaustive — if you
> forgot a password, lost the laptop, or onboarded a teammate, this
> document gets you (or them) from zero back to a fully running 24/7
> bot.
>
> Three other docs live alongside this one:
>
> | File | Audience | When to read |
> |------|----------|--------------|
> | `MASTER_GUIDE.md` (this) | You, six months from now | Anytime you need ground truth |
> | `HANDBOOK.md` | New contributor | Wants the long-form pedagogical tour |
> | `ARCHITECTURE.md` | Code reviewer | Wants the module-by-module map |
> | `WAKE_UP.md` | You, tomorrow morning | The 5-minute "what was I doing" recap |

---

## Table of contents

1. [What we built (60-second pitch)](#1-what-we-built-60-second-pitch)
2. [System architecture (with diagram)](#2-system-architecture-with-diagram)
3. [Every account & platform we set up](#3-every-account--platform-we-set-up)
4. [Every variable in one master table](#4-every-variable-in-one-master-table)
5. [Code architecture — module map](#5-code-architecture--module-map)
6. [End-to-end request flow](#6-end-to-end-request-flow)
7. [Chronological journey — what we built when, and why](#7-chronological-journey--what-we-built-when-and-why)
8. [Daily operations cookbook](#8-daily-operations-cookbook)
9. [Operational runbook — when X breaks, do Y](#9-operational-runbook--when-x-breaks-do-y)
10. [What's built but NOT active (Postgres mirror)](#10-whats-built-but-not-active-postgres-mirror)
11. [Glossary](#11-glossary)
12. [FAQ](#12-faq)
13. [Change log & lessons learned](#13-change-log--lessons-learned) *(new — covers every prod change since launch with the reasoning behind it)*

---

## 1. What we built (60-second pitch)

**A 24/7 personal expense tracker** that you talk to from any
Telegram client (phone, laptop, web). Plain English in, structured
spreadsheet rows out. No buttons, no forms, no app to install.

You text the bot:

```
today I spent 40 dollars for body massage, add this to shopping
```

The bot replies in ~5 seconds:

```
✅ Logged: $40.00 Shopping (body massage) on 2026-05-04
   Today's total: $40.00 · May 2026 total: $40.00
```

And in the background:

* The LLM (Groq's Llama-3.1-8b) parses the message.
* The bot writes one row to your Google Sheets master ledger
  (`Transactions` tab).
* The "May 2026" tab updates its formulas instantly (it's all
  `SUMIFS` against the master ledger — never copy-pasted).
* Your YTD dashboard updates too.
* Every LLM call + every conversation turn is logged to JSONL files
  inside the container for debugging.

You can also ask it questions:

```
how much did I spend on food in April?
how much for shopping this week?
show last 5 transactions
weekly summary
monthly summary
```

Or undo a mistake:

```
/last
/undo
/edit amount 50
/edit category Food
```

The whole thing runs on **Render Free** (24/7 container), pinged
every 5 min by **UptimeRobot** so it never spins down.

**Cost: $0/month forever** (within free-tier limits, which we're well
inside).

---

## 2. System architecture (with diagram)

### Mermaid diagram (renders on GitHub)

```mermaid
flowchart LR
    User["You<br/>(Telegram on phone)"] -->|text msg| TG["Telegram<br/>(MTProto cloud)"]
    TG -->|long-poll getUpdates| RB[["Render container<br/>expense-tracker-bot<br/>(24/7 polling)"]]

    subgraph Render["Render Free (Oregon/Ohio data center)"]
      direction TB
      RB --> HS["Health server<br/>:$PORT/health -> 200 ok"]
      RB --> PIPE["ChatPipeline<br/>(intent -> extract -> ledger)"]
      PIPE --> LLM["LLM client<br/>(Groq Llama-3.1-8b)"]
      PIPE --> LED["Ledger backend<br/>(SheetsLedgerBackend)"]
    end

    LLM -->|HTTPS, JSON mode| GROQ["Groq Cloud<br/>api.groq.com"]
    LED -->|gspread, OAuth2 service-account| GS["Google Sheets<br/>'Daily Expense 2026'<br/>(Transactions + monthly tabs + YTD)"]
    PIPE -->|currency conversion| FX["Frankfurter.app<br/>(free FX rates)"]

    UR["UptimeRobot<br/>(3 US data centers)"] -.->|HEAD /health<br/>every 5 min| HS

    GH["GitHub<br/>vinay3093/expense-tracker-bot"] -->|git push to main<br/>auto-deploy webhook| Render
    GHA["GitHub Actions<br/>(CI tests + backup keep-alive)"] -.->|tests on push| GH

    style RB fill:#1a4480,color:#fff
    style GS fill:#0f9d58,color:#fff
    style GROQ fill:#ff6f00,color:#fff
    style UR fill:#3aa757,color:#fff
    style TG fill:#229ed9,color:#fff
```

### ASCII fallback (in case Mermaid doesn't render)

```
                 ┌──────────────────┐
                 │  You (phone)     │
                 │  Telegram app    │
                 └────────┬─────────┘
                          │ text msg
                          ▼
                 ┌──────────────────┐
                 │  Telegram cloud  │  api.telegram.org
                 │  (MTProto)       │
                 └────────┬─────────┘
                          │ long-poll (getUpdates)
                          │ every ~10 sec
                          ▼
        ┌────────────────────────────────────────────────┐
        │  RENDER CONTAINER  (24/7 Docker, Free tier)    │
        │                                                │
        │   ┌──────────────────────┐                     │
        │   │  HealthServer :PORT  │◄──── HEAD /health ──┼─── UptimeRobot
        │   │  /     -> "alive"   │      every 5 min    │  (3 US regions)
        │   │  /health -> "ok"     │                     │
        │   └──────────────────────┘                     │
        │                                                │
        │   ┌──────────────────────┐                     │
        │   │  Telegram bot loop   │                     │
        │   │  (python-telegram-   │                     │
        │   │   bot v21, polling)  │                     │
        │   └──────────┬───────────┘                     │
        │              │                                 │
        │              ▼                                 │
        │   ┌──────────────────────┐                     │
        │   │  ChatPipeline        │                     │
        │   │  - allow-list check  │                     │
        │   │  - intent classify   │──┐                  │
        │   │  - extract           │  │                  │
        │   │  - write             │  │                  │
        │   │  - format reply      │  │                  │
        │   └──────────┬───────────┘  │                  │
        │              │              │ JSONL trace      │
        │              │              ▼                  │
        │              │     /app/logs/llm_calls.jsonl   │
        │              │     /app/logs/conversations.jsonl
        │              │                                 │
        └──────────────┼─────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
   ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
   │  Groq   │   │  Google  │   │Frankfurter│  │  GitHub  │
   │ (LLM)   │   │  Sheets  │   │  (FX)    │   │ (deploy) │
   │         │   │          │   │          │   │          │
   │api.groq │   │service-  │   │free, no  │   │push to   │
   │.com     │   │account   │   │auth      │   │main →    │
   │         │   │OAuth2    │   │needed    │   │Render    │
   └─────────┘   └──────────┘   └──────────┘   │webhook   │
                                                └──────────┘
```

### What each layer is responsible for

| Layer | Responsibility | Failure behaviour |
|-------|----------------|-------------------|
| Telegram | Message transport. Queues messages while bot is offline. | If Telegram itself is down, you can't reach the bot. (Rare.) |
| Render container | Runs the 24/7 Python process: health server + bot polling loop. | If container crashes, Render auto-restarts. If Render itself is degraded, it'll show on https://status.render.com |
| Health server | Answers `GET /` and `HEAD /health` so platform probes + UptimeRobot keep happy. | If port doesn't bind, container fails to start (Render shows red deploy). |
| ChatPipeline | The actual brain — orchestrates intent classify, extraction, ledger write, reply formatting. | Wrapped in `try/except` per handler — errors logged + apology sent to user, polling loop preserved. |
| LLM (Groq) | Turns "spent 40 on coffee" into `{amount: 40, category: Food, ...}` JSON. | Auto-retries 3x on rate-limit/timeout via tenacity. |
| Sheets backend | Appends row to `Transactions`, ensures monthly tab exists, formats. | 429 quota errors caught → user-friendly reply, polling preserved. |
| Frankfurter (FX) | Converts INR/EUR/etc. → USD using free public API. Local JSON cache. | If API down → cached rate used → if no cache → write fails with clear error. |
| UptimeRobot | External cron pinging `/health` every 5 min so Render never reaches the 15-min idle spin-down threshold. | If UptimeRobot fails, you get email; bot still serves traffic until it spins down. |
| GitHub | Code home + auto-deploy trigger to Render on push to `main`. CI runs the test suite on every push. | If push fails, you stay on previous deploy (Render keeps last good build). |

---

## 3. Every account & platform we set up

This is the complete external-account inventory. If you ever need to
recreate the bot from scratch on a new laptop, work through this
section in order — every account here is required (except Supabase,
which is for the optional mirror mode in §10).

### 3.1 Telegram (BotFather)

* **What:** A chat-app bot that you control via the BotFather meta-bot.
* **Cost:** Free, forever, no card.
* **What we created:**
  * Bot username: `@svk_expense_bot`
  * Bot display name: `Expense Tracker`
  * **Bot token** (treat as password — anyone with this can impersonate the bot):
    `8702975628:AAF2Q_sJS0-8T6F-NHzwS4f8eloCL1QIfz8`
    (Stored as Render env var `TELEGRAM_BOT_TOKEN`.)
  * Allow-listed Telegram user ID: `8684705854` (your account)
    (Stored as Render env var `TELEGRAM_ALLOWED_USERS`.)
* **How to recreate:**
  1. Open Telegram, search `@BotFather`, send `/newbot`.
  2. Pick a display name and a username ending in `_bot`.
  3. Copy the token BotFather gives you.
  4. To find your own user ID: temporarily set `TELEGRAM_ALLOWED_USERS=` (empty),
     start the bot, DM it `/whoami`, and it'll reply with your numeric ID.
* **Where it's used in code:** `src/expense_tracker/telegram_app/factory.py`

### 3.2 Google Cloud Platform (service account)

* **What:** A non-human Google account ("service account") whose
  credentials your bot uses to read/write your Google Sheet.
* **Cost:** Free for our usage (well below GCP's free tier of ~5M reads/month).
* **What we created:**
  * GCP project (any name; we used the default sandbox project).
  * Two APIs enabled in that project:
    * **Google Sheets API**
    * **Google Drive API** (needed for `gspread.open_by_key`)
  * One **service account** (e.g. `expense-bot-svc@<project>.iam.gserviceaccount.com`).
  * One **JSON key** for that service account, downloaded once.
    (Stored as Render env var `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT` —
    pasted as the **full single-line JSON blob**, not a file path.)
* **How to recreate:**
  1. Go to https://console.cloud.google.com.
  2. Create a project (or use an existing one).
  3. APIs & Services → Library → enable "Google Sheets API" and "Google Drive API".
  4. APIs & Services → Credentials → Create Credentials → Service Account.
  5. Give it a name (e.g. `expense-bot-svc`), no roles needed at GCP level.
  6. Open the new service account → Keys tab → Add Key → JSON → Download.
  7. Open your Google Sheet, click Share, paste the service account's
     email (`...iam.gserviceaccount.com`), give it **Editor** access.
* **Security note:** If you ever leak this JSON (e.g. paste it in chat
  by accident — happened once during this build), immediately go to
  the GCP Console → IAM & Admin → Service Accounts → Keys → delete
  the leaked key, then create a new one. The old key stops working
  the moment you delete it.

### 3.3 Google Sheets (your spreadsheet)

* **What:** The spreadsheet the bot writes to. Mobile-first, you can
  open it on your phone in the Sheets app.
* **Cost:** Free with any Google account.
* **What we created:**
  * One spreadsheet titled `Daily Expense 2026`.
  * Three kinds of tabs the bot manages automatically:
    * **`Transactions`** — append-only master ledger. One row per
      expense. Columns: `date · category · amount · currency ·
      amount_usd · vendor · note · source · raw_text · created_at`.
    * **`<Month> <Year>`** (e.g. `April 2026`, `May 2026`) — formula-
      driven monthly grid. One row per day, one column per category,
      formulas (`SUMIFS`) reference `Transactions`. Bot auto-creates
      these on first expense in a new month.
    * **`<Year> Summary`** (e.g. `2026 Summary`) — YTD dashboard with
      monthly totals and category breakdowns. Auto-created.
  * **Spreadsheet ID** (the long token in the URL between `/d/` and `/edit`):
    Stored as Render env var `EXPENSE_SHEET_ID`.
* **How to recreate:**
  1. Create a new blank Sheet in your Drive.
  2. Copy the ID from the URL.
  3. Share it with the service account email from §3.2 (Editor access).
  4. Run `expense --init-transactions` once locally to create the
     master ledger headers — or just send the bot its first expense
     and it will create everything itself.

### 3.4 Groq (LLM provider)

* **What:** Free, fast inference for open-weights LLMs (Llama, Mixtral, Gemma).
* **Cost:** Free tier — 30 req/min, 6000 tokens/min. Vastly more than we use.
* **What we created:**
  * Account at https://console.groq.com.
  * One API key (starts with `gsk_...`).
    (Stored as Render env var `GROQ_API_KEY`.)
  * Default model: `openai/gpt-oss-20b` (cheap, fast, very reliable on
    structured-JSON extraction). Migrated 2026-06-25 from
    `llama-3.1-8b-instant`, which Groq deprecated with hard
    decommission on 2026-08-16.
* **How to recreate:**
  1. Sign up at https://console.groq.com (Google login is fine).
  2. Settings → API Keys → Create API Key.
  3. Copy and store the key — you can't see it again after closing.
* **Why Groq over OpenAI/Anthropic:** Free, fast, and the bot only
  uses it for *extraction* (which any small open-weights model
  nails) — we don't need GPT-4-class reasoning. If you ever want to
  swap, set `LLM_PROVIDER=openai` (or `anthropic`) and add the
  appropriate API key — zero code changes.
* **Model survivorship — important reality check:** Groq deprecates
  models with surprising frequency (8B Llama dropped 7 weeks notice
  on us in June 2026). The `LLMClient` Protocol means swapping in a
  new model is one env-var (or one default change in `config.py`),
  but you have to actually NOTICE the deprecation email. See §13.4
  for the full story and §9.10 for the "Groq said they're killing
  our model" runbook.

### 3.5 GitHub (code home + deploy trigger)

* **What:** Public Git repo where all code lives. Every push to `main`
  triggers a Render auto-deploy.
* **Cost:** Free (public repos have unlimited Actions minutes).
* **What we created:**
  * Repo: https://github.com/vinay3093/expense-tracker-bot
  * Default branch: `main`
  * Two GitHub Actions workflows:
    * `.github/workflows/ci.yml` — runs the test suite on every push.
    * `.github/workflows/keep-render-alive.yml` — backup keep-alive
      that pings Render every 14 min via cron. (Backup to UptimeRobot.)
  * One **repository variable** (not secret — it's just a URL):
    * `RENDER_SERVICE_URL` = `https://expense-tracker-bot-z558.onrender.com`
* **How to recreate:**
  1. Create a new GitHub repo.
  2. `git push -u origin main` from your local clone.
  3. Add the variable: Settings → Secrets and variables → Actions →
     "Variables" tab → New repository variable.
* **Optional secrets (only needed if you want Telegram alerts on
  keep-alive failure):**
  * `KEEPALIVE_TG_BOT_TOKEN` — same token as `TELEGRAM_BOT_TOKEN`
  * `KEEPALIVE_TG_CHAT_ID` — your Telegram user ID

### 3.6 Render (24/7 host)

* **What:** PaaS that runs Docker containers. Free tier gives you
  one container, 512 MB RAM, 0.1 CPU, sleeps after 15 min of no HTTP
  traffic (which we sidestep with UptimeRobot pings).
* **Cost:** Free, forever. (Requires a credit card on file for fraud
  prevention — Stripe holds $1 for verification then releases it.
  No actual charge for free tier.)
* **What we created:**
  * Account at https://dashboard.render.com.
  * One **Web Service** named `expense-tracker-bot`:
    * Runtime: **Docker** (not Native — uses our `Dockerfile`).
    * Plan: **Free**.
    * Region: **Ohio** (nearest to Frisco, TX).
    * Branch: `main`.
    * Auto-deploy: **Yes** (on every push to `main`).
    * Health check path: `/health`.
    * Public URL: `https://expense-tracker-bot-z558.onrender.com`
  * 8 environment variables (all marked **encrypted**):
    * `TELEGRAM_BOT_TOKEN`
    * `TELEGRAM_ALLOWED_USERS`
    * `GROQ_API_KEY`
    * `EXPENSE_SHEET_ID`
    * `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT` (full single-line JSON)
    * `LLM_PROVIDER` = `groq`
    * `TIMEZONE` = `America/Chicago`
    * `DEFAULT_CURRENCY` = `USD`
* **How to recreate:**
  1. Sign up at https://dashboard.render.com.
  2. Click `New +` → `Web Service`.
  3. Connect your GitHub account, select the `expense-tracker-bot` repo.
  4. Pick **Docker** runtime, **Free** plan, **Ohio** region, branch
     `main`. Health check path `/health`.
  5. Click "Advanced" → set the 8 env vars above. Mark all as
     encrypted.
  6. Click "Create Web Service". First build takes ~5 min.
  7. Once green "Live" badge appears, copy the public URL — you'll
     need it for §3.7.
* **Render-specific gotchas (all already handled in code, but FYI):**
  * The container **must** bind to `$PORT` (Render injects this env
    var; we read it in `health_server.py`).
  * Public URL contains a random 4-char suffix that **changes if you
    delete and recreate the service** (e.g. `-z558` → `-zb68`). It
    does NOT change on regular redeploys.
  * Free tier sleeps after 15 min of no HTTP. UptimeRobot pings
    every 5 min sidestep this.

### 3.7 UptimeRobot (24/7 keep-alive)

* **What:** External uptime monitor that pings your service from 3
  geographic regions on a real distributed cron.
* **Cost:** Free forever, 50 monitors max, 5-min interval minimum.
* **What we created:**
  * Account at https://uptimerobot.com.
  * One **HTTP(s) monitor** named `Render expense bot keep-alive`:
    * URL: `https://expense-tracker-bot-z558.onrender.com/health`
    * Interval: **5 minutes**
    * Method: **HEAD** (free-tier locked — that's why we added
      `do_HEAD` to our health server)
    * Alert: email to your account address on 2 consecutive failures.
* **Why UptimeRobot over the GitHub Actions cron:** GitHub Actions
  cron is "best-effort" and notoriously skips runs for low-traffic
  public repos (we observed this — zero scheduled runs in 2.5 hours
  during this build). UptimeRobot uses a real distributed cron and is
  the tool Render's own docs recommend.
* **How to recreate:**
  1. Sign up at https://uptimerobot.com (email + password, no card).
  2. Verify email.
  3. `+ New monitor` → HTTP(s) → paste the URL → set 5 min interval.

### 3.8 (Optional) Supabase — for Postgres mirror mode

Not currently active. See §10 for full setup if you ever want SQL
analytics over your expense history.

---

## 4. Every variable in one master table

This is the complete, authoritative list of every configuration knob
across every platform. If a variable name appears multiple places
(e.g. once in local `.env` for dev, once in Render env vars for prod),
it's listed once with a "Where set" column showing all locations.

### 4.1 Production (Render env vars)

These are the variables required for the live 24/7 bot. All marked
**encrypted** in Render's dashboard so they're hidden in logs.

| # | Variable | Required | Example value | What it does |
|---|----------|----------|---------------|--------------|
| 1 | `TELEGRAM_BOT_TOKEN` | yes | `8702975628:AAF...` | Authenticates the bot to Telegram. From BotFather. |
| 2 | `TELEGRAM_ALLOWED_USERS` | yes | `8684705854` | Comma-sep list of Telegram user IDs allowed to chat. Empty = nobody allowed. |
| 3 | `GROQ_API_KEY` | yes | `gsk_...` | Authenticates LLM calls to Groq. From Groq console. |
| 4 | `EXPENSE_SHEET_ID` | yes | `1AbC2dEf...` | Long ID from your Sheet's URL (between `/d/` and `/edit`). |
| 5 | `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT` | yes | `{"type":"service_account",...}` | Full single-line JSON of the GCP service account key. NOT a file path. |
| 6 | `LLM_PROVIDER` | yes | `groq` | Which LLM to use. Always `groq` on Render. |
| 7 | `TIMEZONE` | recommended | `America/Chicago` | IANA timezone. Controls "today" / "yesterday" resolution. |
| 8 | `DEFAULT_CURRENCY` | recommended | `USD` | Currency assumed when message has no marker (e.g. "spent 40 on coffee"). |
| auto | `PORT` | injected by Render | `10000` | The port our health server must bind to. **Do not set manually.** |
| — | `GROQ_MODEL` | **NOT SET** on Render | (would be `openai/gpt-oss-20b`) | Deliberately omitted. The code default in `src/expense_tracker/config.py` is the source of truth, so model migrations only need a code push — no Render dashboard click required. See §13.4 for why this design held up well during the June 2026 model migration. |

### 4.2 Local development (`.env` file)

For running on your laptop. Includes some values that don't apply on
Render (like Ollama URL).

```bash
# === LLM ===
LLM_PROVIDER=groq                         # or 'ollama' for offline
GROQ_API_KEY=gsk_...
GROQ_MODEL=openai/gpt-oss-20b
OLLAMA_BASE_URL=http://localhost:11434    # only used if LLM_PROVIDER=ollama
OLLAMA_MODEL=llama3.1
LLM_TIMEOUT_S=30.0
LLM_MAX_RETRIES=3
LLM_TEMPERATURE=0.1                       # extraction wants low temp
LLM_MAX_TOKENS=1024

# === Locale ===
TIMEZONE=America/Chicago
DEFAULT_CURRENCY=USD

# === Storage / observability ===
LLM_TRACE=true                            # writes JSONL traces of every call
LOG_DIR=./logs                            # on Render this is /app/logs
CHAT_STORE_BACKEND=jsonl

# === Google Sheets ===
GOOGLE_SERVICE_ACCOUNT_JSON=./secrets/service-account.json   # local: file path
EXPENSE_SHEET_ID=1AbC2dEf...
SHEETS_TIMEOUT_S=30.0

# === Telegram (only for `expense --telegram` locally) ===
TELEGRAM_BOT_TOKEN=8702975628:AAF...
TELEGRAM_ALLOWED_USERS=8684705854
```

### 4.3 GitHub (variables + secrets)

| Type | Name | Value | Used by |
|------|------|-------|---------|
| Variable (public) | `RENDER_SERVICE_URL` | `https://expense-tracker-bot-z558.onrender.com` | `keep-render-alive.yml` workflow |
| Secret (optional) | `KEEPALIVE_TG_BOT_TOKEN` | bot token | Telegram alerts on keep-alive failure |
| Secret (optional) | `KEEPALIVE_TG_CHAT_ID` | your user ID | same |

To edit: https://github.com/vinay3093/expense-tracker-bot/settings/variables/actions

### 4.4 UptimeRobot

Configured via UI, not env vars. Single monitor:

| Setting | Value |
|---------|-------|
| Monitor type | HTTP(s) |
| Friendly name | `Render expense bot keep-alive` |
| URL | `https://expense-tracker-bot-z558.onrender.com/health` |
| Interval | 5 minutes |
| HTTP method | HEAD (free-tier locked) |
| Up codes | 2xx, 3xx |
| Follow redirects | enabled |
| Alert contact | your email (from signup) |

### 4.5 Render service settings (UI, not env vars)

| Setting | Value |
|---------|-------|
| Service name | `expense-tracker-bot` |
| Service ID | `srv-d7sfrikm0tmc7384tnt0` |
| Runtime | Docker |
| Plan | Free |
| Region | Ohio |
| Branch | `main` |
| Auto-deploy | yes |
| Health check path | `/health` |
| Build command | (empty — Dockerfile handles it) |
| Start command | (empty — Dockerfile `CMD` handles it) |

---

## 5. Code architecture — module map

```
expense-tracker-bot/
├── Dockerfile                     # Render + HF container build recipe
├── pyproject.toml                 # deps + entry-point (`expense` CLI)
├── README.md                      # project landing page (also HF Space metadata)
├── .env.example                   # template for local dev
│
├── src/expense_tracker/           # ────── PYTHON PACKAGE ──────
│   ├── __main__.py                # `expense` CLI entry point
│   ├── config.py                  # Pydantic Settings (env -> typed config)
│   │
│   ├── llm/                       # LLM abstraction (provider-agnostic)
│   │   ├── client.py              # LLMClient protocol + concrete impls
│   │   ├── groq_client.py         # Groq adapter (default)
│   │   ├── ollama_client.py       # Local Ollama adapter
│   │   ├── openai_client.py       # OpenAI adapter (paid)
│   │   ├── anthropic_client.py    # Anthropic adapter (paid)
│   │   ├── fake_client.py         # in-memory stub for tests
│   │   └── factory.py             # builds the right client from config
│   │
│   ├── extractor/                 # LLM-guided structured extraction
│   │   ├── intent.py              # IntentClassifier (log / retrieve / chitchat)
│   │   ├── expense.py             # ExpenseExtractor (text -> ExpenseEntry)
│   │   ├── retrieval.py           # RetrievalExtractor (text -> RetrievalQuery)
│   │   ├── categories.py          # CategoryRegistry (alias resolution)
│   │   ├── prompts.py             # System prompts (with category override rules)
│   │   └── data/
│   │       └── categories.yaml    # Canonical 13 categories + aliases
│   │
│   ├── ledger/                    # ────── PERSISTENCE LAYER ──────
│   │   ├── base.py                # LedgerBackend Protocol (abstract API)
│   │   ├── factory.py             # builds backend from STORAGE_BACKEND env
│   │   ├── sheets/                # Google Sheets backend (active)
│   │   │   ├── backend.py         # SheetsLedgerBackend
│   │   │   ├── credentials.py     # resolves _CONTENT or file path
│   │   │   ├── transactions.py    # master ledger writes
│   │   │   ├── monthly.py         # monthly tab create + format
│   │   │   ├── yearly.py          # YTD dashboard
│   │   │   ├── formulas.py        # SUMIFS / QUERY formula generators
│   │   │   ├── fx.py              # Frankfurter FX client + cache
│   │   │   ├── fake_backend.py    # in-memory test impl
│   │   │   └── data/
│   │   │       └── sheet_format.yaml   # column order, colors, etc.
│   │   ├── nocodb/                # Postgres backend (built, not active)
│   │   │   ├── backend.py         # PostgresLedgerBackend
│   │   │   ├── models.py          # SQLAlchemy 2.0 models
│   │   │   └── migrations/        # Alembic migrations
│   │   └── mirror/                # Dual-write wrapper (built, not active)
│   │       ├── backend.py         # MirrorLedgerBackend
│   │       └── reconcile.py       # drift detection + repair
│   │
│   ├── pipeline/                  # ────── ORCHESTRATION ──────
│   │   ├── chat.py                # ChatPipeline (intent -> extract -> write)
│   │   ├── correction.py          # CorrectionProcessor (/undo, /edit)
│   │   ├── retrieval.py           # RetrievalEngine (read + filter + aggregate)
│   │   ├── summary.py             # SummaryEngine (week/month/year)
│   │   ├── reply.py               # human-readable reply formatter
│   │   ├── factory.py             # wires the whole pipeline
│   │   └── logger.py              # JSONL trace logging
│   │
│   ├── storage/                   # JSONL stores
│   │   ├── chat_store.py          # ChatStore protocol
│   │   ├── jsonl_chat_store.py    # default impl
│   │   └── correction_log.py      # for /undo state
│   │
│   └── telegram_app/              # ────── TELEGRAM BOT ──────
│       ├── factory.py             # builds Application, registers handlers,
│       │                          #   adds global error handler, manual
│       │                          #   asyncio bootstrap
│       ├── bot.py                 # message + command handlers
│       ├── settings.py            # Pydantic Settings for TG-specific env
│       └── health_server.py       # tiny HTTP /health endpoint (GET + HEAD)
│
├── tests/                         # ~150 unit + integration tests
│   ├── test_*.py                  # one file per module
│   └── conftest.py                # shared fixtures (FakeSheetsBackend, etc.)
│
├── deploy/                        # ────── DEPLOY BUNDLES ──────
│   ├── render-edition/            # 24/7 host (active)
│   │   ├── render.yaml            # one-click Blueprint
│   │   ├── DEPLOY.md              # click-by-click runbook
│   │   ├── secrets-checklist.md   # the 8 env vars
│   │   └── README.md              # why Render, comparisons
│   ├── huggingface-edition/       # superseded — HF blocks Telegram
│   ├── nocodb-edition/            # docker-compose for local NocoDB+Postgres
│   └── sheets-edition/            # systemd / cron recipes for self-host
│
├── docs/                          # ────── DOCUMENTATION ──────
│   ├── MASTER_GUIDE.md            # this file
│   ├── HANDBOOK.md                # long-form pedagogical tour
│   ├── ARCHITECTURE.md            # module-by-module deep dive
│   ├── WAKE_UP.md                 # "what was I doing yesterday" recap
│   └── HANDBOOK.docx              # Word version of the handbook
│
└── .github/workflows/
    ├── ci.yml                     # tests on every push
    └── keep-render-alive.yml      # backup keep-alive cron (UR is primary)
```

### Layered architecture (top-down)

```
┌─────────────────────────────────────────────────────────────────┐
│  Interface layer  (Telegram bot OR CLI)                         │
│  - telegram_app/bot.py + factory.py                              │
│  - __main__.py (CLI: --chat, --telegram, --init-transactions...)│
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  Pipeline layer  (orchestration, no I/O)                         │
│  - pipeline/chat.py     -> intent -> extract -> ledger -> reply  │
│  - pipeline/correction  -> /undo, /edit                          │
│  - pipeline/retrieval   -> "how much for food in April?"         │
│  - pipeline/summary     -> weekly / monthly / yearly             │
└─────────┬──────────────────────────────────────────┬─────────────┘
          │                                          │
┌─────────▼─────────┐                    ┌───────────▼───────────┐
│  Extractor layer  │                    │  Ledger layer         │
│  - intent.py      │                    │  - base.py (Protocol) │
│  - expense.py     │                    │  - sheets/backend.py  │
│  - retrieval.py   │                    │  - nocodb/backend.py  │
│  - categories.yaml│                    │  - mirror/backend.py  │
└────────┬──────────┘                    └───────┬───────────────┘
         │                                       │
┌────────▼──────────┐                  ┌─────────▼────────────┐
│  LLM layer        │                  │  External I/O        │
│  - client.py      │                  │  - gspread           │
│  - groq_client.py │                  │  - Frankfurter HTTP  │
└────────┬──────────┘                  │  - SQLAlchemy        │
         │                             └──────────────────────┘
┌────────▼──────────┐
│  Groq Cloud HTTP  │
└───────────────────┘
```

**Two key abstractions** make this swappable end-to-end:

1. **`LLMClient` Protocol** — Groq, OpenAI, Anthropic, Ollama, and a
   `FakeLLMClient` for tests all conform to the same 3-method
   interface (`chat`, `extract_json`, `health_check`). Switching is an
   env-var change.

2. **`LedgerBackend` Protocol** — `SheetsLedgerBackend`,
   `PostgresLedgerBackend`, `MirrorLedgerBackend`, and a fake all
   implement the same write/read API. The pipeline doesn't know or
   care which one is active.

---

## 6. End-to-end request flow

What happens between you sending `today I spent 40 dollars for body
massage, add to shopping` and seeing the confirmation reply, in
milliseconds:

```
T+0ms     You hit Send in Telegram on your phone.

T+50ms    Telegram MTProto cloud receives it, queues for delivery.

T+200ms   Render container (which is mid-`getUpdates` long-poll)
          receives the message in its `python-telegram-bot` event loop.

T+201ms   `bot.py:handle_text()` is invoked.
          - `update.effective_user.id` checked against
            TELEGRAM_ALLOWED_USERS. Unknown? Silent drop.
          - "Typing..." indicator sent to your client (free UX).
          - Call dispatched to `processor.process(user_id, text)`
            via `asyncio.to_thread()` so the LLM call doesn't
            block the polling loop.

T+250ms   `pipeline/chat.py:ChatPipeline.process()`
          - JSONL conversation turn logged to /app/logs/conversations.jsonl
          - IntentClassifier.classify(text) called.

T+800ms   IntentClassifier (LLM call #1, Groq):
          - Sends ~200-token system prompt + user text to Groq.
          - Groq responds in ~500ms with JSON `{"intent": "log_expense"}`.
          - Logged to /app/logs/llm_calls.jsonl.

T+900ms   ExpenseExtractor.extract(text) (LLM call #2, Groq):
          - Larger prompt (~1KB) listing 13 categories + aliases +
            the new "user-named category wins" rule.
          - Groq responds in ~700ms with JSON:
              {
                "amount": 40,
                "currency": "USD",
                "category": "Shopping",
                "vendor": null,
                "note": "body massage",
                "date": "2026-05-04",
                "confidence": 0.95
              }
          - CategoryRegistry resolves "Shopping" -> canonical "Shopping".

T+1600ms  SheetsLedgerBackend.write_expense(entry):
          - Resolves Google credentials (from JSON env var).
          - Opens spreadsheet by ID via gspread.
          - Calls ensure_period(2026, 5):
              - Tab "May 2026" exists? Yes (we pre-created it).
              - Just verify; no API calls needed.
          - Appends one row to "Transactions" master ledger
            (1 API call, ~300ms).
          - Currency conversion: USD == USD, no FX call needed.
          - Returns LedgerWrite(success=True, row_index=42).

T+1900ms  ReplyFormatter.format_log_success(entry, ledger_write):
          - Reads back today's total from the May 2026 tab
            (1 API call, ~200ms).
          - Builds: "✅ Logged: $40.00 Shopping (body massage)
            on 2026-05-04 · Today: $40.00 · May 2026 total: $40.00"

T+2100ms  bot.py: `await update.effective_message.reply_text(reply)`
          - Telegram MTProto upload, ~100ms.

T+2200ms  Your Telegram app shows the reply.

──────────────────────────────────────────────────────────────────
TOTAL: ~2.2 seconds end-to-end.
LLM calls: 2 (intent + extract). ~1.2s of total.
Sheets API calls: 2 (append + read). ~0.5s of total.
```

If anything throws (Sheets 429, Groq timeout, FX cache write fail,
bug in extractor), two safety nets catch it:

1. **Per-handler `try/except`** in `bot.py:handle_text` — sends a
   user-friendly apology with the exception type so you can grep
   logs.
2. **Global PTB error handler** in `factory.py:_on_handler_error` —
   logs full traceback at ERROR, sends generic apology if the first
   net missed it. Keeps the polling loop alive no matter what.

---

## 7. Chronological journey — what we built when, and why

This is the build log. If you ever wonder *"why did we add X?"*, the
answer is somewhere here.

### Phase 1 — CLI MVP (Week 1)

* **Goal:** Get an LLM to extract structured fields from chat-ish text.
* **What we built:**
  * `LLMClient` protocol + Groq adapter.
  * Stage-1 `IntentClassifier` and stage-2 `ExpenseExtractor`.
  * `expense --chat "..."` CLI command.
  * JSONL trace logging (`logs/llm_calls.jsonl`).
* **Why:** Prove the LLM extraction works before building anything else.

### Phase 2 — Google Sheets integration (Week 2)

* **Goal:** Write extracted expenses to a real spreadsheet that mirrors
  the manual layout you were already maintaining.
* **What we built:**
  * Service account auth via `gspread`.
  * Master `Transactions` ledger (append-only).
  * `MonthlyBuilder` — auto-creates new month tabs with `SUMIFS`
    formulas pointing at `Transactions`.
  * `YearlyBuilder` — YTD dashboard with monthly totals.
  * `FakeSheetsBackend` for tests (in-memory, no API calls).
  * `expense --init-transactions`, `--build-month YYYY-MM`,
    `--build-year YYYY` CLI subcommands.
* **Why:** Source-of-truth on a platform you can read on your phone
  with zero new apps.

### Phase 3 — Multi-currency + retrieval (Week 3)

* **Goal:** Handle "spent 1500 INR yesterday" (auto-convert to USD)
  and answer "how much did I spend on food in April?".
* **What we built:**
  * `Frankfurter.app` integration with local JSON cache.
  * `RetrievalExtractor` (text → `RetrievalQuery`).
  * `RetrievalEngine` — reads `Transactions`, filters, aggregates.
  * Returns structured `RetrievalAnswer` ("April food: $432.10 across
    12 transactions").
* **Why:** A tracker you can't *query* is just a write-only fire hose.

### Phase 4 — Telegram bot (Week 4)

* **Goal:** Stop running CLI commands; just text the bot.
* **What we built:**
  * `python-telegram-bot` integration with long-polling.
  * `TELEGRAM_ALLOWED_USERS` allow-list.
  * `/start`, `/help`, `/last`, `/whoami` commands.
  * `expense --telegram` CLI starts the bot.
* **Why:** Phone-first UX. No more SSH-into-laptop to log lunch.

### Phase 5 — Corrections (Week 4)

* **Goal:** "Oops, I meant $50 not $40" / "wrong category".
* **What we built:**
  * `CorrectionLogger` — snapshot of the last row written.
  * `CorrectionProcessor` — parses `/undo`, `/edit amount X`,
    `/edit category Y`.
  * Sheet row deletion (`worksheet.delete_rows`).
  * Force monthly recompute after edits.
* **Why:** Real-world data entry has typos. Need surgical fixes, not
  "delete the whole month and re-enter".

### Phase 6 — Summaries (Week 5)

* **Goal:** "Show me last week's spending vs the week before."
* **What we built:**
  * `SummaryEngine` — focal period + prior period comparison.
  * `SummaryScope` enum (week/month/year).
  * `/weekly`, `/monthly`, `/yearly` Telegram commands.
* **Why:** Trends matter more than individual entries.

### Phase 7 — Postgres backend (Week 6, built but not wired)

* **Goal:** Future-proof the storage layer so SQL analytics + a NocoDB
  UI become possible.
* **What we built:**
  * `LedgerBackend` Protocol — abstracted the sheets-specific API.
  * `SheetsLedgerBackend` — wrapped existing code as the first impl.
  * `PostgresLedgerBackend` — SQLAlchemy 2.0 models, soft-delete,
    audit log, Alembic migrations.
  * `MirrorLedgerBackend` — dual-write wrapper (Sheets + Postgres).
  * `expense --init-postgres`, `--postgres-health`,
    `--migrate-sheets-to-postgres`, `--reconcile` CLI subcommands.
  * `deploy/nocodb-edition/` — docker-compose for local NocoDB + Postgres.
* **Status:** Code merged; not turned on in prod (no `STORAGE_BACKEND=mirror`
  env var on Render). See §10 to enable.

### Phase 8 — The hosting odyssey (week 7-8)

This was the hardest part of the project. Documented in detail because
future-you will face the same decisions if you ever migrate.

#### 8a. Oracle Cloud Free Tier (attempted, abandoned)

* **Hope:** 4-core ARM VM, 24 GB RAM, free forever.
* **Reality:** "Out of capacity" in every region we tried (Chicago,
  Phoenix, Ashburn, San Jose) for 3 days straight. Oracle's free-tier
  ARM instances are perpetually oversubscribed.
* **Lesson:** Oracle Free is a great deal *if you can get it*. Don't
  bet a deadline on it.

#### 8b. Hugging Face Spaces (attempted, abandoned)

* **Hope:** Free Docker hosting, 16 GB RAM, persistent.
* **Reality:** Got it deploying after fixing:
  * Missing YAML frontmatter on README.md (HF requires it).
  * `PermissionError` on `logs/` (Dockerfile didn't pre-create writable dirs).
  * Logging silently suppressed at WARNING level (manual `basicConfig`).
* **Then discovered the killer:** **Hugging Face Spaces blocks all
  outbound traffic to `api.telegram.org`** as a deliberate security
  policy. Confirmed via web search of HF community forum threads.
  No code workaround possible.
* **Lesson:** Always check outbound connectivity for the *exact*
  hosts you need before committing to a host.

#### 8c. Render Free (chosen)

* **Why:** Allows arbitrary outbound HTTPS. Auto-deploys on git push.
  Real Docker. Free forever within 750 hours/month.
* **Tradeoff:** Spins down after 15 min of idle → solved with UptimeRobot.
* **Setup:** Built `deploy/render-edition/` with Blueprint (`render.yaml`),
  click-by-click runbook (`DEPLOY.md`), secrets checklist.

### Phase 9 — Robustness fixes (also week 8)

Once on Render, several issues surfaced one by one:

#### 9a. Sheets API 429 quota error

* **Symptom:** First message of a new month → `Quota exceeded` →
  bot replied with error → next message: silence.
* **Root cause:** `ensure_period()` does ~80-100 API calls when
  building a brand-new monthly tab from scratch (each format/border/
  formula write is one call). Burst exceeds 60 writes/min quota.
* **Fix (immediate):** Pre-create the new month tab from your laptop
  (where there's no rate-limit issue) by sending `expense --chat
  "test"`. Render then only needs `append` (3-5 calls/msg) which is
  well under quota.
* **Fix (permanent, would require API redesign):** Batch the format
  calls into one `batchUpdate` request. Not done yet — pre-creation
  workaround is good enough for personal use.

#### 9b. Polling loop death after Sheets exception

* **Symptom:** After a Sheets 429 error reached the bot's exception
  handler, the bot stopped responding to ALL future messages — but
  the container kept running.
* **Root cause:** `python-telegram-bot` v21 has a known issue where
  an unhandled exception in a handler corrupts the asyncio state of
  the polling loop, causing it to silently stop reading updates.
* **Fix:** Two-layer error handling:
  * Per-handler `try/except` in `bot.py:handle_text` — catches
    pipeline exceptions, sends user-friendly apology, polling stays alive.
  * Global PTB error handler in `factory.py:_on_handler_error` —
    catches anything the per-handler missed, prevents the loop from
    dying.
* **Commit:** `3c9f06c`.

#### 9c. PTB silent hang on Render

* **Symptom:** Container starts, prints "Application started", then
  silence forever. No `getUpdates` calls reach Telegram.
* **Root cause:** `app.run_polling()` uses signal handlers that
  conflict with non-default asyncio loops on some Linux containers.
* **Fix:** Replaced `app.run_polling()` with manual
  `asyncio.run(_serve_forever())` that explicitly logs each
  bootstrap step (`initialize`, `start`, `updater.start_polling`).
  Granular logging makes future hangs pinpointable.
* **Commit:** `919406f`.

### Phase 10 — Keep-alive (the night this guide was written)

* **Goal:** Stop Render's 15-min idle spin-down.
* **First attempt:** GitHub Actions cron pinging `/health` every 14 min.
  * **Problem 1:** GitHub variable `RENDER_SERVICE_URL` was wrong
    (`zb68` instead of actual `z558`) → 404 → workflow fails.
  * **Problem 2:** Even after fixing the URL, GitHub Actions
    `schedule:` events for low-traffic public repos are deprioritized
    and often don't fire for 30-60 min. Unreliable.
* **Final attempt:** **UptimeRobot** — purpose-built external uptime
  monitor, free, 5-min real cron, immune to GitHub's scheduling jitter.
* **Bonus fix:** UptimeRobot free tier hard-locks the HTTP method
  picker to HEAD. Our `health_server.py` only implemented `do_GET`
  → `BaseHTTPServer` returned 501 Not Implemented for HEAD →
  UptimeRobot reported "Down" on a perfectly healthy bot.
* **Fix:** Added `do_HEAD` method that mirrors GET status + headers
  but skips the body (per RFC 7231 §4.3.2). Plus 3 new socket-level
  tests so this can never silently regress.
* **Commit:** `de811c6`.

### Phase 11 — Pre-build a year of monthly tabs (June 1, 2026)

* **Goal:** Kill the "new month → 429 quota burst → user's first
  message of the month fails" problem permanently for the next 12
  months.
* **What we built:** `scripts/_one_off_prebuild_year.py` —
  laptop-side script that calls `build_month_tab()` and
  `build_ytd_tab()` for each upcoming month, with a 60-sec sleep
  between operations to stay under Google's 60-writes/min quota.
* **What worked:** Built 7 out of 13 monthly tabs cleanly (July 2026,
  September 2026, November 2026, January 2027, March 2027, April 2027,
  June 2027) — plus YTD 2027 on a follow-up retry pass at 240-sec
  pacing.
* **What didn't (yet):** 3 months still wedged with 429 even at
  240-sec pacing: **October 2026, December 2026, May 2027**. Root
  cause appears to be the Render bot processing telemetry writes
  concurrently with the bulk script, stealing quota at the edge of
  the rolling window. **TODO before those months arrive:** either
  retry again with the Render bot temporarily suspended, OR fix the
  real root cause by batching the per-month API calls into a single
  Sheets `batchUpdate` (would turn 80 calls into 1).
* **Lesson:** The bulk script proves a workaround works; the real
  fix is still upstream in `build_month_tab()`.
* **Commits:** `3d34009` (initial script), `20ffba4` (added the
  retry script and the YAML config tweaks).

### Phase 12 — The silent-deploy-failure discovery (June 26, 2026)

* **What we noticed:** While migrating Groq models (Phase 14), we
  tried to deploy commit `20ffba4` and the Render Events tab showed
  "Deploy failed". On closer look — **the June 1 prebuild-script
  commit had ALSO failed to deploy.** The last successful deploy
  was `a065868` from May 4.
* **What that means:** **Production had been running 25-day-stale
  code without anyone noticing.** Render's "graceful failover" —
  keeping the last good build live when a new build fails — is
  user-friendly but observability-hostile. The bot kept replying to
  Telegram messages on May-4 code while every subsequent deploy
  silently failed.
* **Why we didn't notice for 25 days:**
  - UptimeRobot only monitors *liveness* (HTTP 200), not *commit
    freshness*.
  - GitHub Actions CI passes don't imply Render deploys pass — they
    use different build environments.
  - No alert was wired for "deploy failed".
* **Fixes:**
  - Document this failure mode in the runbook (§9.9 new).
  - **TODO:** add an alert path — either GitHub Action that pings the
    Render API for deploy status after each push, or wire the Render
    webhook → Telegram/email.
* **Lesson:** Liveness checks are insufficient. You also need
  *deploy-success* alerts.

### Phase 13 — Hatch duplicate-file fix (June 26, 2026)

* **The actual root cause** of the silent failures from Phase 12.
* **What broke:** `hatchling` (our wheel build backend) released
  1.27 in mid-2025 which hard-errors on duplicate file entries in
  the wheel. Our `pyproject.toml` listed YAML data files **twice**
  — once implicitly via `packages = ["src/expense_tracker"]` (which
  ships everything under that path) and once explicitly via
  `[tool.hatch.build.targets.wheel.force-include]` "for defensive
  clarity". Old Hatch deduplicated silently; new Hatch raised:
  ```
  ValueError: A second file is being added to the wheel archive at
  the same path: `expense_tracker/extractor/data/categories.yaml`.
  ```
* **Why our laptop didn't reproduce it:** local installs use
  `pip install -e .` (editable) which doesn't build a wheel at all,
  so the duplicate-file check never fires.
* **Why Render started failing on June 1, not earlier:** Render's
  Docker builder uses a fresh hatchling on every build (no caching
  across builds). It happened to pull a 1.27+ version on the June 1
  deploy attempt, and every attempt since.
* **Fix:** Removed the redundant `force-include` block from
  `pyproject.toml`. The `packages` line already ships both YAML
  files (verified locally — `CategoryRegistry` loads 13 categories,
  `SheetFormat` correctly renders "July 2026" from the config).
  Full test suite (555 tests) still passes.
* **Commit:** `f6b71a1`.

### Phase 14 — Groq model migration (June 26, 2026)

* **The trigger:** Groq emailed announcing **`llama-3.1-8b-instant`
  is deprecated 2026-06-25, hard-decommissioned 2026-08-16**.
  After that date every Groq API call for this model returns 410
  Gone, the bot crashes at the intent-classify step, and every
  Telegram message gets the global error handler's apology — silent
  loss-of-service for the user.
* **Migration target:** `openai/gpt-oss-20b` — Groq's recommended
  replacement (OpenAI's open-weights model that Groq added day-zero
  support for):
  - Same OpenAI-compatible JSON-mode API → zero code-shape changes
  - Same free-tier limits (30 RPM, 6K TPM, 14,400 RPD)
  - Same 128K context window
  - 2.5× larger model (20B vs 8B) → more reliable structured JSON
  - ~2× throughput on Groq's silicon (1000 tok/s vs 560 tok/s)
* **The change:** Updated `Settings.GROQ_MODEL` default in
  `config.py` + the fallback in `groq_client.py` + the assertion in
  `test_config.py` + every doc string referencing the old name.
  No env var change needed on Render — `GROQ_MODEL` was never set
  there (the code default is the source of truth).
* **Live-verified locally** before push: both LLM-using paths
  (`log_expense` and `query_category_total`) returned correctly-
  shaped JSON in ~1.7s with 0.95 confidence on real expense and
  query messages.
* **The complication:** First push (`20ffba4`) silently failed to
  deploy because of the Phase 12/13 Hatch issue. After the Phase
  13 fix shipped (`f6b71a1`), both fixes deployed together because
  Render rebuilds from HEAD, not commit-by-commit.
* **Commits:** `20ffba4` (migration), `f6b71a1` (Hatch unblock).

### What's the bot doing right now?

As of this guide's "last updated" timestamp:

* Container running 24/7 at `https://expense-tracker-bot-z558.onrender.com`.
* UptimeRobot pinging every 5 min from 3 US regions, 100% uptime.
* GitHub Actions backup keep-alive running every 14 min.
* Auto-deploy on every push to `main` (working again as of `f6b71a1`).
* LLM = `openai/gpt-oss-20b` via Groq (migrated June 26 from the
  now-deprecated `llama-3.1-8b-instant`; safely past the Aug 16
  decommission deadline).
* Writing to Google Sheets only (Postgres mirror mode still dormant —
  the one remaining "future-me will thank present-me" task).
* Monthly tabs pre-built through June 2027 for most months; 3 still
  pending due to 429 issues (October 2026, December 2026, May 2027).

---

## 8. Daily operations cookbook

### 8.1 Log an expense

Just text the bot:

```
spent 12 dollars for lunch at chipotle
1500 INR for groceries yesterday
50 for haircut
gas bill 80 dollars on the 28th
got shampoo for my wife which cost me 100$, add this to shopping
```

### 8.2 Ask a retrieval question

```
how much did I spend on food in April?
how much for shopping this week?
food expenses last 7 days
show last 5 transactions
how much for groceries on the 24th?
```

### 8.3 Get a summary

Use the slash commands:

```
/weekly        # this week vs last week
/monthly       # this month vs last month
/yearly        # this year vs last year
/last          # show the most recent expense
```

### 8.4 Fix a mistake

```
/last                       # confirm what's there
/undo                       # delete the last expense + recompute monthly tab
/edit amount 50             # change last expense's amount
/edit category Food         # change last expense's category
/edit vendor Chipotle       # change last expense's vendor
```

### 8.5 Open the Sheet on your phone

Just open Google Sheets app → "Daily Expense 2026". Tap any tab.
The bot's writes show up within seconds because we use formulas, not
copy-paste.

### 8.6 Check that the bot is alive

* **Quick check:** DM the bot `/start` or `/whoami`.
* **Status page:** UptimeRobot dashboard → see live up/down + response
  time chart.
* **Render logs:** https://dashboard.render.com → expense-tracker-bot →
  Logs tab.

---

## 9. Operational runbook — when X breaks, do Y

### 9.1 Bot doesn't reply to Telegram

**Step 1: Is the container alive?**

```bash
curl -i https://expense-tracker-bot-z558.onrender.com/health
```

* `HTTP 200 ok` (after ~1s warm response or ~30s cold-start) → container is fine, problem is elsewhere.
* `HTTP 404` or `x-render-routing: no-server` → container is suspended/crashed. Go to step 2.
* Connection timeout → Render itself may be degraded; check https://status.render.com.

**Step 2: Wake / redeploy on Render**

1. Open https://dashboard.render.com → expense-tracker-bot.
2. Look at the top: green "Live" badge?
   * Yes → click `Manual Deploy` → `Deploy latest commit`.
   * No → click `Resume Service` (if suspended), or look at Events tab for the failure reason.
3. Watch Logs tab for `PTB bootstrap: bot is now LIVE`.

**Step 3: Confirm Telegram + Render are talking**

Check Telegram's queue:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
```

* If you see queued messages, the bot is online but processing slow.
* If you see `"description": "Conflict: terminated by other getUpdates"`
  — there's another bot instance polling. Likely a stale local
  `expense --telegram` you forgot to kill. Find and kill it.

### 9.2 UptimeRobot says "Down" but the bot replies fine

* **Most likely cause:** UptimeRobot is hitting the wrong URL (Render's
  random suffix changed because the service was deleted+recreated).
* **Fix:** Get the *current* URL from Render dashboard top header,
  update the UptimeRobot monitor URL, also update the GitHub variable
  `RENDER_SERVICE_URL`.

### 9.3 Sheets 429 quota error in logs

* **What it means:** Bot hit Google's 60 writes/min quota, almost
  always when building a fresh monthly tab.
* **Fix:** Pre-create the new month from your laptop. From the repo
  root with your local `.env` set up:

  ```bash
  expense --chat "today I spent 1 dollar for testing init"
  ```

  Then `/undo` from Telegram to remove the test row. Future month
  writes only need `append` (3-5 calls/msg, well under quota).

### 9.4 Render auto-deploy didn't fire after a push

* **Check Events tab:** Did Render see the commit? If yes but it failed,
  click the failed deploy for build logs.
* **If Render didn't see the push:** GitHub → Settings → Webhooks → look
  for the Render webhook → check "Recent deliveries" tab for failures.

### 9.5 Bot replied with apology mentioning some `Error`

* The error message includes the exception type (e.g. `LedgerError`,
  `ExtractorError`, `LLMTimeoutError`).
* Check Render logs for the full traceback (search for the exception name).
* Most common: transient LLM/Sheets/network blip — re-send the message.

### 9.6 Wrong category was assigned

* You can override at chat time:
  ```
  add this to shopping column for today: spent 30 on shampoo
  ```
  The extractor's prompt has an explicit "user-named category wins"
  rule.
* Or fix after the fact:
  ```
  /edit category Shopping
  ```

### 9.7 I lost my BotFather token / GCP key / Groq key

* **BotFather token:** Open Telegram → `@BotFather` → `/mybots` →
  pick your bot → API Token → Revoke current token → copy new one.
  Update `TELEGRAM_BOT_TOKEN` in Render env vars. Render auto-redeploys.
* **GCP service account JSON:** GCP Console → IAM & Admin → Service
  Accounts → click yours → Keys tab → delete old, create new, download
  JSON. Run the one-liner below to get a single-line version, paste
  into Render's `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT`:
  ```bash
  python -c "import json; print(json.dumps(json.load(open('service-account.json'))))" | xclip -selection clipboard
  ```
* **Groq API key:** Groq console → API Keys → revoke + create new.
  Update `GROQ_API_KEY` in Render. Auto-redeploys.

### 9.8 Want to roll back to a previous deploy

Render dashboard → Events tab → find the previous good deploy →
click the `Rollback` button on its row.

### 9.9 Deploys are failing silently — bot is fine but new code never ships

* **Symptom:** Bot replies to Telegram normally, UptimeRobot green,
  but the latest code changes you pushed don't seem to be live.
* **Root cause family:** Render keeps the last good build running
  when a new build fails. The container itself doesn't restart, so
  liveness checks all pass. You only find out by looking at the
  Render **Events** tab and seeing red ❌ on recent deploys. We were
  burned by this on June 26 — 25 days of silent failures (Phase 12).
* **How to check this is happening to you:**
  1. Render dashboard → expense-tracker-bot → **Events** tab.
  2. Look at the very top entry. Green checkmark = latest deploy
     succeeded. Red X = latest deploy FAILED but the previous good
     one is still running (this is the silent failure).
  3. Cross-check: compare the commit hash on the latest green
     deploy with `git log -1 origin/main` (most recent push). If
     they differ, deploys are silently failing.
* **How to fix once spotted:**
  1. Click `deploy logs` on the top failed deploy.
  2. Scroll up from the bottom to find the actual error (NOT the
     generic "exit code 1" at the very bottom — that's pip's noise).
     Look for: `ERROR: Could not build wheel for X` / a Python
     traceback / `ValueError:` / `ImportError:`.
  3. Fix the issue locally, run `pytest`, push.
* **Common causes** we've seen:
  - **Hatch duplicate-file** (June 2026, our case) → see §13.3.
  - **Python dependency yanked from PyPI** → pin that dependency.
  - **Transitive dep dropped Python 3.11 support** → bump the
    `requires-python` or pin the dep before the drop.
* **Long-term prevention:** TODO — wire up a deploy-success alert.
  Options: GitHub Action that polls Render's deploy API after each
  push, or a Render webhook → Telegram. Without this, you only find
  out when you try to deploy something else and it ALSO fails.

### 9.10 Groq emailed saying our model is being deprecated

* The migration is straightforward because of the `LLMClient`
  Protocol — same OpenAI-compatible API across every Groq model,
  so swapping is a one-line config change.
* **Steps:**
  1. Open the Groq console → Models → pick a replacement model.
     Look for one with **OpenAI-compatible JSON mode** support
     (almost all of them do). Recommended: their suggested
     replacement in the deprecation email, OR
     `meta-llama/llama-4-scout-17b-16e-instruct` (newer
     architecture, generous free tier).
  2. Update `src/expense_tracker/config.py` —
     `GROQ_MODEL: str = Field(default="<new-model-id>")`.
  3. Also update `src/expense_tracker/llm/groq_client.py` fallback
     and `tests/test_config.py` assertion to match.
  4. Run `expense --extract "spent 7 dollars on coffee"` locally to
     verify the new model returns valid JSON.
  5. Run `pytest` (should still pass).
  6. Push to `main` → Render auto-deploys.
  7. Verify in production by sending a Telegram message.
* See §13.4 for the full June 2026 migration story for reference.

---

## 10. What's built but NOT active (Postgres mirror)

The full Postgres-mirror codepath is in `main` but turned off. If you
ever want SQL analytics over your expense history, here's how to
flip it on.

### 10.1 What you'll get

* Every chat write goes to **Sheets first** (still source of truth, still appears on phone).
* Then to **Postgres** as best-effort secondary mirror.
* Reads + retrieval queries + summaries still come from Sheets (no UI change).
* `expense --reconcile` from your laptop back-fills any drift.
* Optional: NocoDB UI on top of Postgres for spreadsheet-style browsing.

### 10.2 Setup steps (~20 min)

1. **Create a Supabase project** at https://supabase.com (free, no card,
   500 MB Postgres).
2. **Get the connection string:** Supabase → Project Settings →
   Database → "Connection string" → use the **Session pooler** URL
   on port 6543 (not direct connect on 5432; Render's free tier
   IPv4 only, Supabase direct is IPv6).
   Format: `postgresql+psycopg://postgres.xxx:password@aws-0-region.pooler.supabase.com:6543/postgres`
3. **Initialize the schema** from your laptop:
   ```bash
   export DATABASE_URL='postgresql+psycopg://...'
   expense --init-postgres
   expense --postgres-health
   ```
4. **Backfill** existing Sheets rows into Postgres (one-time):
   ```bash
   expense --migrate-sheets-to-postgres
   ```
5. **Add 2 env vars to Render:**
   * `STORAGE_BACKEND=mirror`
   * `DATABASE_URL=postgresql+psycopg://...` (same string from step 2)
6. **Render auto-redeploys.** From now on, every Telegram expense
   lands in both Sheets and Postgres.

### 10.3 Reverting

Set `STORAGE_BACKEND=sheets` in Render and redeploy. No data loss —
Sheets was always the source of truth.

### 10.4 Mobile dashboard for Postgres

Supabase has a mobile-friendly web dashboard you can bookmark and
open from your phone. Or run NocoDB from `deploy/nocodb-edition/`
for a more spreadsheet-like view.

---

## 11. Glossary

| Term | Meaning |
|------|---------|
| BotFather | Telegram's meta-bot for creating other bots. |
| Cold start | First HTTP request after a container has been idle long enough to spin down. Render Free cold starts take ~30-60s. |
| FX | Foreign exchange (currency conversion). |
| HEAD request | HTTP method that returns headers but no body. UptimeRobot free tier defaults to HEAD. |
| IANA timezone | Standard timezone name like `America/Chicago` (not `CST` or `UTC-6`). |
| Idempotent | Safe to run multiple times — same result. Reconcile + migrate-sheets-to-postgres are idempotent. |
| JSONL | One JSON object per line, append-only. Used for our trace logs. |
| Long-poll | Way of getting Telegram updates by holding an HTTP connection open for ~10s, used as alternative to webhooks. |
| Mirror mode | Dual-write storage where every expense goes to both Sheets (primary) and Postgres (secondary). |
| MTProto | Telegram's custom binary protocol. We don't speak it directly — `python-telegram-bot` does. |
| Pipeline | The sequence intent classify → extract → write → reply. |
| Render Free | Render's free Docker hosting tier. 512 MB RAM, sleeps after 15 min idle. |
| Service account | A non-human Google account whose credentials a program uses to call Google APIs. |
| Spin-down | When Render stops a Free-tier container due to inactivity. |
| Spin-up | Restarting a spun-down container on the next inbound request. |
| `STORAGE_BACKEND` | Env var that picks the ledger backend: `sheets` (default), `nocodb`, or `mirror`. |
| SUMIFS | Google Sheets formula that sums a range conditional on multiple criteria. Used heavily in monthly tabs. |
| UptimeRobot | External uptime monitor we use as the primary keep-alive. |

---

## 12. FAQ

**Q: Will the bot work when I'm overseas?**
Yes. Everything is in the cloud — Telegram, Render, Sheets, Groq are
all globally accessible. You just need internet on your phone (T-Mobile
roaming, hotel WiFi, etc.).

**Q: Will Render charge me?**
No, as long as you stay within the free tier (750 instance-hours/month
= 31 full days for one service). The credit card is only for fraud
prevention. Stripe holds $1 for verification then releases it.

**Q: What if Render goes down?**
You'll get a UptimeRobot email. Telegram will queue your messages.
When Render comes back, the bot will process the queued messages in
order. You miss nothing — just a delay.

**Q: What if I push broken code to `main`?**
Render's auto-deploy will fail (build error or container crash on
boot). Render keeps the previous good deploy live until the new one
is healthy. Your bot stays up. You'll see a red "Deploy failed" in
Events tab; just push a fix.

**Q: Can multiple people use the bot?**
Yes — add their Telegram user IDs to `TELEGRAM_ALLOWED_USERS`
(comma-separated). All expenses currently go to one shared Sheet.
Multi-tenant (per-user Sheets) would require code changes.

**Q: Can I use this from a laptop without Telegram?**
Yes, via the CLI:
```bash
expense --chat "spent 12 on lunch"
expense --chat "how much for food this month"
```

**Q: Why does the LLM sometimes guess the wrong category?**
The category taxonomy is in `src/expense_tracker/extractor/data/categories.yaml`.
Each category has a `hint` (sent to the LLM as part of the prompt) and an
`aliases` list. To improve a miscategorization:
1. Add the missed phrase to the right category's `aliases`.
2. Sharpen the `hint` if two categories overlap (we did this for
   Shopping vs Saloon — shampoo product vs salon service).
3. Push to `main`, Render auto-deploys.

**Q: How do I know my data is safe?**
* Sheets data is in your Google account; Google's standard backups apply.
* Bot code on GitHub (public) — but no secrets are committed; secrets
  live only in Render env vars.
* If Render dies tomorrow, your Sheet keeps every expense forever —
  Render is just a compute layer, not the database.

**Q: The bot replied to me on Telegram with an apology — what happened?**
A handler caught an exception and apologized rather than crashing.
Check Render logs (Logs tab) for the full traceback — it'll be at
ERROR level with the exception type. Most common: transient Sheets
or Groq blip — just re-send.

**Q: Where do I find my Telegram user ID?**
DM your bot `/whoami` and it'll reply with your numeric ID.

**Q: How do I add a new category?**
Edit `src/expense_tracker/extractor/data/categories.yaml`:
```yaml
- name: NewCategory
  hint: short description of what goes here
  aliases: [synonym1, synonym2, ...]
```
Push to `main`. Render auto-deploys. The new column will appear in
the next monthly tab the bot creates (existing tabs stay as-is).

---

## 13. Change log & lessons learned

A chronological record of every meaningful production change since the
bot went live, with the **why** as much as the **what**. Reading this
top-to-bottom gives you the institutional memory of how the system
evolved and where the landmines have been.

### 13.1 May 2026 — initial 24/7 launch on Render

| Date | Commit | What | Why |
|---|---|---|---|
| 2026-05-03 | `29de007` | Created Render deploy bundle (`render.yaml`, `DEPLOY.md`, secrets checklist) | Hugging Face Spaces was the original 24/7 host plan but blocks all outbound traffic to `api.telegram.org` (deliberate HF security policy — confirmed via HF community forum). Pivoted to Render Free which has no outbound restrictions. |
| 2026-05-03 | `919406f` | Replaced `app.run_polling()` with manual `asyncio.run(_serve_forever())` | PTB v21's `run_polling()` uses signal handlers that silently deadlock on non-default asyncio loops in some Linux containers. The replacement logs each bootstrap step (`initialize`, `start`, `start_polling`) so future hangs are pinpointable. |
| 2026-05-04 | `3c9f06c` | Added per-handler `try/except` in `bot.py` + global PTB error handler in `factory.py` | A Sheets 429 exception cascaded into the polling loop, corrupted asyncio state, and silently killed all future message processing without crashing the container. The two-layer error handling guarantees the polling loop survives any handler exception. |
| 2026-05-04 | `de811c6` | Added `do_HEAD` to `health_server.py` + 3 socket-level tests | UptimeRobot free tier hard-locks the HTTP method picker to HEAD. Our health server only implemented `do_GET` → `BaseHTTPServer` returned `501 Not Implemented` for HEAD → UptimeRobot reported "Down" on a perfectly healthy bot. |
| 2026-05-04 | `a065868` | Initial `MASTER_GUIDE.md` (~1100 lines covering everything end-to-end) | One canonical reference doc. We now have HANDBOOK (pedagogical), MASTER_GUIDE (operational truth), ARCHITECTURE (code-deep), WAKE_UP (5-min recap). |

**Lesson:** Every "deploy 24/7 on a free tier" problem comes back to *outbound network policy* (was Telegram allowed?) and *container lifecycle* (does the platform restart on crash? on idle?). Validate both before committing to a host.

### 13.2 June 1, 2026 — pre-build a year of monthly tabs (partial)

| Date | Commit | What | Why |
|---|---|---|---|
| 2026-06-01 | `3d34009` | Added `scripts/_one_off_prebuild_year.py` | Each time the bot creates a new monthly tab on the first message of a new month, it bursts ~80-100 Sheets API calls — over the 60/min quota — causing the user's first-of-month message to lose data. Pre-creating tabs from the laptop sidesteps this. |

**Outcome:** Built 7 of 13 tabs cleanly (July/Sep/Nov 2026, Jan/Mar/Apr/Jun 2027 + YTD 2027). Three months still wedged at 429 even at 240-sec pacing (Oct 2026, Dec 2026, May 2027). Concurrent writes from the Render bot likely steal quota at the rolling window edge.

**Lesson:** Workarounds for upstream issues (here: API call burst) are honest tactical fixes, not the real solution. The real fix is `build_month_tab()` using Sheets `batchUpdate` (one call instead of 80). That's still on the TODO list.

**Hidden lesson revealed 25 days later:** *this commit was the first to silently fail to deploy* (see §13.3 below). We didn't notice because the bot kept replying.

### 13.3 June 26, 2026 — silent deploy failures + Hatch fix

The biggest operational scare to date — turned out to be a great forcing function for documenting the failure modes that already existed but were invisible.

| Date | Commit | What | Why |
|---|---|---|---|
| 2026-06-26 12:14 AM | (none) | **Discovered: deploys had been silently failing for 25 days** | When trying to deploy the Groq model migration, noticed Render's Events tab showed FAILED on the previous attempt. Scrolled up — the June 1 prebuild commit had ALSO failed. Last successful deploy was `a065868` from May 4. Production was running stale code the whole time. |
| 2026-06-26 12:14 AM | `f6b71a1` | Removed `[tool.hatch.build.targets.wheel.force-include]` from `pyproject.toml` | Hatch 1.27 (mid-2025) started hard-erroring on duplicate wheel entries instead of silently deduplicating. Our `pyproject.toml` listed the YAML data files **twice** — once via `packages = ["src/expense_tracker"]` (implicit) and once via `force-include` (explicit, "for defensive clarity"). Render's Docker build pulled a fresh hatchling that happened to be 1.27+ on June 1 and every build since failed with `ValueError: A second file is being added to the wheel archive`. |
| 2026-06-26 12:15 AM | (deploy `f6b71a1`) | First successful Render deploy in 25 days | The Hatch fix unblocked builds. Render simultaneously picked up the queued Groq migration (§13.4). |

**Lesson 1: liveness ≠ deploy-success.** UptimeRobot only knows the container is responding. It can't tell you which commit is running. **Action item:** wire up a deploy-success alert (Render webhook → Telegram, or a GitHub Action that polls Render's deploy API after each push).

**Lesson 2: pip's "exit code 1" footer is noise.** The real Python error always lives 30-50 lines higher in the build log. Train your eye to scroll up past pip's `note: This is an issue with the package mentioned above, not pip` and find the actual `ValueError:` / `ImportError:` / `ERROR: Could not build wheel for X`.

**Lesson 3: `pip install -e .` doesn't catch wheel-build issues.** Editable installs skip the wheel build entirely, so our local development never reproduced the Render failure. **Action item:** consider adding a `pytest` step that does `python -m build --wheel --no-isolation` to catch wheel issues in CI.

**Lesson 4: defensive redundancy can become a liability.** The original `force-include` was added "in case `packages` doesn't ship data files." It worked fine for ~a year. Then the build tool changed semantics. The cost of carrying redundant config was a 25-day silent outage of our CI/CD path.

### 13.4 June 26, 2026 — Groq model migration

The trigger that exposed everything in §13.3.

| Date | Commit | What | Why |
|---|---|---|---|
| 2026-06-25 | — | Groq emailed: `llama-3.1-8b-instant` deprecated today, hard decommission **2026-08-16** | Free/developer-tier users have 7 weeks of runway. After Aug 16, every Groq API call for that model returns 410 Gone, the bot crashes at intent-classify, and every Telegram message gets the apology handler — silent loss-of-service for the user. |
| 2026-06-26 | `20ffba4` | Migrated default model to `openai/gpt-oss-20b` in `config.py` + `groq_client.py` + `test_config.py` + docs | Groq's officially recommended replacement (their day-zero support for OpenAI's open-weights models). Same OpenAI-compatible JSON API → zero code-shape changes. Same free-tier limits (30 RPM / 6K TPM / 14,400 RPD). Same 128K context. 2.5× larger model (20B vs 8B) → more reliable structured JSON. ~2× throughput on Groq silicon. |
| 2026-06-26 | (deploy `f6b71a1` 12:15 AM) | New model effective in production | Render rebuilt from HEAD which included both `20ffba4` (model migration) and `f6b71a1` (Hatch unblock) — so both fixes deployed together. |

**Why we chose to NOT set `GROQ_MODEL` as a Render env var:** The code default in `config.py` is the source of truth. This means:
- Migrations only need a code push — no Render dashboard click that future-you might forget.
- The default visible to everyone reading the repo matches what's actually running.
- Setting it on Render would create two sources of truth that could diverge silently.

This design choice held up perfectly during this migration — the env-var-less approach meant a single git push was the whole migration.

**Lesson 1: assume your LLM provider will deprecate models faster than you'd like.** Groq gave 7 weeks. That's enough if you notice. It's catastrophic if your provider deprecation emails go to spam.

**Lesson 2: pre-existing `LLMClient` Protocol abstraction paid off.** Because every LLM provider already conformed to the same interface, swapping models was a one-line config change instead of a refactor. **Time-to-migrate: ~30 min including all the docs updates and 2 live verification calls.**

**Lesson 3: live verification matters even on "drop-in replacement" migrations.** Before pushing, ran `expense --extract "..."` twice — once for `log_expense` intent, once for `query_category_total` intent — to confirm the new model returned correctly-shaped JSON with reasonable confidence. Cost: 4 LLM calls, ~3 sec of laptop time. Insurance against the migration silently degrading extraction quality.

### 13.5 The pattern across all these changes

A few themes recur:

1. **Every "drop-in" change has a non-zero chance of breaking something.** Hatch was a "drop-in" build tool update. Groq's "drop-in" replacement model needed local verification. The keep-alive cron was a "drop-in" alternative until UptimeRobot's free tier turned out to lock HEAD-only.
2. **Observability gaps stay invisible until they bite.** We had 100% liveness uptime via UptimeRobot and 0% deploy-success visibility. The bot was "up" but stale for 25 days.
3. **Workarounds buy time; root-cause fixes buy peace of mind.** The pre-build-a-year script is a workaround for the burst-API-call issue. The real fix (Sheets `batchUpdate`) is still TODO. Each new month is a reminder.
4. **Documentation pays for itself the day you debug something you wrote 6 months ago.** Without this MASTER_GUIDE's runbook §9.x entries, every recurrence would be a new investigation. With them, it's a checklist.

### 13.6 Open TODOs (future maintenance debt)

| # | Item | Trigger to address |
|---|------|---------------------|
| 1 | Deploy-success alert (Render webhook → Telegram / GitHub Action polling Render API) | Before the next time a deploy silently fails |
| 2 | `python -m build --wheel` step in CI to catch wheel-build issues locally | Same as #1 — both prevent the silent failure pattern |
| 3 | Rebuild missing monthly tabs: October 2026, December 2026, May 2027 | Before each of those months arrives in real time |
| 4 | Replace `build_month_tab()` per-cell API calls with a single Sheets `batchUpdate` | Eliminates the 429 burst issue at its root → makes the year-prebuild script obsolete |
| 5 | Postgres mirror wiring (Supabase) — code is built, config not done | When SQL analytics over expense history becomes desired |
| 6 | LLM model deprecation alert (auto-check `/v1/models` weekly?) | Nice-to-have. Email is good enough until it isn't. |
| 7 | Receipt OCR HF Space (photo → bot logs the line) | When manual restaurant-receipt entry becomes annoying |

---

*End of Master Guide. If you hit a question this doesn't answer,
add it to §12 with the answer once you find it — that's how this
document stays useful long-term. If you make a production change
not yet captured here, add a new row to §13 with the **why** —
that's how this document compounds in value over time.*
