#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  echo "gcommit: .venv not found; create the virtualenv first" >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

MSG_FILE="$(mktemp -t gcommit_msg)"
cleanup() { rm -f "$MSG_FILE"; }
trap cleanup EXIT

python smart_commit.py >"$MSG_FILE"
MSG="$(head -n 1 "$MSG_FILE" | tr -d '\r')"
if [[ -z "${MSG// }" ]]; then
  echo "gcommit: empty commit message" >&2
  exit 1
fi

printf '%s\n' "$MSG"
git commit -F "$MSG_FILE"
