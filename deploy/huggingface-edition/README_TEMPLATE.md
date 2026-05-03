---
title: Expense Bot
emoji: "$"
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: "Personal Telegram bot that logs expenses into Google Sheets."
---

# Expense bot — Hugging Face Space

A chat-driven personal expense tracker.  Talks to Telegram, writes
to Google Sheets, runs on Groq for LLM extraction.

> **This is a deployment of [github.com/<your-username>/expense-tracker-bot](https://github.com/<your-username>/expense-tracker-bot).**
> Source code, full architecture, and the laptop / Postgres editions
> live there.  This Space exists only to host the running container.

## How this Space is configured

* **SDK**: Docker (the `Dockerfile` at the repo root builds the image).
* **Hardware**: CPU basic, 2 vCPU, 16 GB RAM (free tier).
* **Port**: `7860` — exposes a `/health` endpoint for the keep-alive
  cron job; the actual Telegram bot uses outbound long-polling and
  doesn't accept inbound HTTP traffic.
* **Secrets**: 5 required, configured under Settings → Secrets.  See
  `deploy/huggingface-edition/secrets-checklist.md` in the source repo.

## Health endpoints

* `GET /` → `200 alive`  (platform probe)
* `GET /health` → `200 ok`  (cron keep-alive)
* anything else → `404`

## Operations

* **Restart**: Settings → "Restart this Space".
* **View logs**: Logs tab.
* **Update code**: `git push huggingface main` from your laptop.
* **Stop accepting messages**: clear `TELEGRAM_ALLOWED_USERS` and
  restart — the bot will refuse every incoming message.
