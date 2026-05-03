#!/usr/bin/env bash
# deploy/nocodb-edition/update.sh
#
# Pull latest code, run any new Alembic migrations, and restart the
# bot.  Run this on the VM whenever you push new code from your
# laptop.
#
# Idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPLOY_DIR="${REPO_ROOT}/deploy/nocodb-edition"
ENV_FILE="${DEPLOY_DIR}/.env-deploy"

cd "${REPO_ROOT}"

echo "==> 1/4  git pull"
git pull --ff-only

echo "==> 2/4  pip install -e .[telegram,nocodb]"
# shellcheck disable=SC1091
source "${REPO_ROOT}/.venv/bin/activate"
pip install --upgrade pip
pip install -e ".[telegram,nocodb]"

echo "==> 3/4  Alembic migrate"
# shellcheck disable=SC1091
source "${ENV_FILE}"
export DATABASE_URL="postgresql+psycopg://${POSTGRES_USER:-expense}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB:-expense}"
export STORAGE_BACKEND=nocodb
alembic -c "${REPO_ROOT}/alembic.ini" upgrade head

echo "==> 4/4  systemctl restart expense-bot"
sudo systemctl restart expense-bot
echo
sudo systemctl status expense-bot --no-pager
echo
echo "Tail logs with: sudo journalctl -u expense-bot -f"
