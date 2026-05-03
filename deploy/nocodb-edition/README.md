# `deploy/nocodb-edition/` — Postgres + NocoDB deploy bundle

Everything needed to run the **Postgres + NocoDB edition** of the
bot 24/7 on a free Oracle ARM VM (or any Ubuntu host with Docker).

> Want the **Google Sheets edition** instead?  See
> [`../sheets-edition/`](../sheets-edition/).  The two editions
> share 100% of the chat / LLM / Telegram code; only the storage
> destination differs.

| File | What |
|---|---|
| [`DEPLOY.md`](./DEPLOY.md) | **Read this first.**  Step-by-step runbook from "I'm signing up for OCI" to "the bot is replying to my phone *and* I'm browsing rows in NocoDB." |
| `docker-compose.yml` | Two-service stack: Postgres 16 + NocoDB.  Volume-mounted, restart-unless-stopped. |
| `setup.sh` | First-time bootstrap.  Installs Docker + Python venv, generates random secrets, starts containers, runs migrations, links the systemd unit. |
| `update.sh` | Pull latest code + run any new Alembic migrations + restart the bot. |
| `expense-bot.service` | Hardened systemd unit.  Waits for Postgres to be reachable before starting. |

---

## Architecture (one diagram)

```
                ┌──────────────────────────────────────┐
                │         your phone (Telegram)        │
                └──────────────────┬───────────────────┘
                                   │ long-poll (HTTPS, no inbound port)
                                   ▼
              ┌─────────────────────────────────────────────┐
              │    expense --telegram   (systemd, Python)   │
              │    chat → LLM → ledger.append → reply       │
              └─────────────────────────┬───────────────────┘
                                        │ DATABASE_URL
                                        ▼
                  ┌──────────────────────────────────────┐
                  │  expense-postgres   (docker-compose) │
                  │   transactions, transactions_audit_log│
                  └────────────────────┬─────────────────┘
                                       │
                          ┌────────────┴───────────┐
                          ▼                        ▼
                ┌──────────────────┐     ┌──────────────────┐
                │ expense-nocodb   │     │ pg_dump backups  │
                │ http://...:8080  │     │ (cron, weekly)   │
                └──────────────────┘     └──────────────────┘
```

---

## TL;DR

```bash
# On the VM, after cloning the repo:
bash deploy/nocodb-edition/setup.sh

# Edit ~/expense-tracker-bot/.env:
#   STORAGE_BACKEND=nocodb
#   DATABASE_URL=postgresql+psycopg://expense:<from .env-deploy>@127.0.0.1:5432/expense
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_ALLOWED_USERS=<your numeric ID>
#   GROQ_API_KEY=...

chmod 600 ~/expense-tracker-bot/.env
sudo systemctl enable --now expense-bot
sudo journalctl -u expense-bot -f

# In your browser, go to http://<vm-public-ip>:8080 to use NocoDB.
```

For the long version with explanations, see
[`DEPLOY.md`](./DEPLOY.md).

---

## Why this edition exists

The Sheets edition is great for:

* Zero-infra setup (just a Google account).
* Anyone you'd share the spreadsheet with already knows how to
  read a spreadsheet.

The NocoDB / Postgres edition is great for:

* **Long-term scale.**  Sheets gets sluggish past a few thousand
  rows; Postgres laughs at millions.
* **Real types + indexes.**  Date filtering, category drill-downs,
  and YTD reports are SQL queries — instant, no formula re-eval.
* **Audit log.**  Every insert / update / delete writes a row to
  `transactions_audit_log` with the before/after JSON.  Free
  forensic trail.
* **Soft deletes.**  `/undo` doesn't physically remove the row; it
  flips a `deleted_at` flag, so a future "undo undo" is trivial.
* **NocoDB UI.**  Same spreadsheet feel, but with proper joins,
  views, and shareable links.

Both editions write through the **exact same chat pipeline** — the
LLM extractor, currency converter, expense logger, retrieval
engine, summary engine, and Telegram bot are unchanged.  Only the
last 5 cm of plumbing (the `LedgerBackend`) differs.

---

## Secret hygiene

Nothing in this folder contains real credentials.  When you run
`setup.sh`, it generates `deploy/nocodb-edition/.env-deploy` with
strong random `POSTGRES_PASSWORD` and `NC_AUTH_JWT_SECRET` (chmod
600).  That file is in `.gitignore` and never committed.
