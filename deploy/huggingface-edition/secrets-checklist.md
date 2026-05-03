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
* `STORAGE_BACKEND` — defaults to `sheets` which is what you want here.
* `DATABASE_URL` — Postgres edition only.
* `GOOGLE_SERVICE_ACCOUNT_JSON` (the file-path variant) — would be
  ignored anyway because `_CONTENT` wins, but cleaner to leave it out.

After saving secrets, click **"Restart this Space"** at the top of
Settings.  The container picks up the new env vars on next boot.
