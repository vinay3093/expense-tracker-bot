# Render edition — 24/7 Telegram bot, $0/month

This folder contains everything you need to deploy the expense
tracker on **Render Free** as a long-running Telegram bot.

## Why Render

We originally targeted **Hugging Face Spaces** for free 24/7
hosting and got the build green there — but ran into a hard wall:
**HF blocks all outbound traffic to `api.telegram.org`** as a
security policy.  No timeout knob or library swap fixes that.
Render places no such restriction; thousands of Telegram bots run
on its free tier today.

## What's in here

| File | Purpose |
|---|---|
| `render.yaml` | One-click "Blueprint" spec.  Connect your GitHub repo to Render, point at this file, and the service is created with the right runtime + healthcheck + env-var stubs. |
| `DEPLOY.md` | Click-by-click runbook (15 min from zero).  Use this if Blueprints don't work — Path B is a manual UI walkthrough that always works. |
| `secrets-checklist.md` | The 8 env vars you must set in Render's dashboard, with example values and "do NOT set" warnings. |
| `README.md` | This file. |

## Architecture

```
Phone (Telegram app)
       │
       │ DM
       ▼
Telegram servers ◀────── long-poll ─────── Render container (Free)
                                              │
                                              ├── /health  ←──── GitHub Actions cron
                                              │                   (every 14 min,
                                              │                    keeps Render awake)
                                              │
                                              ├── api.groq.com (LLM)
                                              ├── sheets.googleapis.com (writes)
                                              └── frankfurter.app (FX rates)
```

Same Dockerfile as the HF, NocoDB, and self-hosted (Oracle)
editions.  The only thing that changes between hosts is the
deploy bundle — `deploy/render-edition/` here, `deploy/sheets-
edition/` for self-hosted on a VM, `deploy/nocodb-edition/` for
docker-compose on a VM.

## Comparison with other 24/7 free hosts (verified May 2026)

| | Render Free | Hugging Face Spaces | Koyeb Free | Self-host (laptop / Pi) |
|---|---|---|---|---|
| Free? | yes (no CC) | yes (no CC) | yes (no CC) | yes |
| Telegram allowed? | **yes** | **NO — blocked** | yes | yes |
| Sleep policy | 15 min idle | 48 h idle | 1 h idle | n/a |
| Hours cap | 750 / month | unlimited | unlimited | n/a |
| Public URL | `*.onrender.com` | `*.hf.space` | `*.koyeb.app` | none (or DDNS) |
| Setup effort | 15 min | (broken for us) | 15 min | 30 min one-time |
| Auto-redeploy on push | yes | yes | yes | manual `git pull` |
| Verdict | **recommended** | dead end | backup option | best for "always have it" |

## TL;DR

1. Push the repo to GitHub (already done).
2. Create a Render account at https://render.com (no card).
3. **New + → Web Service → pick your repo → Docker → Free**.
4. Paste 8 env vars from `secrets-checklist.md`.
5. Watch the build (~3 min) — look for `bot is now LIVE` in logs.
6. DM the bot from Telegram on your phone.
7. Add `RENDER_SERVICE_URL` repo variable on GitHub so the
   keep-alive cron in `.github/workflows/keep-render-alive.yml`
   knows where to ping.

Total: **15 minutes**.  Full walkthrough: `DEPLOY.md`.
