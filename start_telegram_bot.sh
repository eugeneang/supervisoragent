#!/bin/bash
# start_telegram_bot.sh — Start the Telegram supervisor bot.
# Loads secrets from ai_news_push.env (shared secret store for all supervisor
# services). Works identically whether invoked manually or via launchd.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/ai_news_push.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: env file not found: $ENV_FILE" >&2
  echo "       Copy ai_news_push.env.example to ai_news_push.env and fill in the secrets." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is not set in $ENV_FILE}"
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is not set in $ENV_FILE}"

exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/telegram_bot.py"
