# HF Spaces · secrets checklist

Paste each of these into your Space's **Settings → Variables and
secrets → "New secret"** form.  Use **Secret** (encrypted, hidden in
build logs), not **Variable** (visible).

| # | Name | Required? | Example value | Notes |
|---|------|-----------|---------------|-------|
| 1 | `TELEGRAM_BOT_TOKEN` | ✅ | `8702975628:AAF...` | From `@BotFather`. Treat as a password. |
| 2 | `TELEGRAM_ALLOWED_USERS` | ✅ | `8684705854` | Your Telegram user ID. Comma-separate if multiple. |
| 3 | `GROQ_API_KEY` | ✅ | `gsk_...` | https://console.groq.com/keys |
| 4 | `EXPENSE_SHEET_ID` | ✅ | `1AbC...` | Long token in your Sheet URL. |
| 5 | `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT` | ✅ | `{"type":"service_account",...}` | **Full JSON blob**, not a path. |
| 6 | `LLM_PROVIDER` | ✅ | `groq` | Always `groq` on HF — no Ollama on free CPU. |
| 7 | `TIMEZONE` | recommended | `America/Chicago` | IANA TZ name; controls "today" / "yesterday". |
| 8 | `DEFAULT_CURRENCY` | recommended | `USD` | Assumed when message has no currency marker. |

**Do NOT set:**

* `TELEGRAM_HEALTH_PORT` — the Dockerfile sets it from `$PORT` already.
* `GOOGLE_SERVICE_ACCOUNT_JSON` (the file-path variant) — would be
  ignored anyway because `_CONTENT` wins, but cleaner to leave it out.

After saving secrets, click **"Restart this Space"** at the top of
Settings.  The container picks up the new env vars on next boot.

---

## Optional: enable mirror mode (Sheets + Postgres dual-write)

If you've also set up Supabase (or any reachable Postgres) and want
every expense to land in BOTH Sheets and Postgres for future
analytics / NocoDB UI:

| # | Name | Required for mirror? | Example value | Notes |
|---|------|---------------------|---------------|-------|
| 9 | `STORAGE_BACKEND` | ✅ | `mirror` | Replaces the default `sheets` |
| 10 | `MIRROR_PRIMARY` | optional | `sheets` | Default. Source-of-truth for reads. |
| 11 | `MIRROR_SECONDARY` | optional | `nocodb` | Default. Best-effort mirror. |
| 12 | `DATABASE_URL` | ✅ | `postgresql+psycopg://user:pass@db.example.supabase.co:6543/postgres` | Use Supabase's **Session pooler** URL for IPv4 compat |

**Behaviour:**

* Every chat write goes to **Sheets first** (must succeed → user sees
  the confirmation reply).
* Then to **Postgres** as a best-effort mirror (failures logged at
  WARNING but never break the user's chat).
* Reads + retrieval queries + summaries always come from Sheets so
  the phone view is unchanged.
* If Postgres ever falls behind during a Supabase outage, run
  `expense --reconcile` from your laptop to back-fill missing rows.
  Idempotent.

**Before turning mirror mode on**, make sure:

1. Your Supabase database has the schema initialised — run
   `expense --init-postgres` once from your laptop with `DATABASE_URL`
   in your local `.env`.
2. Your `DATABASE_URL` points at the **Session pooler** on port 6543
   (not direct connection on 5432) — the pooler URL is the only one
   Hugging Face Spaces can reach over IPv4.

**Switching modes is reversible.**  Set `STORAGE_BACKEND=sheets` and
restart to drop Postgres mirroring; the Sheets write path is
unchanged so no data is lost.
