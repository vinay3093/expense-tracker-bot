# Deploying the Expense Tracker Bot to Oracle Cloud Free Tier

> **Goal:** keep `expense --telegram` running 24/7 so you can chat the
> bot from your phone even when your laptop is closed.
>
> **Cost:** $0 forever, on Oracle's *Always-Free* tier (a credit card is
> required at signup for ID verification — it is never charged on
> Always-Free).
>
> **Effort:** ~30–45 minutes start to finish, the first time.

---

## 0. What you'll end up with

```
                 ┌──────────────────────────────────────────────┐
                 │   Oracle Cloud Free Tier ARM VM (ubuntu)     │
                 │                                              │
   Telegram  ◀──▶│   systemd: expense-bot.service               │
                 │      └─ /home/ubuntu/expense-tracker-bot/    │
                 │            ├─ .venv/   (python 3.10 + deps)  │
                 │            ├─ .env     (chmod 600)           │
                 │            ├─ secrets/ (service-account.json)│
                 │            └─ logs/    (jsonl traces)        │
                 └──────────────────────────────────────────────┘
                              │
                              ▼
            api.telegram.org · api.groq.com · sheets.googleapis.com
```

- **Inbound network:** none.  The bot uses Telegram long-polling, so it
  pulls updates instead of receiving webhooks — no public port, no
  domain, no TLS to manage.
- **Outbound network:** HTTPS to Telegram, Groq, Google Sheets,
  frankfurter.app.  Oracle's default VCN security list allows this.
- **Auto-restart:** systemd brings the bot back on crash and on reboot.

---

## 1. Sign up for Oracle Cloud Free Tier

1. Go to <https://www.oracle.com/cloud/free/>.
2. Click **Start for free**.
3. Pick a **home region** in the country you live in (you cannot change
   it later — Always-Free resources only exist in the home region).
4. Provide a credit card for identity verification.  **You are NOT
   charged on Always-Free resources** — Oracle just won't let bots sign
   up without a card.  If you want guaranteed safety, you can later
   downgrade the account to "Always Free" only via the console.
5. Wait for the activation email (~5 minutes).

> **Heads-up:** Oracle has been known to be strict during signup —
> some cards fail.  If yours does, try a different card or use a
> different browser.  Don't worry about charges; once your account is
> activated you will see "Always Free" badges next to free resources.

---

## 2. Create the VM

In the Oracle Cloud console:

1. **Menu (☰) → Compute → Instances → Create Instance**.
2. Name it something memorable: `expense-bot`.
3. **Image:** click *Change image* → **Ubuntu** → **22.04 (aarch64)**.
4. **Shape:** click *Change shape* → **Ampere** (this is the big ARM
   shape that's part of Always-Free).  Default 1 OCPU + 6 GB RAM is
   plenty for the bot.  You can go up to 4 OCPU / 24 GB still free.
5. **Networking:** keep defaults (auto-creates a VCN and public subnet
   with a public IPv4).
6. **SSH keys:** click *Generate a key pair for me* → **save BOTH
   files** (the private key `.key` and the public key `.pub`).  You
   will need the private key to SSH in.
7. Click **Create**.  Wait ~60 seconds for the VM to show *Running*.
8. **Copy the VM's public IPv4 address** — you'll see it on the
   instance page.  Call it `<vm-ip>` from here on.

> **If Ampere ("Out of capacity")**, switch shape to **VM.Standard.E2.1.Micro**
> (AMD x86, also free, 1/8 OCPU + 1 GB RAM).  The bot fits there too.

---

## 3. SSH in for the first time

From your laptop:

```bash
chmod 600 ~/Downloads/ssh-key-<date>.key      # private key from step 2.6
ssh -i ~/Downloads/ssh-key-<date>.key ubuntu@<vm-ip>
```

You should land in `ubuntu@expense-bot:~$`.

> **Optional but recommended** — add the key to your SSH config so you
> never type `-i ...` again:
>
> ```
> # ~/.ssh/config
> Host expense-bot
>     HostName <vm-ip>
>     User ubuntu
>     IdentityFile ~/Downloads/ssh-key-<date>.key
> ```
>
> After that, `ssh expense-bot` is enough.

---

## 4. Clone the repo + run the bootstrap

Still on the VM:

```bash
git clone https://github.com/<your-username>/expense-tracker-bot.git
cd expense-tracker-bot
bash deploy/oracle/setup.sh
```

The script:

- Installs `python3`, `python3-venv`, `git`, build tools.
- Creates `.venv/` and runs `pip install -e ".[telegram]"`.
- Creates `logs/` and a 0700 `secrets/`.
- Symlinks `expense-bot.service` into `/etc/systemd/system/`.
- Does **not** start the service — secrets aren't there yet.

If anything fails on Ampere because of an ARM wheel issue, swap to
the AMD shape (see Step 2 note); all our deps are pure Python today,
but new ones could change that.

---

## 5. Copy your secrets to the VM

From your **laptop**, in the local repo root:

```bash
# 1. Your .env (TELEGRAM_BOT_TOKEN, allow-list, sheet ID, Groq key, etc.)
scp .env expense-bot:~/expense-tracker-bot/.env

# 2. The Google service-account JSON
scp secrets/service-account.json \
    expense-bot:~/expense-tracker-bot/secrets/service-account.json
```

Back on the **VM**, lock them down:

```bash
chmod 600 ~/expense-tracker-bot/.env
chmod 600 ~/expense-tracker-bot/secrets/service-account.json
```

> **Why two separate files instead of stuffing the JSON into `.env`?**
> The codebase reads `GOOGLE_SERVICE_ACCOUNT_JSON=./secrets/service-account.json`
> from `.env`.  Keep them separate — the JSON path matches the laptop
> setup so you don't have to special-case the deploy.

### What `.env` should contain (copy from your laptop's working `.env`)

```bash
# LLM
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
GROQ_MODEL=llama-3.1-8b-instant

# Locale
TIMEZONE=America/Chicago        # match the timezone you actually live in
DEFAULT_CURRENCY=USD

# Logs / tracing
LLM_TRACE=true
LOG_DIR=./logs

# Sheets
GOOGLE_SERVICE_ACCOUNT_JSON=./secrets/service-account.json
EXPENSE_SHEET_ID=1abc...XYZ

# Telegram
TELEGRAM_BOT_TOKEN=8702975628:AAF...
TELEGRAM_ALLOWED_USERS=8684705854
```

---

## 6. Smoke-test before going live

On the **VM**, run these in order.  Each one fails loudly if a secret
is wrong, so debug here, NOT in journalctl after the service is up.

```bash
cd ~/expense-tracker-bot

# 1. Sheets credentials work?
.venv/bin/expense --whoami
# → "Connected to: <your sheet title>"

# 2. LLM credentials work?
.venv/bin/expense --ping-llm
# → a one-line LLM reply, latency, token counts

# 3. End-to-end chat against the real sheet?
.venv/bin/expense --chat "test ping from Oracle VM"
# (this DOES write a row.  /undo to remove it after.)

# 4. Telegram bot starts and accepts messages?
.venv/bin/expense --telegram
# Open Telegram on your phone, DM the bot, send "spent 1 on test".
# Confirm the reply, then Ctrl-C in the SSH session to stop the bot.
# /undo from Telegram to remove the test row.
```

Only proceed once all four pass.

---

## 7. Start the bot under systemd

```bash
sudo systemctl enable --now expense-bot
sudo journalctl -u expense-bot -f
```

You should see something like:

```
expense-bot[12345]: Starting Telegram long-polling...
expense-bot[12345]: Authorized users: {8684705854}
expense-bot[12345]: Telegram polling started — Ctrl-C to stop.
```

Press **Ctrl-C** in the journal tail (it just detaches from the log;
the service keeps running).

DM the bot from your phone — it should reply within ~1 second.

---

## 8. Operating it

| What | Command (on the VM) |
|---|---|
| Tail live logs | `sudo journalctl -u expense-bot -f` |
| Last 200 lines | `sudo journalctl -u expense-bot -n 200 --no-pager` |
| Service status | `sudo systemctl status expense-bot` |
| Stop the bot | `sudo systemctl stop expense-bot` |
| Start it | `sudo systemctl start expense-bot` |
| Restart it | `sudo systemctl restart expense-bot` |
| **Deploy new code** | `cd ~/expense-tracker-bot && bash deploy/oracle/update.sh` |
| Inspect the JSONL traces | `tail -f ~/expense-tracker-bot/logs/conversations.jsonl` |
| Run a one-off `--summary` | `cd ~/expense-tracker-bot && .venv/bin/expense --summary week` |

The `update.sh` helper does `git pull` → `pip install -e .[telegram]` →
`systemctl restart expense-bot` and prints the new status block.

### Reboot behaviour

`systemctl enable` (which `setup.sh` and `update.sh` already ran) means
the bot is **on by default** after every reboot.  Test it once:

```bash
sudo reboot
# wait 60 seconds, ssh back in
sudo systemctl status expense-bot
# → active (running)
```

---

## 9. Things to be aware of

### Always-Free reclamation policy

Oracle reclaims an Always-Free instance if it sits at **<20 % CPU AND
<20 % network for 7 days continuously**.  A long-polling Telegram bot
sends a request every ~30s, so network usage stays well above the
threshold.  You're fine — this is not the same product as Google
Cloud's spot instances.

### Updating Python or the OS

```bash
sudo apt update && sudo apt upgrade -y
sudo systemctl restart expense-bot   # in case site-packages relocated
```

### Backing up

The Google Sheet **is** the backup — that's the whole point of the
data living there.  The VM is stateless except for:

- `.env`
- `secrets/service-account.json`
- `logs/*.jsonl` (tracing — useful for debugging, not strictly needed)

Keep copies of `.env` and the service-account JSON in a password
manager.  If the VM dies, you re-run Steps 1–7 on a new one in <30
minutes.

### Rotating the Telegram token

If the token leaks:

1. Telegram → DM `@BotFather` → `/revoke` → pick the bot.
2. He gives you a new token.
3. Edit `~/expense-tracker-bot/.env` on the VM (and your laptop's copy).
4. `sudo systemctl restart expense-bot`.

### Rotating the Google service-account key

1. Cloud Console → IAM & Admin → Service Accounts → your bot's
   account → **Keys** → *Add key* → JSON.
2. Download the new JSON, replace it on the VM, `chmod 600`.
3. `sudo systemctl restart expense-bot`.
4. In the same panel, **delete the old key** so the leaked one is dead.

### Multiple bots on one VM

Copy the unit file to `expense-bot-<name>.service`, change
`WorkingDirectory`, and run `setup.sh` from a second clone.  systemd
handles them independently.

---

## 10. Tearing it down (just in case)

```bash
sudo systemctl disable --now expense-bot
sudo rm /etc/systemd/system/expense-bot.service
sudo systemctl daemon-reload
rm -rf ~/expense-tracker-bot
```

To kill the VM entirely, terminate it from the Oracle console — that
returns the resources to the Always-Free pool.

---

## Appendix A — Files in this folder

| File | What it is |
|---|---|
| `DEPLOY.md` | This runbook. |
| `setup.sh` | One-shot bootstrap (run on the VM, once). |
| `update.sh` | Pull-latest-and-restart helper (run on the VM, every deploy). |
| `expense-bot.service` | systemd unit; symlinked into `/etc/systemd/system/`. |

## Appendix B — Common errors and fixes

| Symptom | Cause | Fix |
|---|---|---|
| `journalctl` shows `gspread.exceptions.APIError: PERMISSION_DENIED` | Sheet not shared with the service account | In Sheets → Share → paste the service-account email, give Editor access |
| Service flips between `activating` and `failed` quickly | Bot token is wrong / revoked | Update `.env`, `systemctl restart expense-bot` |
| Bot replies nothing on Telegram | Your user ID isn't in `TELEGRAM_ALLOWED_USERS` | DM the bot, send `/whoami`, copy the ID, edit `.env`, restart |
| `pip install ... cryptography failed` | (Rare) Missing build tools on a fresh VM | `sudo apt install -y build-essential libffi-dev libssl-dev` |
| `journalctl` has no output | systemd-journald disk quota hit | `sudo journalctl --vacuum-time=7d` |
| SSH disconnects mid-install | Oracle's NAT timeout on idle session | Re-SSH, re-run `setup.sh` (it's idempotent) |
