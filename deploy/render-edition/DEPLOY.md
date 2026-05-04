# Render — full deployment runbook

End-to-end click-by-click.  Time budget: **15 minutes** if you have
your secrets handy from the Hugging Face attempt, **25 minutes** if
you're collecting them from scratch.

> **Why Render and not Hugging Face?**  HF Spaces blocks all
> outbound traffic to `api.telegram.org` as a security policy
> (verified May 2026 from official HF forum threads).  Render
> places no such restriction; thousands of Telegram bots run on
> it.  Same Dockerfile, same secrets, same code — different host.

---

## Step 0 · Prerequisites (5 min, one-time)

You already have most of these from the HF attempt.  If not, collect
them in a scratchpad now:

| Secret | Where to get it | Required? |
|----|----|----|
| `TELEGRAM_BOT_TOKEN` | DM `@BotFather` → `/newbot` (or `/token` for an existing bot) | yes |
| `TELEGRAM_ALLOWED_USERS` | Your Telegram user ID (DM the bot once and run `/whoami`) | yes |
| `EXPENSE_SHEET_ID` | The long token between `/spreadsheets/d/` and `/edit` in your Sheet URL | yes |
| `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT` | The **full contents** of `secrets/service-account.json` — copy/paste the entire `{ ... }` blob | yes |
| `GROQ_API_KEY` | https://console.groq.com/keys → Create API Key | yes |
| `LLM_PROVIDER` | Always `groq` for hosted runs (Ollama needs a GPU) | yes |
| `TIMEZONE` | Your IANA TZ, e.g. `America/Chicago` | recommended |
| `DEFAULT_CURRENCY` | `USD` or `INR` etc. | recommended |

> **Don't have these yet?**  See `docs/HANDBOOK.md` chapters 4
> (Sheets) and 7 (Telegram) for the original from-zero collection.

You'll also need:

* A **GitHub account** with this repo pushed (Render reads from
  GitHub directly — same as we did with HF).
* A **Render account** — sign up free at https://render.com.
  No credit card required for the free tier.

---

## Step 1 · Create your Render account (1 min)

1. Open https://render.com.
2. Click **"Get Started for Free"** (top right).
3. Pick **"Sign up with GitHub"** (fastest — auto-links the
   repo connection we'll need in Step 3).
4. Authorise Render's GitHub App on the next screen — you can
   pick "Only select repositories" and choose just
   `expense-tracker-bot`.
5. You're dropped into the Render dashboard.

---

## Step 2 · Create the Web Service (3 min)

You have two paths here.  **Path A (Blueprint)** is one-click but
new accounts sometimes hit a "free plan blueprints disabled" wall;
in that case fall back to **Path B (Manual UI)** which always works.

### Path A — Blueprint (try this first)

1. In the Render dashboard, click **"New +"** (top right) →
   **"Blueprint"**.
2. Connect your `expense-tracker-bot` repo.
3. Render reads `deploy/render-edition/render.yaml`, shows a
   preview ("1 service: expense-bot — Web Service, Docker, Free").
4. Click **"Apply"**.
5. Skip to Step 3 (env vars).

### Path B — Manual UI (always works)

1. Click **"New +"** → **"Web Service"**.
2. Pick the `expense-tracker-bot` repo from the list.
3. Fill the form:

   | Field | Value |
   |---|---|
   | Name | `expense-bot` |
   | Project | (leave blank or pick one) |
   | Language | **Docker** (NOT Python) |
   | Branch | `feat/nocodb-edition` |
   | Region | `Oregon` (US) |
   | Root Directory | (leave blank — Dockerfile is at repo root) |
   | Dockerfile Path | `./Dockerfile` |
   | Docker Build Context Directory | `.` |
   | Instance Type | **Free** |

4. Scroll down to **"Health Check Path"** (might be under
   "Advanced") → set to `/health`.
5. Leave everything else default.
6. Click **"Create Web Service"** at the bottom.
   (Don't worry about the "deploy will fail without env vars"
   warning — we add them in Step 3 before the next deploy.)

---

## Step 3 · Add secrets (5 min)

1. You'll land on your service's page at
   `https://dashboard.render.com/web/srv-xxxxx`.
2. Left sidebar → **"Environment"**.
3. Under **"Environment Variables"** click **"Add Environment
   Variable"** for each row in `secrets-checklist.md`.
4. For each one, mark it as **secret** (encrypted) by clicking
   the pencil/eye icon next to the value field.

> **Tip for `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT`:** This is the
> only awkward one — paste the *entire* JSON blob (300+ chars,
> includes newlines).  Render's textarea handles it fine; just
> make sure you didn't accidentally drop the leading `{` or
> trailing `}` when copying.  Validate locally with:
>
> ```bash
> python -c "import json,sys; json.loads(sys.argv[1]); print('OK')" "$(pbpaste)"
> ```

5. Click **"Save Changes"** at the top.  Render auto-triggers a
   redeploy with the new env vars.

---

## Step 4 · Watch the first build (3 min)

1. Left sidebar → **"Logs"** (or **"Events"** for high-level).
2. The build progresses through:

   ```
   ==> Cloning from https://github.com/...
   ==> Checking out commit ... in branch feat/nocodb-edition
   ==> Building Docker image
   Step 1/12 : FROM python:3.11-slim AS builder
   ...
   Successfully built ...
   ==> Pushing image to registry...
   ==> Deploying...
   ==> Running 'expense --telegram'
   ```

3. Once **deploy succeeds**, container logs start streaming.
   You should see:

   ```
   Provider       : groq
   Timezone       : America/Chicago
   Currency       : USD
   Backend        : gspread
   Allowed users  : [<your_id>]

   Starting long-polling. Press Ctrl-C to stop.

   XX:XX:XX INFO  ...health_server | Health endpoint listening on http://0.0.0.0:10000/...
   XX:XX:XX INFO  ...factory       | PTB bootstrap: awaiting Application.initialize() ...
   XX:XX:XX INFO  ...factory       | PTB bootstrap: initialize() complete — calling start() ...
   XX:XX:XX INFO  ...factory       | PTB bootstrap: start() complete — beginning updater long-poll ...
   XX:XX:XX INFO  ...factory       | PTB bootstrap: updater is polling Telegram — bot is now LIVE.
   ```

4. **The last line is the gold standard.**  When you see
   `bot is now LIVE`, the bot is genuinely polling Telegram.

> **Note on the port number:** Render assigns each service a
> random port (usually 10000); our Dockerfile picks it up via
> `$PORT`.  Don't be surprised that it's not 7860 like on HF.

---

## Step 5 · Smoke test (1 min)

1. Open Telegram on your phone.
2. Find `@<your_bot_username>` (the username you registered
   with BotFather).
3. Send: `/start`
4. Expect a greeting reply within ~2 seconds.
5. Send: `today I spent 40 bucks for food`
6. Expect a confirmation like
   `Logged $40.00 to Food (May 2026)` within ~5 seconds.
7. Open your Google Sheet on your phone — verify the row
   appears in `Transactions` and the `May 2026` tab.

If all four steps pass, **you have a 24/7 chat-driven expense
logger running for $0/month.**

---

## Step 6 · Wire up keep-alive (2 min)

Render free tier sleeps after **15 minutes of no inbound HTTP
traffic**.  Our GitHub Actions cron pings the bot's `/health`
endpoint every 14 minutes to keep it awake.

1. Copy your Render service URL from the dashboard
   (top of the service page, looks like
   `https://expense-bot.onrender.com`).
2. On GitHub: https://github.com/<your-username>/expense-tracker-bot/settings/variables/actions
3. Click **"New repository variable"** (NOT secret — it's just a
   URL, nothing private).
4. Name: `RENDER_SERVICE_URL`
5. Value: `https://expense-bot.onrender.com` (no trailing slash)
6. Click **"Add variable"**.
7. (Optional) If you want a Telegram DM when the keep-alive
   ever fails, also add two **secrets**:
   * `KEEPALIVE_TG_BOT_TOKEN` = your bot token (same as in
     Render Settings).
   * `KEEPALIVE_TG_CHAT_ID` = your Telegram user ID.

The cron is already in `.github/workflows/keep-render-alive.yml`
and runs every 14 minutes.  First run starts on the next
:00, :14, :28, :42, or :56 of the hour.

---

## Step 7 · Tear down the Hugging Face Space (1 min, optional but tidy)

Now that Render is the live host, kill the dead HF Space so it's
not confusing anyone:

1. Open https://huggingface.co/spaces/<your-username>/expense-bot/settings.
2. Scroll to the bottom → **"Delete this Space"**.
3. Type the Space name to confirm.

Your code is still on GitHub — only the dead Space is gone.

---

## Troubleshooting

### "Deploy live" but bot doesn't reply

* Check the **Logs** tab.  If you see the four `PTB bootstrap`
  lines ending in `bot is now LIVE`, the bot IS polling — verify
  your `TELEGRAM_ALLOWED_USERS` matches your Telegram user ID
  (the bot silently refuses anyone else).
* If you don't see `bot is now LIVE`, paste the last 30 lines
  of logs into the chat.

### "Connection reset by peer" every few hours

Known polling quirk on free PaaS hosts.  PTB auto-reconnects in
a few seconds; you may miss messages sent during that window.
For a personal expense logger this is acceptable.  If it bothers
you, switch to webhook mode (see future `docs/HANDBOOK.md`
chapter — webhook support is a half-day refactor).

### "750 instance hours exceeded"

You hit Render's free-tier monthly cap.  The bot pauses until
the 1st of next month.  Workarounds:

* Pay $7/month for the Starter plan (no hour cap).
* Or migrate to Koyeb (1-hour idle sleep, 100GB outbound — also
  free, see `deploy/koyeb-edition/` if we ever build it).

### Build fails with "no space left on device"

Render's free tier disk is small but our image fits.  If you hit
this, click **"Clear build cache & deploy"** in the dashboard.

### Render says service is "Suspended"

Means you exceeded the free tier or violated ToS.  Click
**"Resume"** at the top of the service page.  If it keeps
suspending, check **Settings → Suspended Reason**.

---

## What this gives you

* **24/7 Telegram bot** at `https://expense-bot.onrender.com`
* **Same UX as Hugging Face** — DM your bot, expenses log to
  Sheets, summaries on demand
* **$0/month** as long as you stay within 750 hours/month and
  100GB egress (which the bot uses ~10MB of)
* **Auto-redeploy on every git push** — no manual deploys
* **Health-check liveness** — Render restarts the container if
  the health endpoint stops responding

What this does NOT give you (and you don't need yet):

* Persistent disk (logs reset on every deploy — they're already
  mirrored to Sheets so this is fine)
* Custom domain (the `.onrender.com` URL is permanent and free)
* SSL (Render does HTTPS for you, no cert setup)
