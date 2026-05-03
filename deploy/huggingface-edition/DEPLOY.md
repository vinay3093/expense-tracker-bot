# Hugging Face Spaces — full deployment runbook

End-to-end click-by-click.  Time budget: **15 minutes** if you have
your secrets handy, **30 minutes** if you're collecting them as you go.

---

## Step 0 · Prerequisites (5 min, one-time)

You should already have all of these from the laptop setup.  If not,
collect them in a scratchpad before continuing:

| Secret | Where to get it | Required? |
|----|----|----|
| `TELEGRAM_BOT_TOKEN` | DM `@BotFather` → `/newbot` (or `/token` for an existing one) | yes |
| `TELEGRAM_ALLOWED_USERS` | Your Telegram user ID (run `/whoami` against your bot once) | yes |
| `EXPENSE_SHEET_ID` | The long token between `/spreadsheets/d/` and `/edit` in your Google Sheet URL | yes |
| `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT` | The **full contents** of `secrets/service-account.json` — copy/paste the whole `{ ... }` blob | yes |
| `GROQ_API_KEY` | https://console.groq.com/keys → Create API Key | yes |
| `LLM_PROVIDER` | Always `groq` for HF (Ollama isn't available without a GPU) | yes |
| `TIMEZONE` | Your IANA TZ, e.g. `America/Chicago` | recommended |
| `DEFAULT_CURRENCY` | `USD` or `INR` etc. | recommended |

> **Don't have these yet?** See the laptop setup section in `HANDBOOK.md`
> chapters 4 (Sheets) and 7 (Telegram) — they cover every screen.

You'll also need:

* A **GitHub account** with this repo pushed (for the keep-alive cron).
* A **Hugging Face account** — sign up free at https://huggingface.co/join
  (no credit card asked, ever).

---

## Step 1 · Create a write token on Hugging Face (1 min)

1. Log in → https://huggingface.co/settings/tokens
2. Click **"New token"**.
3. Name: `expense-bot-deploy`
4. Type: **Write** (Read isn't enough — you need to push code).
5. Click **Create token**.
6. **Copy it immediately** (it's shown once).  Paste somewhere safe.

---

## Step 2 · Create the Space (2 min)

1. Go to https://huggingface.co/new-space
2. Fill in:
   * **Owner**: your username
   * **Space name**: `expense-bot` (any name works — this becomes the URL)
   * **License**: MIT (or whatever you prefer)
   * **Select the Space SDK**: **Docker** → **Blank**
   * **Space hardware**: **CPU basic · 2 vCPU · 16 GB · FREE**
   * **Visibility**: **Public** (free tier doesn't allow private Spaces)
3. Click **Create Space**.

You're now sitting on an empty Space at:
`https://huggingface.co/spaces/<your-username>/expense-bot`

---

## Step 3 · Add the secrets (3 min)

In the Space you just created, click **Settings** (gear icon, top right) →
scroll to the **"Variables and secrets"** section.

Add each row from `secrets-checklist.md` as a **Secret** (NOT a
"Variable" — Variables are visible in build logs; Secrets aren't):

```
Name                                    Value
─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN                      <token from BotFather>
TELEGRAM_ALLOWED_USERS                  <your Telegram user ID>
GROQ_API_KEY                            <key from console.groq.com>
EXPENSE_SHEET_ID                        <ID from Sheet URL>
GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT     <paste the full JSON blob>
LLM_PROVIDER                            groq
TIMEZONE                                America/Chicago
DEFAULT_CURRENCY                        USD
```

> **Special handling for `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT`:**
> When pasting, keep the JSON as-is — newlines inside `private_key`
> can be either real `\n` characters or literal `\\n` escapes; both
> work.  The bot validates the JSON parses on startup and writes it
> to a temp file at chmod 600.  If you misformat it, the deploy
> logs will say *"GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT is not valid JSON"*
> with the exact parse error.

---

## Step 4 · Push the code (3 min)

From this repo on your laptop:

```bash
# Add the HF Space as a git remote.  Username is your HF username,
# NOT your GitHub username.  The token from Step 1 is the password.
git remote add huggingface https://huggingface.co/spaces/<username>/expense-bot

# When prompted for a password, paste the WRITE TOKEN from Step 1
# (not your account password — Hugging Face deprecated that login).
git push huggingface feat/nocodb-edition:main

# Tip: to avoid pasting the token every push, store it once with:
#   git credential-store store
```

Watch the **"Build logs"** tab on the Space page.  You should see:

1. `[builder] Step 1/X: FROM python:3.11-slim AS builder` ...
2. `[runtime] Step 1/X: FROM python:3.11-slim AS runtime` ...
3. `Successfully built ...`
4. The container starts; the **"App"** tab on the Space goes live.

If the App tab shows `alive` at the URL, the bot is up.

---

## Step 5 · Smoke-test the bot (1 min)

Open Telegram, find your bot, send:

```
/start
/whoami
today I spent 5 dollars on coffee
/last
```

Expected: bot replies, the expense lands in the `Transactions` tab
of your Google Sheet within ~3 seconds.

If it doesn't reply, see **Troubleshooting** below.

---

## Step 6 · Set up the keep-alive cron (3 min)

Without this, the Space will sleep after 48 hours of true idle.
With it, the Space gets a `/health` ping every 36 hours and stays
awake forever.

The workflow file is already in this repo at
`.github/workflows/keep-hf-alive.yml`.  You just need to:

1. Push it to GitHub if you haven't:
   ```bash
   git push origin feat/nocodb-edition
   ```
2. In your **GitHub repo** → **Settings** → **Secrets and variables**
   → **Actions** → **New repository secret**:
   * **Name**: `HF_SPACE_URL`
   * **Value**: `https://<your-username>-expense-bot.hf.space`
     (NOT the huggingface.co/spaces/... URL — the *.hf.space one)
3. Manually trigger the workflow once to confirm it runs:
   * GitHub repo → **Actions** tab → **"Keep HF Space awake"** →
     **Run workflow**.
4. Verify: the workflow should finish green in ~5 seconds and
   `https://<your-space>.hf.space/health` should return `ok`.

The cron then runs automatically every 36 hours.  Free for life.

---

## Troubleshooting

### Bot doesn't reply on Telegram

1. **Check build logs** (Space → "Logs" tab → "Build logs").  If
   you see `ModuleNotFoundError`, the build failed before the
   container started.
2. **Check container logs** (Space → "Logs" tab → "Container logs").
   Look for `Telegram bot starting (long-polling)`.  If you see
   `TELEGRAM_BOT_TOKEN is not set`, the secret didn't propagate —
   click **"Restart this Space"** in Settings.
3. **Check `/whoami`**.  If the bot replies but says you're not on
   the allow-list, copy the user ID from the reply into
   `TELEGRAM_ALLOWED_USERS` and restart.

### "Service-account JSON is not valid JSON"

The most common cause is HF stripping line endings when you paste a
multi-line value.  Fix:

1. On your laptop, run `cat secrets/service-account.json` — copy the
   entire output.
2. In HF Settings → Secrets → edit `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT`
   → paste fresh.
3. Restart the Space.

### `/health` returns 404 or doesn't respond

The container is alive but the health server isn't up.  Almost
always means `TELEGRAM_HEALTH_PORT` got overridden — confirm it's
**not** set as an HF Variable (the Dockerfile sets it to `7860`
via `$PORT` already).

### The Space keeps restarting

Check the container logs for the *root cause* (almost always a
missing or malformed secret).  HF restarts the container endlessly
until startup succeeds.

### Space went to sleep anyway

* Confirm the GH Actions workflow has run successfully in the past
  48 hours (Actions tab → workflow runs).
* Double-check `HF_SPACE_URL` is the `*.hf.space` URL, not the
  `huggingface.co/spaces/...` one (the latter is HTML; the former
  is your container).
* Manually trigger the workflow to wake it up.

---

## Updating the bot

After any code change on your laptop:

```bash
git push origin feat/nocodb-edition       # GitHub
git push huggingface feat/nocodb-edition:main   # Hugging Face → triggers rebuild
```

HF rebuilds the image (~2 min) and swaps in the new container with
zero downtime on your end.

---

## What about the Postgres edition?

The Postgres edition (NocoDB UI + multi-currency dashboards) is the
upgrade path — but Hugging Face Spaces is the wrong host for it.
NocoDB needs persistent storage, which HF doesn't provide on the
free tier.

When you're ready for it, deploy the Postgres edition to:

* **Oracle Free** when capacity opens up (see `deploy/nocodb-edition/`)
* **Render Pro** ($7/mo) for managed Postgres + always-on container
* **A $5/mo VPS** (Hetzner / DigitalOcean) for full control

Until then, the Sheets edition on Hugging Face IS the 24/7 personal
bot.  Run it as long as you like.
