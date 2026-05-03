# `deploy/nocodb-edition/` — runbook

How to host the **NocoDB / Postgres edition** of the expense tracker
on a free Oracle Cloud (or any Ubuntu) VM, end-to-end.

> Want the **Google Sheets edition** instead?  See
> [`../sheets-edition/DEPLOY.md`](../sheets-edition/DEPLOY.md).
> The chat / LLM / Telegram code is identical between the two; only
> the storage destination differs.

---

## What you'll have when you're done

* A Postgres 16 database running on the VM, holding every expense
  in a typed schema with full audit log.
* A NocoDB UI at `http://<vm-public-ip>:8080` for browsing /
  editing rows visually (think: Airtable for your own data).
* The `expense --telegram` bot running 24/7 under systemd, talking
  to the same Postgres database.
* All three survive crashes and reboots automatically.

---

## Prerequisites

* An Oracle Cloud Free Tier account (or any Ubuntu 22.04+ VM with
  ≥ 1 GB RAM and a public IP).
* SSH access to the VM as the `ubuntu` user (Oracle's default).
* Inbound port 22 (SSH) and 8080 (NocoDB UI) open in your security
  list / firewall.  The bot does NOT need any inbound port — it
  long-polls Telegram.
* A Telegram bot token (talk to [@BotFather](https://t.me/BotFather)
  on Telegram to create one in 30 seconds).
* An LLM API key — Groq is free; OpenAI / Anthropic also supported.
* (Optional) A Supabase / Neon URL if you'd rather use a managed
  Postgres instead of running one on the VM.  Skip steps 4 and 5
  below in that case.

---

## Step 1.  SSH to the VM and clone the repo

```bash
ssh ubuntu@<vm-public-ip>
sudo apt-get update -qq && sudo apt-get install -y git
git clone https://github.com/<you>/expense-tracker-bot.git
cd expense-tracker-bot
```

---

## Step 2.  Run the bootstrap script

```bash
bash deploy/nocodb-edition/setup.sh
```

The script (idempotent — safe to re-run) does:

1. Installs Docker + the Compose plugin.
2. Installs Python 3.10+ + creates `./.venv`.
3. `pip install -e ".[telegram,nocodb]"`.
4. Generates `deploy/nocodb-edition/.env-deploy` with strong random
   `POSTGRES_PASSWORD` and `NC_AUTH_JWT_SECRET` (chmod 600, never
   committed).
5. Runs `docker compose up -d` — starts `expense-postgres` +
   `expense-nocodb`.
6. Waits for Postgres to come healthy, then runs `alembic upgrade
   head` — creates the `transactions` and `transactions_audit_log`
   tables.
7. Symlinks the systemd unit into `/etc/systemd/system/`.

When it's done you'll see a "Next steps" box.  The script does NOT
start the bot yet — that needs your Telegram token first.

---

## Step 3.  Configure `.env`

Create `~/expense-tracker-bot/.env` with at least:

```bash
# Storage selection
STORAGE_BACKEND=nocodb
DATABASE_URL=postgresql+psycopg://expense:<paste from .env-deploy>@127.0.0.1:5432/expense

# Telegram
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_ALLOWED_USERS=<your numeric Telegram user ID>

# LLM
LLM_PROVIDER=groq
GROQ_API_KEY=<from https://console.groq.com/keys>
GROQ_MODEL=llama-3.3-70b-versatile
```

Then lock it down:

```bash
chmod 600 ~/expense-tracker-bot/.env
```

> **Tip:** the `POSTGRES_PASSWORD` is in `deploy/nocodb-edition/.env-deploy`
> (also chmod 600).  Copy it from there into `DATABASE_URL`.

---

## Step 4.  Start the bot

```bash
sudo systemctl enable --now expense-bot
sudo journalctl -u expense-bot -f
```

You should see "Telegram bot started; waiting for updates" within a
few seconds.  Send `/start` to your bot from Telegram to confirm it
replies.

---

## Step 5.  (Optional) Open the NocoDB UI

Browse to `http://<vm-public-ip>:8080`.

* Create the admin account (any email + password — local-only).
* Click "+ New Project" → "Connect to a Database" → "Postgres".
* Use these connection settings:

  | Field    | Value                                                  |
  |----------|--------------------------------------------------------|
  | Host     | `expense-postgres`  (the docker service name)          |
  | Port     | `5432`                                                 |
  | User     | `expense`                                              |
  | Password | (from `.env-deploy`)                                   |
  | Database | `expense`                                              |

* You'll see the `transactions` and `transactions_audit_log`
  tables.  NocoDB renders them as spreadsheets — you can sort,
  filter, edit, build views, etc.  Anything you change in NocoDB
  is visible to the bot immediately (and vice-versa).

> **Security note:** NocoDB's UI is exposed on port 8080 over
> HTTP.  For long-term use, run it behind Caddy or Nginx with a
> Let's Encrypt cert, or restrict port 8080 to your home IP in
> the OCI security list.

---

## Step 6.  (Optional) Migrate existing data from the Sheets edition

If you've been using the Sheets edition and want to bring that
history over:

```bash
# On the VM, with both .env vars present:
~/expense-tracker-bot/.venv/bin/expense --migrate-sheets-to-postgres
```

This reads every active row from the Sheets `Transactions` tab and
inserts it into the Postgres `transactions` table.  Refuses to run
if Postgres already has rows (pass `--migrate-force` to override).

---

## Day-to-day operations

| Want to ...               | Run                                                                            |
|---------------------------|--------------------------------------------------------------------------------|
| **Tail the bot logs**     | `sudo journalctl -u expense-bot -f`                                            |
| **Restart the bot**       | `sudo systemctl restart expense-bot`                                           |
| **Deploy new code**       | `cd ~/expense-tracker-bot && bash deploy/nocodb-edition/update.sh`             |
| **Stop everything**       | `sudo systemctl stop expense-bot && sudo docker compose down`                  |
| **Start everything**      | `sudo docker compose up -d && sudo systemctl start expense-bot`                |
| **DB shell**              | `sudo docker exec -it expense-postgres psql -U expense -d expense`             |
| **Check schema**          | `~/expense-tracker-bot/.venv/bin/expense --postgres-health`                    |
| **Run any new migration** | `cd ~/expense-tracker-bot && alembic -c alembic.ini upgrade head`              |

---

## Backups

Two ways to back up your data:

* **Quick:** `docker exec expense-postgres pg_dump -U expense expense > backup.sql`
* **Periodic:** add a daily cron job that does the above and rotates
  the last 7 dumps.  The `audit_log` table makes deletes recoverable
  even without dumps, but dumps are still cheap insurance.

---

## Troubleshooting

| Symptom                                              | Likely cause                                                                               |
|------------------------------------------------------|--------------------------------------------------------------------------------------------|
| `expense --postgres-health` says "schema MISSING"    | Migrations haven't run — `alembic -c alembic.ini upgrade head`.                            |
| Bot logs "OperationalError: could not connect"       | Postgres container isn't up — `sudo docker compose ps`; restart with `up -d`.              |
| NocoDB UI is unreachable                             | Port 8080 closed in OCI security list, or NocoDB container exited — `docker logs`.         |
| Telegram messages aren't being received              | `TELEGRAM_BOT_TOKEN` wrong, or your user ID isn't in `TELEGRAM_ALLOWED_USERS`.             |
| LLM extraction returns nothing                       | `GROQ_API_KEY` missing / quota exceeded.  Test with `expense --ping-llm`.                  |
| First boot fails with "psql command not found"       | `sudo apt-get install postgresql-client` (only needed for the systemd `ExecStartPre`).     |
