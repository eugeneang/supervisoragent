"""
/ops command — read-only operational status for local agents.

Summarises launchd jobs, AI news scheduling, smoke-test activity, recent log
state, and git branch health without mutating services or repository state.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes


def _is_allowed(user_id: int) -> bool:
    from telegram_bot import is_user_allowed  # noqa: PLC0415
    return is_user_allowed(user_id)


try:
    from config import GIT_REPO_PATH, SMOKE_LOG_PATH
except ImportError:
    GIT_REPO_PATH = "."
    SMOKE_LOG_PATH = "logs/smoke_tests.log"


REPO_ROOT = Path(GIT_REPO_PATH).resolve()
AGENTS_ROOT = REPO_ROOT.parent
AI_NEWS_CONFIG = REPO_ROOT / "ai_news_config.json"
LOG_PATHS = {
    "bot": AGENTS_ROOT / "telegram_bot.error.log",
    "news": AGENTS_ROOT / "ai_news_push.log",
    "health": AGENTS_ROOT / "health_monitor.log",
    "smoke": Path(SMOKE_LOG_PATH),
}
SERVICE_LABELS = {
    "telegram_bot": "com.eugene.telegram_bot",
    "ai_news_push": "com.eugene.ai_news_push",
    "health_monitor": "com.eugene.health_monitor",
    "smoke_tests": "com.eugene.smoke_tests",
}
TOKEN_RE = re.compile(r"bot[^/\s]+/")


@dataclass
class LaunchdJob:
    pid: str
    status: str
    label: str

    @property
    def state(self) -> str:
        if self.pid != "-":
            return f"running pid={self.pid}"
        if self.status == "0":
            return "loaded"
        return f"exited status={self.status}"


def _run_command(cmd: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd) if cwd else None,
        timeout=10,
    )


def _redact(text: str) -> str:
    return TOKEN_RE.sub("bot[REDACTED]/", text)


def _parse_launchctl(output: str) -> dict[str, LaunchdJob]:
    jobs: dict[str, LaunchdJob] = {}
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or parts[0] == "PID":
            continue
        pid, status, label = parts
        jobs[label] = LaunchdJob(pid=pid, status=status, label=label)
    return jobs


def _launchd_jobs() -> tuple[dict[str, LaunchdJob], str | None]:
    try:
        result = _run_command(["launchctl", "list"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, f"unavailable ({exc.__class__.__name__})"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:120]
        return {}, f"unavailable ({detail or 'launchctl failed'})"
    return _parse_launchctl(result.stdout), None


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _tail_nonempty_line(path: Path, max_lines: int = 80) -> str:
    try:
        if not path.exists():
            return "no log found"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"unavailable ({exc})"
    for line in reversed(lines[-max_lines:]):
        stripped = line.strip()
        if stripped:
            return _redact(stripped[:220])
    return "log empty"


def _git_line(cmd: list[str]) -> str:
    try:
        result = _run_command(cmd, cwd=REPO_ROOT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable ({exc.__class__.__name__})"
    if result.returncode != 0:
        return "unavailable"
    return result.stdout.strip()


def _git_status() -> dict[str, str]:
    branch = _git_line(["git", "rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"
    upstream = _git_line(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    porcelain = _git_line(["git", "status", "--porcelain"])
    last_commit = _git_line(["git", "log", "-1", "--format=%h %s"]) or "unknown"

    if upstream and upstream != "unavailable":
        counts = _git_line(["git", "rev-list", "--left-right", "--count", f"{branch}...{upstream}"])
        if counts and counts != "unavailable":
            ahead, behind = counts.split()[:2]
            ahead_behind = f"{ahead}/{behind}"
        else:
            ahead_behind = "unknown"
    else:
        upstream = "none"
        ahead_behind = "unknown"

    return {
        "branch": branch,
        "upstream": upstream,
        "status": "clean" if porcelain == "" else "dirty",
        "ahead_behind": ahead_behind,
        "last_commit": last_commit,
    }


def build_ops_report() -> str:
    jobs, launch_error = _launchd_jobs()
    news_config = _read_json(AI_NEWS_CONFIG)
    git = _git_status()

    def job_state(name: str) -> str:
        if launch_error:
            return launch_error
        label = SERVICE_LABELS[name]
        job = jobs.get(label)
        return job.state if job else "not loaded"

    news_enabled = "enabled" if news_config.get("enabled", True) else "disabled"
    news_time = news_config.get("daily_push_time", "unknown")
    news_tz = news_config.get("timezone", "unknown")
    news_last = news_config.get("last_sent_date") or "never"

    return "\n".join([
        "Ops Status",
        "",
        "Telegram Bot",
        f"  Job: {job_state('telegram_bot')}",
        f"  Last log: {_tail_nonempty_line(LOG_PATHS['bot'])}",
        "",
        "AI News",
        f"  Config: {news_enabled}",
        f"  Schedule: {news_time} {news_tz}",
        f"  Last sent: {news_last}",
        f"  Job: {job_state('ai_news_push')}",
        f"  Last log: {_tail_nonempty_line(LOG_PATHS['news'])}",
        "",
        "Health Monitor",
        f"  Job: {job_state('health_monitor')}",
        f"  Last log: {_tail_nonempty_line(LOG_PATHS['health'])}",
        "",
        "Smoke Tests",
        f"  Job: {job_state('smoke_tests')}",
        f"  Last log: {_tail_nonempty_line(LOG_PATHS['smoke'])}",
        "",
        "Git",
        f"  Branch: {git['branch']}",
        f"  Upstream: {git['upstream']}",
        f"  Status: {git['status']}",
        f"  Ahead/behind: {git['ahead_behind']}",
        f"  Last commit: {git['last_commit']}",
    ])


async def ops_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    await update.message.reply_text(build_ops_report())
