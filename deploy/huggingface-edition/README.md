# Hugging Face Spaces edition

The free, 24/7-friendly hosting target for the **Sheets** edition of
the bot.

## Why Hugging Face Spaces?

For a personal Telegram bot that talks to Google Sheets, Hugging Face
Spaces beats every other free tier in 2026:

| Platform | Sleep policy | RAM | Setup time | Credit card? |
|----|----|----|----|----|
| **Hugging Face Spaces** | 48 h idle (we ping every 36 h) | **16 GB** | 5 min | **No** |
| Render Free | 15 min idle | 512 MB | 5 min | No |
| Koyeb Free | 1 h idle | 512 MB | 10 min | Yes |
| Fly.io Free | None | 256 MB × 3 VMs | 30 min | **Yes** |
| Oracle Free | None (when available) | 1 GB | 1–2 h | **Yes** + capacity issues |

The 48-hour idle window is trivially defeated with a GitHub Actions
cron job (one file, lives at `.github/workflows/keep-hf-alive.yml`)
that hits `https://<your-space>.hf.space/health` every 36 hours.

## What this folder contains

| File | Purpose |
|----|----|
| `DEPLOY.md` | Step-by-step click-by-click runbook (start here) |
| `README_TEMPLATE.md` | The README that goes INSIDE the Hugging Face Space repo (HF parses YAML front-matter from this file to know it's a Docker Space) |
| `secrets-checklist.md` | The exact list of secrets to paste into the Space's "Settings → Secrets" tab |
| `.env.example` | Example .env for local parity testing before pushing |

## Architecture (60-second version)

```
                 ┌─────────────────────────────────────┐
GitHub Actions ──┤  HTTPS GET /health  every 36 hours  │
   (cron job)    │  → keeps the Space "active"         │
                 └──────────────┬──────────────────────┘
                                ▼
              ┌──────────────────────────────────┐
              │   Hugging Face Space (Docker)    │
              │                                  │
              │   ┌──────────────────────────┐   │
              │   │  expense --telegram      │   │
              │   │   ├─ long-poll Telegram  │   │
              │   │   └─ HTTP :7860 /health  │   │
              │   └──────────────────────────┘   │
              │                                  │
              │   Secrets injected by HF:        │
              │   ├─ TELEGRAM_BOT_TOKEN          │
              │   ├─ GROQ_API_KEY                │
              │   ├─ EXPENSE_SHEET_ID            │
              │   ├─ GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT
              │   └─ TELEGRAM_ALLOWED_USERS     │
              └──────────┬───────────────────────┘
                         │
              ┌──────────┴──────────┬──────────────────┐
              ▼                     ▼                  ▼
      Telegram servers       Groq API          Google Sheets
        (long-poll)        (LLM extraction)    (your ledger)
```

## TL;DR — three commands once setup is done

```bash
# 1. Create the Space + push (one-time, see DEPLOY.md)
git remote add huggingface git@hf.co:spaces/<username>/expense-bot
git push huggingface feat/nocodb-edition:main

# 2. Trigger / verify the keep-alive
gh workflow run keep-hf-alive.yml

# 3. Watch it run
curl -s https://<username>-expense-bot.hf.space/health
# → ok
```

## Limits & caveats (read these BEFORE deploying)

1. **Public source, private secrets.** Free Spaces have public source
   code.  Your `.env`, service-account JSON, and tokens stay private
   because they live in HF's encrypted Secrets store, not in git.
   The bot's source code itself is already on your public GitHub —
   nothing new is exposed.
2. **48-hour true idle = sleep.** A keep-alive cron is mandatory.
   The provided GitHub Actions workflow handles it for free.
3. **Free tier is CPU-only** (no GPU).  Fine — we use Groq for the
   LLM, not local inference.
4. **HF can rebuild your image any time.**  The Dockerfile must be
   reproducible from a clean clone.  This one is.
5. **The Space's URL is public.**  Only your `/health` endpoint
   responds; the Telegram bot itself talks outbound to Telegram via
   long-polling, never accepts inbound from random visitors.
