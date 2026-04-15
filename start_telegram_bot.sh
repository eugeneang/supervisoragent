#!/bin/bash
# start_telegram_bot.sh — Start the Telegram supervisor bot.
# Follows the same env-loading pattern as start_ai_news_push.sh.
# Both read from ai_news_push.env, which is the shared secret store for all
# supervisor services. Add ANTHROPIC_API_KEY there if it is not already set.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

ENV_FILE="$ROOT/ai_news_push.env"
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  if [ ! -f "$ENV_FILE" ]; then
    echo "telegram_bot: create $ENV_FILE with TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "telegram_bot: TELEGRAM_BOT_TOKEN still empty after sourcing env" >&2
  exit 1
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "telegram_bot: ANTHROPIC_API_KEY not set — /design and /approve will fail" >&2
  # Non-fatal: Ollama-based features still work without this key.
fi

source .venv/bin/activate
exec python telegram_bot.py "$@"
