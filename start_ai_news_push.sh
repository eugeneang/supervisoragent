#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

# launchd loads TELEGRAM_* from ai_news_push.env via the plist; manual runs load here.
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  ENV_FILE="$ROOT/ai_news_push.env"
  if [ ! -f "$ENV_FILE" ]; then
    echo "ai_news_push: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or create $ENV_FILE (see ai_news_push.env.example)" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "ai_news_push: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID still empty after sourcing env" >&2
  exit 1
fi

source .venv/bin/activate
exec python ai_news_push.py "$@"
