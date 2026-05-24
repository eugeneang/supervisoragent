"""
/logs command — safely tail predefined local agent logs.

The command is read-only, accepts only known aliases, redacts secrets, and
truncates output to stay within Telegram message limits.
"""
from __future__ import annotations

import re
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes


def _is_allowed(user_id: int) -> bool:
    from telegram_bot import is_user_allowed  # noqa: PLC0415
    return is_user_allowed(user_id)


AGENTS_ROOT = Path("/Users/eugene/Agents")
DEFAULT_LINES = 40
MAX_LINES = 120
TELEGRAM_LIMIT = 3900

LOG_ALIASES = {
    "bot": AGENTS_ROOT / "telegram_bot.error.log",
    "bot-out": AGENTS_ROOT / "telegram_bot.log",
    "news": AGENTS_ROOT / "ai_news_push.log",
    "health": AGENTS_ROOT / "health_monitor.log",
    "smoke": AGENTS_ROOT / "smoke_tests.log",
    "smoke-error": AGENTS_ROOT / "smoke_tests.error.log",
    "supervisor": AGENTS_ROOT / "supervisor.log",
}
ALL_ALIASES = ("bot", "news", "health", "smoke")

REDACTIONS = [
    (re.compile(r"bot[^/\s]+/"), "bot[REDACTED]/"),
    (re.compile(r"(ANTHROPIC_API_KEY\s*=\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(TELEGRAM_BOT_TOKEN\s*=\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(X-Api-Key:\s*)\S+", re.IGNORECASE), r"\1[REDACTED]"),
]


def _valid_aliases_text() -> str:
    return ", ".join(sorted([*LOG_ALIASES.keys(), "all"]))


def parse_args(args: list[str]) -> tuple[str, int, str | None]:
    alias = args[0].lower() if args else "bot"
    if alias not in LOG_ALIASES and alias != "all":
        return alias, DEFAULT_LINES, f"Unknown log alias '{alias}'. Valid aliases: {_valid_aliases_text()}"

    if len(args) < 2:
        return alias, 8 if alias == "all" else DEFAULT_LINES, None

    try:
        requested = int(args[1])
    except ValueError:
        return alias, DEFAULT_LINES, "Line count must be a number."

    return alias, max(1, min(requested, MAX_LINES)), None


def redact(text: str) -> str:
    redacted = text
    for pattern, replacement in REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def tail_lines(path: Path, lines: int) -> tuple[str, str | None]:
    try:
        if not path.exists():
            return "", "log file not found"
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return "", f"could not read log: {exc}"

    snippet = "\n".join(content[-lines:])
    return redact(snippet), None


def truncate_message(text: str, limit: int = TELEGRAM_LIMIT) -> str:
    if len(text) <= limit:
        return text
    marker = "...(truncated)\n"
    keep = max(0, limit - len(marker))
    return marker + text[-keep:]


def format_single_log(alias: str, lines: int) -> str:
    path = LOG_ALIASES[alias]
    body, error = tail_lines(path, lines)
    if error:
        body = error
    message = "\n".join([
        f"Logs: {alias}",
        f"Path: {path.name}",
        f"Lines: {lines}",
        "",
        body or "(empty)",
    ])
    return truncate_message(message)


def format_all_logs(lines: int) -> str:
    sections = []
    for alias in ALL_ALIASES:
        path = LOG_ALIASES[alias]
        body, error = tail_lines(path, lines)
        sections.append("\n".join([
            f"== {alias} ({path.name}) ==",
            error if error else (body or "(empty)"),
        ]))
    return truncate_message("Logs: all\n\n" + "\n\n".join(sections))


def build_logs_report(args: list[str]) -> str:
    alias, lines, error = parse_args(args)
    if error:
        return error
    if alias == "all":
        return format_all_logs(lines)
    return format_single_log(alias, lines)


async def logs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    await update.message.reply_text(build_logs_report(context.args or []))
