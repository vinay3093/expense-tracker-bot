# Architecture — one-page module map

A lightweight overview of *where things live* and *how they fit
together*.  For setup, operation, and the full dive on every
component, read [`HANDBOOK.md`](./HANDBOOK.md).

---

## The system in one diagram

```
                                  ┌─────────────────────────┐
                                  │  YOU on Telegram (any   │
                                  │  device, anywhere)      │
                                  └────────────┬────────────┘
                                               │
                                               ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │   src/expense_tracker/telegram_app/      Front-end              │
   │   ────────────────────────────────                              │
   │   ▸ bot.py            — message processors + command handlers   │
   │   ▸ auth.py           — allow-list of Telegram user IDs         │
   │   ▸ factory.py        — wires the python-telegram-bot SDK       │
   │   ▸ health_server.py  — tiny HTTP /health endpoint for hosts    │
   └─────────────────────────────┬───────────────────────────────────┘
                                 ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │   src/expense_tracker/pipeline/          Chat orchestration     │
   │   ────────────────────────────                                  │
   │   ▸ chat.py           — end-to-end chat → reply pipeline        │
   │   ▸ logger.py         — write side: extract → log to ledger     │
   │   ▸ retrieval.py      — read side: parse + filter + aggregate   │
   │   ▸ summary.py        — week / month / year totals + comparison │
   │   ▸ correction.py     — /undo + /edit                           │
   │   ▸ reply.py          — turn structured results into prose      │
   │   ▸ factory.py        — build all the above from settings       │
   └────────┬────────────────┬────────────────────┬───────────────────┘
            ▼                ▼                    ▼
   ┌────────────────┐ ┌────────────────┐ ┌──────────────────────────┐
   │ extractor/     │ │ llm/           │ │ ledger/                  │
   │ ─────────      │ │ ─────          │ │ ──────                   │
   │ ▸ intent       │ │ ▸ groq         │ │ ▸ base.py    Protocol +  │
   │ ▸ expense      │ │ ▸ ollama       │ │              data shapes │
   │ ▸ retrieval    │ │ ▸ openai       │ │ ▸ factory.py edition     │
   │ ▸ vocab/cats   │ │ ▸ anthropic    │ │              picker      │
   │   (YAML)       │ │ ▸ traced       │ │                          │
   │                │ │ ▸ json_repair  │ │ ┌─ sheets/    Google     │
   │                │ │                │ │ │  ▸ adapter             │
   │                │ │                │ │ │  ▸ backend / fake      │
   │                │ │                │ │ │  ▸ format / month /    │
   │                │ │                │ │ │    year / ytd builders │
   │                │ │                │ │ │  ▸ currency (FX cache) │
   │                │ │                │ │ │  ▸ credentials         │
   │                │ │                │ │ │                        │
   │                │ │                │ │ └─ nocodb/   Postgres    │
   │                │ │                │ │    ▸ adapter             │
   │                │ │                │ │    ▸ models (SQLAlchemy) │
   │                │ │                │ │    ▸ migrations (Alembic)│
   └────────────────┘ └────────┬───────┘ └────────┬─────────────────┘
                               │                  │
                               ▼                  ▼
                       ┌────────────────┐ ┌──────────────────────┐
                       │ Groq / Ollama  │ │ Google Sheets        │
                       │ / OpenAI / etc │ │ Postgres / Supabase  │
                       └────────────────┘ └──────────────────────┘

                       ┌─────────────────────────┐
                       │ storage/  (chat history │
                       │ JSONL — every chat turn │
                       │ + every LLM call traced)│
                       └─────────────────────────┘
```

---

## Where to look first

| Question | Start here |
|---|---|
| "How is a Telegram message turned into a Sheets row?" | `pipeline/chat.py` → `pipeline/logger.py` |
| "How does the LLM extract structured data from text?" | `extractor/expense.py` |
| "Where do my expense categories live?" | `extractor/data/categories.yaml` |
| "How does the bot read past expenses to answer questions?" | `pipeline/retrieval.py` |
| "How does Sheets vs Postgres get picked at runtime?" | `ledger/factory.py` |
| "How do I add a new LLM provider?" | `llm/factory.py` + new file in `llm/` |
| "How does the bot know who's allowed to talk to it?" | `telegram_app/auth.py` |
| "How is the monthly tab in Sheets generated?" | `ledger/sheets/month_builder.py` + `format.py` |
| "How does INR → USD conversion work?" | `ledger/sheets/currency.py` |
| "Where is every CLI flag defined?" | `__main__.py` |
| "Where is every config/env-var declared?" | `config.py` |

---

## The 3 storage editions

All implement the same `LedgerBackend` Protocol from
`src/expense_tracker/ledger/base.py`.

| Edition | When to use | Module |
|---|---|---|
| **sheets** *(default)* | Personal use, phone access via the Sheets app, zero infra | `ledger/sheets/` |
| **nocodb / postgres** | Long-term scale, NocoDB UI, audit log, soft-delete | `ledger/nocodb/` |
| **mirror** *(coming next)* | Best of both: Sheets for phone, Postgres as durable backup + future analytics | `ledger/mirror/` |

Switch with one env var: `STORAGE_BACKEND=sheets | nocodb | mirror`.

---

## Layers, in dependency order (top depends on bottom)

```
   ┌────────────────────────────────────────────────────────────┐
   │ telegram_app/   ← entry point for chat                     │
   │ __main__.py     ← entry point for CLI                      │
   ├────────────────────────────────────────────────────────────┤
   │ pipeline/       ← chat orchestration, reply formatting     │
   ├────────────────────────────────────────────────────────────┤
   │ extractor/      ← LLM + categories + parsing               │
   ├────────────────────────────────────────────────────────────┤
   │ ledger/         ← storage abstraction (Sheets / Postgres)  │
   │ llm/            ← provider clients (Groq / Ollama / ...)   │
   │ storage/        ← chat-history JSONL append-only log       │
   ├────────────────────────────────────────────────────────────┤
   │ config.py       ← settings; everything reads this          │
   └────────────────────────────────────────────────────────────┘
```

**Rules of the architecture**:

- Higher layers may import from lower; the reverse is forbidden.
- The pipeline depends on `LedgerBackend` *as a Protocol*, not on
  any specific edition.  This is what makes a 3rd backend (mirror,
  Notion, anything new) cheap to add.
- LLM clients are duck-typed against `LLMClient` — same trick.
- The CLI in `__main__.py` is the one place wiring happens.  Tests
  build the same objects directly via `pipeline/factory.py` and
  `ledger/factory.py`.

---

## Tests

Flat `tests/` directory, one file per module under test.  All hermetic
— no real LLM calls (`FakeLLMClient`), no real Sheets calls
(`FakeSheetsBackend`), no real Postgres (SQLite in-memory).  Run
with:

```bash
pytest -q
```

Test count: ~520, all green; ruff + isort clean.

---

## Deploy bundles — `deploy/`

Each subfolder is a self-contained production deployment recipe for a
specific host + edition combination.

| Folder | Host | Storage |
|---|---|---|
| `render-edition/` | Render Free (recommended 24/7 host — Telegram works) | Sheets |
| `huggingface-edition/` | Hugging Face Spaces (kept for fork authors; **HF blocks Telegram**) | Sheets |
| `sheets-edition/` | Oracle Cloud Free / any Ubuntu VM | Sheets |
| `nocodb-edition/` | Oracle Cloud Free + docker-compose | Postgres + NocoDB |

Each contains a runbook (`DEPLOY.md`), idempotent `setup.sh`, and
either a `systemd` unit or a `Dockerfile`.

---

## What lives at the repo root (and why)

| File | Required at root by | Purpose |
|---|---|---|
| `pyproject.toml` | Python convention | Package metadata + deps |
| `Dockerfile` | Render / Koyeb / Hugging Face | Container build (edition-agnostic) |
| `.dockerignore` | Docker | Keeps secrets / docs / logs out of the image |
| `alembic.ini` | Alembic convention | Postgres migrations entry point |
| `.github/workflows/` | GitHub Actions | CI + Render keep-alive |
| `LICENSE` | OSS convention | MIT |
| `README.md` | GitHub convention | Front door |
| `docs/` | Us | All other documentation |

If a file isn't required at the root by an external tool, it lives
under `docs/`, `deploy/`, `scripts/`, `src/`, or `tests/`.
