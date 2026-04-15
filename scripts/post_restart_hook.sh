#!/usr/bin/env bash
# post_restart_hook.sh
# ---------------------
# Called after the Telegram bot service starts to run smoke tests.
#
# Integration options:
#   systemd  — add to your service unit:
#                 ExecStartPost=/path/to/repo/scripts/post_restart_hook.sh
#   Docker   — call from your entrypoint script after starting the bot process.
#   Launchd  — call via a separate plist that depends on the bot plist.
#
# The script waits briefly for the bot to fully initialise before testing.
# It NEVER blocks the bot startup — exits 0 regardless of test outcome.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

LOG_FILE="${REPO_ROOT}/logs/smoke_test.log"
mkdir -p "${REPO_ROOT}/logs"

echo "[post_restart_hook] $(date -u +"%Y-%m-%dT%H:%M:%SZ") Starting smoke tests..." | tee -a "${LOG_FILE}"

# Wait for the bot to initialise (adjust if your bot takes longer to start).
SLEEP_BEFORE_TEST="${SMOKE_STARTUP_DELAY:-8}"
echo "[post_restart_hook] Waiting ${SLEEP_BEFORE_TEST}s for bot to initialise..." | tee -a "${LOG_FILE}"
sleep "${SLEEP_BEFORE_TEST}"

# Run the smoke tester in a subshell so failures never propagate upward.
(
    cd "${REPO_ROOT}"
    # Prefer the virtualenv python if present
    if [ -f "${REPO_ROOT}/.venv/bin/python" ]; then
        PYTHON="${REPO_ROOT}/.venv/bin/python"
    elif [ -f "${REPO_ROOT}/venv/bin/python" ]; then
        PYTHON="${REPO_ROOT}/venv/bin/python"
    else
        PYTHON="python3"
    fi

    echo "[post_restart_hook] Running: ${PYTHON} tests/telegram_smoke_tester.py" | tee -a "${LOG_FILE}"
    "${PYTHON}" tests/telegram_smoke_tester.py 2>&1 | tee -a "${LOG_FILE}"
) || true

echo "[post_restart_hook] $(date -u +"%Y-%m-%dT%H:%M:%SZ") Smoke tests finished (non-blocking)." | tee -a "${LOG_FILE}"
exit 0
