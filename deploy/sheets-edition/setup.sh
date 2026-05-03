#!/usr/bin/env bash
# =============================================================================
# setup.sh — first-time bootstrap on a fresh Oracle Cloud Ubuntu VM
# Tested on Ubuntu 22.04 LTS (ships python 3.10) and 24.04 LTS (ships python 3.12).
# =============================================================================
# Run this AS the `ubuntu` user (the default sudo-capable user on Oracle's
# Ubuntu image).  Idempotent — re-runs are safe.
#
#   ssh ubuntu@<vm-public-ip>
#   git clone https://github.com/<you>/expense-tracker-bot.git
#   cd expense-tracker-bot
#   bash deploy/sheets-edition/setup.sh
#
# What it does:
#   1. Installs OS packages: python3 (whatever the OS ships), venv, git, build tools.
#   2. Creates ./.venv/ and installs the project + telegram extras.
#   3. Creates ./logs and ./secrets with safe permissions.
#   4. Symlinks deploy/sheets-edition/expense-bot.service into /etc/systemd/system/.
#
# What it does NOT do (you do these by hand, see DEPLOY.md):
#   - Write your `.env`         (contains secrets — never put it in git).
#   - Drop service-account JSON (same reason).
#   - Start the service        (you start it AFTER secrets are in place).
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
LOGS_DIR="${REPO_ROOT}/logs"
SECRETS_DIR="${REPO_ROOT}/secrets"
SERVICE_SRC="${REPO_ROOT}/deploy/sheets-edition/expense-bot.service"
SERVICE_DST="/etc/systemd/system/expense-bot.service"

log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok  \033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn \033[0m %s\n' "$*" >&2; }

# -----------------------------------------------------------------------------
# 0. Sanity checks
# -----------------------------------------------------------------------------
if [[ "${EUID}" -eq 0 ]]; then
    warn "Run as the 'ubuntu' user, not root.  Aborting."
    exit 1
fi
if [[ ! -f "${REPO_ROOT}/pyproject.toml" ]]; then
    warn "Could not find pyproject.toml at ${REPO_ROOT}.  Run from a clone of the repo."
    exit 1
fi

# -----------------------------------------------------------------------------
# 1. OS packages
# -----------------------------------------------------------------------------
log "Installing OS packages (python, venv, git, build tools)..."
sudo apt-get update -y
sudo apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    git \
    build-essential \
    ca-certificates
ok "OS packages installed"

# -----------------------------------------------------------------------------
# 2. Virtualenv + project install
# -----------------------------------------------------------------------------
if [[ ! -d "${VENV_DIR}" ]]; then
    log "Creating virtualenv at ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
    ok "Virtualenv created"
else
    ok "Virtualenv already exists — reusing"
fi

log "Upgrading pip + installing the project (with telegram extras)..."
"${VENV_DIR}/bin/pip" install --upgrade pip wheel >/dev/null
"${VENV_DIR}/bin/pip" install -e "${REPO_ROOT}[telegram]"
ok "Project installed into venv"

# -----------------------------------------------------------------------------
# 3. Runtime directories
# -----------------------------------------------------------------------------
mkdir -p "${LOGS_DIR}" "${SECRETS_DIR}"
chmod 700 "${SECRETS_DIR}"
chmod 755 "${LOGS_DIR}"
ok "logs/ and secrets/ ready (secrets/ is 0700)"

# -----------------------------------------------------------------------------
# 4. systemd unit
# -----------------------------------------------------------------------------
if [[ ! -f "${SERVICE_SRC}" ]]; then
    warn "Cannot find ${SERVICE_SRC} — bailing on systemd step."
    exit 1
fi

log "Installing systemd unit -> ${SERVICE_DST}..."
sudo install -m 0644 "${SERVICE_SRC}" "${SERVICE_DST}"
sudo systemctl daemon-reload
ok "systemd unit installed (not started — start it after secrets are in place)"

# -----------------------------------------------------------------------------
# 5. Done
# -----------------------------------------------------------------------------
cat <<EOF

=============================================================================
 Bootstrap complete.

 Next steps (do these from your laptop):

   1. scp your .env to the VM:
        scp .env ubuntu@<vm-ip>:${REPO_ROOT}/.env

   2. scp the Google service-account JSON to the VM:
        scp secrets/service-account.json \\
            ubuntu@<vm-ip>:${SECRETS_DIR}/service-account.json

   3. Lock down their permissions (back on the VM):
        chmod 600 ${REPO_ROOT}/.env
        chmod 600 ${SECRETS_DIR}/service-account.json

   4. Smoke-test before starting the service:
        cd ${REPO_ROOT}
        ${VENV_DIR}/bin/expense --whoami       # should print sheet title
        ${VENV_DIR}/bin/expense --ping-llm     # should print Groq reply

   5. Start the bot under systemd:
        sudo systemctl enable --now expense-bot
        sudo journalctl -u expense-bot -f      # tail logs (Ctrl-C to detach)

 See deploy/sheets-edition/DEPLOY.md for the full runbook.
=============================================================================
EOF
