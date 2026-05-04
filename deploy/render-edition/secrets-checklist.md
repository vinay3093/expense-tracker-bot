# Render · secrets checklist

Paste each of these into your service's **Environment** tab in the
Render dashboard.  Click the eye/pencil icon next to each value to
mark it as **encrypted** (Render's term for secret) — Render hides
encrypted values in build logs and the UI.

| # | Name | Required? | Example value | Notes |
|---|------|-----------|---------------|-------|
| 1 | `TELEGRAM_BOT_TOKEN` | yes | `8702975628:AAF...` | From `@BotFather`. Treat as a password. |
| 2 | `TELEGRAM_ALLOWED_USERS` | yes | `8684705854` | Your Telegram user ID. Comma-separate if multiple. |
| 3 | `GROQ_API_KEY` | yes | `gsk_...` | https://console.groq.com/keys |
| 4 | `EXPENSE_SHEET_ID` | yes | `1AbC...` | Long token in your Sheet URL. |
| 5 | `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT` | yes | `{"type":"service_account",...}` | **Full JSON blob**, not a path. ~300 chars. |
| 6 | `LLM_PROVIDER` | yes | `groq` | Always `groq` on Render — no GPU for Ollama. |
| 7 | `TIMEZONE` | recommended | `America/Chicago` | IANA TZ; controls "today" / "yesterday". |
| 8 | `DEFAULT_CURRENCY` | recommended | `USD` | Used when message has no currency marker. |

**Do NOT set:**

* `PORT` — Render injects this automatically; setting it manually
  gets overridden silently and breaks the health endpoint.
* `TELEGRAM_HEALTH_PORT` — the Dockerfile reads `$PORT` and passes
  it through, so the health server always listens where Render
  expects it.
* `GOOGLE_SERVICE_ACCOUNT_JSON` (the file-path variant) — would be
  ignored anyway because `_CONTENT` wins.

After saving secrets, Render auto-triggers a redeploy.  Watch
the **Logs** tab for the four `PTB bootstrap` lines; the last
one (`bot is now LIVE`) is the green light.

---

## Optional: enable mirror mode (Sheets + Postgres dual-write)

If you've also set up Supabase (or any reachable Postgres) and want
every expense to land in BOTH Sheets and Postgres for future
analytics / NocoDB UI:

| # | Name | Required for mirror? | Example value | Notes |
|---|------|---------------------|---------------|-------|
| 9 | `STORAGE_BACKEND` | yes | `mirror` | Replaces the default `sheets` |
| 10 | `MIRROR_PRIMARY` | optional | `sheets` | Default. Source-of-truth for reads. |
| 11 | `MIRROR_SECONDARY` | optional | `nocodb` | Default. Best-effort mirror. |
| 12 | `DATABASE_URL` | yes | `postgresql+psycopg://user:pass@db.example.supabase.co:6543/postgres` | Use Supabase's **Session pooler** URL for IPv4 |

**Behaviour:**

* Every chat write goes to **Sheets first** (must succeed → user
  sees the confirmation reply).
* Then to **Postgres** as a best-effort mirror (failures logged at
  WARNING but never break the user's chat).
* Reads + retrieval queries + summaries always come from Sheets so
  the phone view is unchanged.
* If Postgres ever falls behind during a Supabase outage, run
  `expense --reconcile` from your laptop to back-fill missing rows.
  Idempotent.

**Before turning mirror mode on**, make sure:

1. Your Supabase database has the schema initialised — run
   `expense --init-postgres` once from your laptop with
   `DATABASE_URL` in your local `.env`.
2. Your `DATABASE_URL` points at the **Session pooler** on port
   6543 (not direct connection on 5432) — most free PaaS hosts
   only have IPv4 outbound and Supabase's direct port is IPv6-only.

**Switching modes is reversible.**  Set `STORAGE_BACKEND=sheets`
and redeploy to drop Postgres mirroring; the Sheets write path is
unchanged so no data is lost.
