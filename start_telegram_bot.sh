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

# ── PID-file guard ────────────────────────────────────────────────────────────
# Prevents a second instance from starting if launchd somehow has two jobs
# pointing at this script (e.g. com.eugene.supervisor + com.eugene.telegram_bot).
PIDFILE="/tmp/telegram_bot.pid"
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "ERROR: telegram_bot.py is already running as PID $OLD_PID — refusing to start a duplicate." >&2
        echo "       If this is stale, delete $PIDFILE and retry." >&2
        exit 1
    else
        echo "WARNING: Stale PID file found (PID $OLD_PID is not running). Removing." >&2
        rm -f "$PIDFILE"
    fi
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/telegram_bot.py"
