# `deploy/sheets-edition/` — Oracle Cloud + Google Sheets deploy bundle

Everything needed to run the **Google Sheets edition** of the bot
(`STORAGE_BACKEND=sheets`) 24/7 on a free Oracle ARM VM.

> Want the **NocoDB / Postgres edition** instead?  See
> [`../nocodb-edition/`](../nocodb-edition/).  The two editions
> share 100% of the chat / LLM / Telegram code; only the storage
> destination differs.

| File | What |
|---|---|
| [`DEPLOY.md`](./DEPLOY.md) | **Read this first.** Step-by-step runbook from "I'm signing up for OCI" to "the bot is replying to my phone." |
| `setup.sh` | First-time bootstrap.  Run once on the VM after cloning the repo. |
| `update.sh` | Pull latest code + restart the bot.  Run on the VM whenever you push new code. |
| `expense-bot.service` | systemd unit that keeps the bot alive across crashes and reboots. |

> **Heads-up on secrets:** Nothing in this folder contains real
> credentials.  Your `.env` and `secrets/service-account.json` stay
> out of git and get `scp`'d to the VM separately — see Step 5 of
> `DEPLOY.md`.

---

## TL;DR

```bash
# On the VM, after cloning the repo:
bash deploy/sheets-edition/setup.sh

# From your laptop:
scp .env secrets/service-account.json ubuntu@<vm-ip>:~/expense-tracker-bot/

# Back on the VM:
chmod 600 ~/expense-tracker-bot/.env ~/expense-tracker-bot/secrets/service-account.json
sudo systemctl enable --now expense-bot
sudo journalctl -u expense-bot -f
```

For the long version with explanations of every step (and why), see
[`DEPLOY.md`](./DEPLOY.md).
