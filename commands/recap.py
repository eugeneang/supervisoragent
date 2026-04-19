"""
/recap command — daily activity summary.

Sections:
  🔀 Commits    — last 5 git commits (hash, author, relative time, subject)
  🖥 Supervisor — supervisorctl process states
  🧪 Tests Today — count of smoke test runs logged today
"""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

# is_user_allowed is defined in telegram_bot — import lazily to avoid circular import.
def _is_allowed(user_id: int) -> bool:
    from telegram_bot import is_user_allowed  # noqa: PLC0415
    return is_user_allowed(user_id)

# ---------------------------------------------------------------------------
# Config (mirrors config.py defaults; import from there if available)
# ---------------------------------------------------------------------------
try:
    from config import SMOKE_LOG_PATH, GIT_REPO_PATH
except ImportError:
    SMOKE_LOG_PATH = "logs/smoke_tests.log"
    GIT_REPO_PATH = "."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_commits() -> str:
    """Return a formatted block of the last 5 git commits."""
    try:
        result = subprocess.run(
            ["git", "log", "--format=%h|%an|%ar|%s", "-5"],
            capture_output=True,
            text=True,
            check=True,
            cwd=GIT_REPO_PATH,
            timeout=10,
        )
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        if not lines:
            return "  (no commits found)"
        rows = []
        for line in lines:
            parts = line.split("|", 3)
            if len(parts) == 4:
                h, author, when, subject = parts
                rows.append(f"  {h} [{author}, {when}]\n    {subject}")
            else:
                rows.append(f"  {line}")
        return "\n".join(rows)
    except FileNotFoundError:
        return "  ⚠️ git not found"
    except subprocess.CalledProcessError as exc:
        return f"  ⚠️ git error: {exc.returncode}"
    except subprocess.TimeoutExpired:
        return "  ⚠️ git timed out"


def _supervisor_state() -> str:
    """Return launchd service states for com.eugene.* jobs."""
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        rows = []
        for line in result.stdout.strip().splitlines():
            # launchctl list columns: PID  LastExitStatus  Label
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[2].startswith("com.eugene."):
                pid, _exit, label = parts
                short = label.replace("com.eugene.", "")
                icon = "🟢" if pid != "-" else "🔴"
                rows.append(f"  {icon} {short}")
        if not rows:
            return "  (no com.eugene services found)"
        return "\n".join(rows)
    except FileNotFoundError:
        return "  ⚠️ launchctl not found"
    except subprocess.CalledProcessError as exc:
        return f"  ⚠️ launchctl error: {exc.returncode}"
    except subprocess.TimeoutExpired:
        return "  ⚠️ launchctl timed out"


def _smoke_test_count() -> str:
    """Count smoke test runs logged today."""
    today = datetime.date.today().isoformat()  # e.g. "2025-01-15"
    log_path = Path(SMOKE_LOG_PATH)
    try:
        if not log_path.exists():
            return f"  0 (log file not found: {log_path})"
        count = 0
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if today in line:
                    count += 1
        return f"  {count} run(s) logged on {today}"
    except OSError as exc:
        return f"  ⚠️ Could not read log: {exc}"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def recap_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a daily activity summary."""
    if not update.effective_user or not update.message:
        return
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    commits = _git_commits()
    supervisor = _supervisor_state()
    tests = _smoke_test_count()

    message = (
        "📋 *Daily Recap*\n"
        "\n"
        "🔀 *Commits* (last 5)\n"
        f"{commits}\n"
        "\n"
        "🖥 *Supervisor*\n"
        f"{supervisor}\n"
        "\n"
        "🧪 *Tests Today*\n"
        f"{tests}"
    )

    await update.message.reply_text(message, parse_mode="Markdown")
