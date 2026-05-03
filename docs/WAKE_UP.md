# WAKE UP — morning checklist

You went to sleep with the NocoDB / Postgres edition fully wired
**and** the Hugging Face Spaces 24/7 hosting bundle ready to push.
Here's exactly what to do today, in priority order.

This file is on the `feat/nocodb-edition` branch.  Once you're happy,
merge into `main`.

---

## ⭐ TOP PRIORITY — get the bot running 24/7 on Hugging Face

This is the path to making the Telegram bot answer your messages
*even when your laptop is closed*.  All the code is already in place
(`Dockerfile`, env-var credentials, health server, deploy bundle,
GitHub Actions cron).  You just need to do 3 click-paths:

### 1. Create a Hugging Face account (2 min, no credit card)

* https://huggingface.co/join → sign up → verify email.
* Pick a username (becomes the Space URL).

### 2. Create a write token (1 min)

* https://huggingface.co/settings/tokens → "New token"
  → name `expense-bot-deploy`, type **Write** → Create.
* **Copy the token** — it's shown once.

### 3. Follow the click-by-click runbook (12 min)

Open: **[`deploy/huggingface-edition/DEPLOY.md`](../deploy/huggingface-edition/DEPLOY.md)**

It walks you through:

* Creating the Space (Docker SDK, free CPU tier).
* Pasting 5–8 secrets into the Space Settings.
* `git push huggingface feat/nocodb-edition:main` to trigger the
  build.
* Setting up the GitHub Actions keep-alive (`HF_SPACE_URL` repo
  secret) so the Space never sleeps.
* Smoke-testing with `/whoami` and "today I spent $5 on coffee".

When you finish, you should be able to **close your laptop, walk
away, send a Telegram message from your phone in Texas / anywhere in
the world**, and the bot replies + writes to Sheets.

That's the whole point.  Do this first.

The Postgres / NocoDB stuff below is the *upgrade path* for later —
once you have NocoDB-style dashboards on top.  Ignore it today
unless you specifically want to play with that.

---

## What's already done (you can skim past)

* New branch `feat/nocodb-edition` (4 commits ahead of `main`).
* Existing code reorganised under `src/expense_tracker/ledger/sheets/`.
* New `LedgerBackend` Protocol that abstracts storage.
* Brand-new `PostgresLedgerBackend` (SQLAlchemy 2.0, soft-delete,
  audit log, cross-dialect — works against SQLite for tests too).
* Alembic migrations + `alembic.ini` at repo root.
* Three new CLI commands:
  * `expense --init-postgres`
  * `expense --postgres-health`
  * `expense --migrate-sheets-to-postgres`
* New deploy bundle: `deploy/nocodb-edition/` (docker-compose for
  Postgres + NocoDB, setup.sh, runbook, systemd unit).
* Old `deploy/oracle/` renamed to `deploy/sheets-edition/`.
* 508 tests passing, ruff clean.

The Telegram bot you've been chatting with **still works exactly
the same** — the chat pipeline is unchanged.  All the new stuff is
behind one env var: `STORAGE_BACKEND=nocodb`.

---

## Step 0 — Sanity check (1 minute)

On your laptop:

```bash
cd ~/Documents/personal_github/expense-tracker-bot
git status              # should be on feat/nocodb-edition, clean
source .venv/bin/activate
python -m pytest -q    # should print "508 passed"
expense --version
```

If anything is red here, **stop and re-read this file** — don't
proceed until tests are green.

---

## Step 1 — Choose your Postgres host (5 minutes)

Three options, pick one:

### Option A — Supabase free tier (easiest, recommended)

1. Sign up at <https://supabase.com>.
2. Create a new project (name: `expense-tracker`).  Pick the
   region closest to you.  Set a strong DB password — write it
   down somewhere safe.
3. Wait ~2 minutes for the project to provision.
4. Project Settings → Database → "Connection pooling" tab →
   **Transaction pooler** mode → copy the connection string.  It
   looks like:

   ```
   postgresql://postgres.<project>:<password>@aws-0-us-east-1.pooler.supabase.com:6543/postgres
   ```

5. Convert it for our SQLAlchemy + psycopg driver:

   ```
   postgresql+psycopg://postgres.<project>:<password>@aws-0-us-east-1.pooler.supabase.com:6543/postgres
   ```

   (only the `postgresql://` prefix changes to `postgresql+psycopg://`).

### Option B — Local Docker Postgres (free, runs on your laptop)

```bash
docker run -d --name expense-pg \
    -e POSTGRES_USER=expense \
    -e POSTGRES_PASSWORD=changeme \
    -e POSTGRES_DB=expense \
    -p 127.0.0.1:5432:5432 \
    postgres:16-alpine
```

Then your URL is:

```
postgresql+psycopg://expense:changeme@127.0.0.1:5432/expense
```

### Option C — Oracle VM (production target)

Skip ahead to Step 5; the deploy bundle handles everything.

---

## Step 2 — Wire `.env` (1 minute)

Open `~/Documents/personal_github/expense-tracker-bot/.env` and add
(or update):

```bash
STORAGE_BACKEND=nocodb
DATABASE_URL=postgresql+psycopg://...   # paste from Step 1
```

Leave the existing Sheets settings untouched — they're ignored when
`STORAGE_BACKEND=nocodb`, but you'll want them back if you ever
want to switch.

> **Tip:** to switch back to Sheets at any time, just change
> `STORAGE_BACKEND=sheets`.  The bot will read+write Sheets again
> on the next start.

---

## Step 3 — Bootstrap the schema (30 seconds)

Make sure deps are installed (the `[nocodb]` extra ships SQLAlchemy
+ psycopg + alembic):

```bash
pip install -e ".[nocodb]"   # or .[all] if you want everything
```

Then create the schema:

```bash
expense --init-postgres
```

You should see:

```
Engine   : postgresql+psycopg://...
Creating tables: transactions, transactions_audit_log ...
Schema present : True
Active rows    : 0
```

Verify health:

```bash
expense --postgres-health
```

---

## Step 4 — (Optional) Move existing Sheets data over

If you want your existing 1-2 expense rows from Sheets to come into
Postgres:

```bash
# Temporarily flip back to Sheets to read FROM, but the migrate command
# reads from Sheets and writes to Postgres in one shot regardless of
# STORAGE_BACKEND, so this just works:
expense --migrate-sheets-to-postgres
```

It refuses if Postgres already has rows (safety).  Pass
`--migrate-force` to override.

---

## Step 5 — Test the chat pipeline against Postgres (1 minute)

```bash
# Try a logging round-trip:
expense --chat "spent 7 dollars on coffee today"

# Then a query:
expense --chat "how much did I spend today?"

# Then check the audit trail:
expense --postgres-health
```

You should see the row appear (active count goes up).  Try `/undo`
in Telegram or `expense --undo` in the CLI; the row stays in the
database (with `deleted_at` set), and the active count drops by one.

---

## Step 6 — (Optional) Deploy to Oracle Cloud

If you've signed up for Oracle Cloud and have a VM running:

```bash
# On the VM:
git clone https://github.com/<you>/expense-tracker-bot.git
cd expense-tracker-bot
git checkout feat/nocodb-edition  # or main, after you merge

# For NocoDB / Postgres edition (recommended for long-term):
bash deploy/nocodb-edition/setup.sh
# ... then follow the on-screen "Next steps" output.

# OR for the Sheets edition (simpler, no docker):
bash deploy/sheets-edition/setup.sh
```

Both runbooks are at:

* `deploy/sheets-edition/DEPLOY.md`
* `deploy/nocodb-edition/DEPLOY.md`

---

## Step 7 — Merge & push

When you're happy:

```bash
git checkout main
git merge --no-ff feat/nocodb-edition
git push origin main
git push origin feat/nocodb-edition  # if you want to keep the branch around
```

---

## Cheat sheet — useful new commands

| Command | What it does |
|---|---|
| `expense --init-postgres` | Create schema in DATABASE_URL (idempotent) |
| `expense --postgres-health` | Connectivity ping + row count |
| `expense --migrate-sheets-to-postgres` | One-shot data move |
| `expense --migrate-sheets-to-postgres --migrate-force` | Override "destination already has rows" check |
| `alembic -c alembic.ini upgrade head` | Apply any pending migrations (production path) |
| `alembic -c alembic.ini current` | Which migration is currently applied |
| `alembic -c alembic.ini history` | All migrations |
| `STORAGE_BACKEND=sheets expense --chat "..."` | Force Sheets edition for one command |
| `STORAGE_BACKEND=nocodb expense --chat "..."` | Force Postgres edition for one command |
| `git push huggingface feat/nocodb-edition:main` | Deploy / update the HF Space |
| `gh workflow run keep-hf-alive.yml` | Manually wake the HF Space |
| `curl https://<space>.hf.space/health` | Liveness check (returns `ok`) |
| `docker build -t expense-bot . && docker run -p 7860:7860 --env-file .env expense-bot` | Local parity test of the HF image |
| `STORAGE_BACKEND=mirror expense --chat "..."` | Test mirror dual-write for one message |
| `expense --reconcile-dry-run` | Preview drift between Sheets + Postgres (mirror mode) |
| `expense --reconcile` | Back-fill rows missing from secondary (mirror mode) |

---

## If anything goes wrong

* `pytest -q` should always pass — if not, something env-specific
  changed overnight.  Re-run `pip install -e ".[dev,nocodb]"`.
* If `expense --init-postgres` fails with "DATABASE_URL is not
  set", check `.env` and run `python -c "from expense_tracker.config import get_settings; print(get_settings().DATABASE_URL)"`.
* If Supabase rejects the connection: confirm you used the
  **Transaction pooler** URL (port 6543, not 5432), and the
  `postgresql+psycopg://` prefix.
* If you see "prepared statement already exists" against Supabase:
  that's the pooler complaining about double-pooling.  The factory
  *should* detect Supabase and use NullPool — file an issue if it
  doesn't.

Have a great morning.  All the heavy lifting is done — Steps 1-3
above should take under 10 minutes total.
