#!/usr/bin/env bash
# =============================================================================
# update.sh — pull latest code, reinstall, restart the bot
# =============================================================================
# Run on the VM whenever you want to deploy new code:
#
#   ssh ubuntu@<vm-ip>
#   cd expense-tracker-bot
#   bash deploy/sheets-edition/update.sh
#
# Idempotent.  Safe to re-run.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"

log()  { printf '\033[1;34m[update]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m   ok  \033[0m %s\n' "$*"; }

cd "${REPO_ROOT}"

log "Fetching latest code..."
git fetch --quiet origin
git pull --ff-only origin main
ok "Code up to date at $(git rev-parse --short HEAD)"

log "Reinstalling project (in case deps changed)..."
"${VENV_DIR}/bin/pip" install -e "${REPO_ROOT}[telegram]" --quiet
ok "Dependencies reconciled"

# Reinstall the systemd unit in case it changed in this commit.
SERVICE_SRC="${REPO_ROOT}/deploy/sheets-edition/expense-bot.service"
SERVICE_DST="/etc/systemd/system/expense-bot.service"
if ! cmp -s "${SERVICE_SRC}" "${SERVICE_DST}"; then
    log "Updating systemd unit..."
    sudo install -m 0644 "${SERVICE_SRC}" "${SERVICE_DST}"
    sudo systemctl daemon-reload
    ok "systemd unit refreshed"
fi

log "Restarting expense-bot..."
sudo systemctl restart expense-bot
sleep 2
sudo systemctl --no-pager --lines=10 status expense-bot
ok "Done.  Tail logs with: sudo journalctl -u expense-bot -f"
