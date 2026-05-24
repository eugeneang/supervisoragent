#!/bin/bash
# run_smoke_tests.sh — Run Telegram bot in-process smoke tests.
#
# Called by launchd after every Mac restart and daily at 09:00 SGT.
# Also callable manually:
#
#   bash run_smoke_tests.sh
#
# Required env vars (in ai_news_push.env):
#   TELEGRAM_BOT_TOKEN  — production bot (used to deliver the results summary)
#   SMOKE_CHAT_ID       — chat ID where the summary is sent
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/ai_news_push.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: env file not found: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is not set in $ENV_FILE}"
: "${SMOKE_CHAT_ID:?SMOKE_CHAT_ID is not set in $ENV_FILE}"

# Give the production bot time to start its polling loop before we run.
echo "Waiting 45s for production bot to start..."
sleep 45

echo "Starting smoke tests at $(date)..."
exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/tests/telegram_smoke_tester.py"
