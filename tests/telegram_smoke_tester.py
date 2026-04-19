"""
Telegram Bot In-Process Smoke Tester.

Calls each command handler function directly with a mock Update/Context,
captures the reply_text output, and checks it against an expected pattern.
After all tests run, sends a formatted summary to the configured chat via
the PRODUCTION bot token so you see results in Telegram without needing a
second bot.

This design avoids all cross-bot pitfalls:
  - No Telegram privacy-mode restrictions (bots only receive /commands)
  - No whitelist issues (we pass an authorized user ID directly)
  - No getUpdates conflicts with the production polling loop
  - No second bot token required for reading replies

Usage (standalone):
    python tests/telegram_smoke_tester.py

Environment variables (loaded from ai_news_push.env by run_smoke_tests.sh):
    TELEGRAM_BOT_TOKEN  — production bot token (used to send the summary)
    SMOKE_CHAT_ID       — chat ID where the summary is delivered
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

# Allow running from tests/ or from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [smoke] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SmokeResult:
    command: str
    description: str
    passed: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_update(user_id: int, chat_id: int) -> MagicMock:
    """Build a minimal mock Update that satisfies the bot's handlers."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = "SmokeTest"
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.text = ""
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


def _captured_reply(update: MagicMock) -> Optional[str]:
    """Return the text of the first reply_text call, or None."""
    calls = update.message.reply_text.call_args_list
    if not calls:
        return None
    first = calls[0]
    # Positional arg
    if first.args:
        return str(first.args[0])
    # Keyword arg
    return str(first.kwargs.get("text", ""))


# ---------------------------------------------------------------------------
# Whitelist helper
# ---------------------------------------------------------------------------

def _first_authorized_user() -> Optional[int]:
    """Return the first user ID from whitelist.json, or None if empty."""
    wf = _REPO_ROOT / "whitelist.json"
    if wf.exists():
        try:
            data = json.loads(wf.read_text(encoding="utf-8"))
            users = data.get("allowed_users", [])
            if users:
                return int(users[0])
        except Exception:
            logger.warning("Could not read whitelist.json", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------

@dataclass
class TestSpec:
    command: str
    description: str
    handler_attr: str          # attribute name on the telegram_bot module
    context_args: list[str] = field(default_factory=list)
    pattern: re.Pattern = field(default_factory=lambda: re.compile(r".*"))
    needs_auth: bool = False   # True = skip if no authorized user found
    timeout: float = 10.0      # seconds before marking as failed


_TEST_SPECS: list[TestSpec] = [
    TestSpec(
        command="/ping",
        description="Liveness check — expects 'pong'",
        handler_attr="ping_command",
        pattern=re.compile(r"pong", re.IGNORECASE),
    ),
    TestSpec(
        command="/help",
        description="Help text lists all registered commands",
        handler_attr="help_command",
        pattern=re.compile(r"/ping.*\n.*/design|Available commands", re.IGNORECASE | re.DOTALL),
    ),
    TestSpec(
        command="/id",
        description="/id echoes the caller's Telegram user ID",
        handler_attr="show_id",
        pattern=re.compile(r"Telegram user ID", re.IGNORECASE),
    ),
    TestSpec(
        command="/start",
        description="/start greets the user by name",
        handler_attr="start",
        pattern=re.compile(r"hello|hi|SmokeTest", re.IGNORECASE),
    ),
    TestSpec(
        command="/build_status",
        description="Build status returns current supervisor state",
        handler_attr="build_status_command",
        pattern=re.compile(r"State:|IDLE|BUILDING|AWAITING", re.IGNORECASE),
        needs_auth=True,
    ),
    TestSpec(
        command="/ai",
        description="/ai returns an AI news digest",
        handler_attr="ai_command",
        pattern=re.compile(r"AI|news|digest|model|research|summary|GPT|LLM", re.IGNORECASE),
        needs_auth=True,
        timeout=45.0,   # fetches external content + runs LLM
    ),
    TestSpec(
        command="/recap",
        description="Daily activity summary",
        handler_attr="recap_handler",
        pattern=re.compile(r"(?s).*Commits.*Supervisor.*Tests Today.*"),
    ),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class InProcessSmokeTester:

    def __init__(self, chat_id: int, bot_token: str) -> None:
        self.chat_id = chat_id
        self.bot = Bot(token=bot_token)
        self.authorized_user_id = _first_authorized_user()
        if self.authorized_user_id:
            logger.info("Using authorized user ID %s for auth-gated tests.", self.authorized_user_id)
        else:
            logger.warning("No authorized user found in whitelist.json — auth-gated tests will be skipped.")

    async def _run_one(self, spec: TestSpec) -> SmokeResult:
        import telegram_bot as tb  # imported here so env is fully loaded first

        if spec.needs_auth and self.authorized_user_id is None:
            return SmokeResult(
                command=spec.command,
                description=spec.description,
                passed=False,
                detail="Skipped — no authorized user in whitelist.json",
            )

        user_id = self.authorized_user_id if spec.needs_auth else 0
        update = _make_update(user_id=user_id, chat_id=self.chat_id)
        ctx = _make_context(args=spec.context_args)

        handler = getattr(tb, spec.handler_attr)

        try:
            await asyncio.wait_for(handler(update, ctx), timeout=spec.timeout)
        except asyncio.TimeoutError:
            return SmokeResult(
                command=spec.command,
                description=spec.description,
                passed=False,
                detail=f"Handler did not return within {spec.timeout:.0f}s",
            )
        except Exception as exc:
            logger.exception("%s raised an exception", spec.command)
            return SmokeResult(
                command=spec.command,
                description=spec.description,
                passed=False,
                detail=f"Exception: {exc}",
            )

        reply = _captured_reply(update)
        if reply is None:
            return SmokeResult(
                command=spec.command,
                description=spec.description,
                passed=False,
                detail="Handler completed but sent no reply",
            )

        if spec.pattern.search(reply):
            snippet = reply[:100].replace("\n", " ")
            logger.info("%s PASSED — %r", spec.command, snippet)
            return SmokeResult(
                command=spec.command,
                description=spec.description,
                passed=True,
                detail=snippet,
            )

        snippet = reply[:100].replace("\n", " ")
        logger.warning("%s FAILED — pattern %r not in %r", spec.command, spec.pattern.pattern, snippet)
        return SmokeResult(
            command=spec.command,
            description=spec.description,
            passed=False,
            detail=f"Pattern not matched. Got: {snippet!r}",
        )

    async def run_all(self) -> list[SmokeResult]:
        results: list[SmokeResult] = []
        for spec in _TEST_SPECS:
            logger.info("Testing %s…", spec.command)
            result = await self._run_one(spec)
            results.append(result)
        return results

    async def send_summary(self, results: list[SmokeResult]) -> None:
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        header = f"\U0001f916 *Smoke Test Results* — {passed}/{total} passed"
        lines = [header, ""]
        for r in results:
            icon = "\u2705" if r.passed else "\u274c"
            lines.append(f"{icon} `{r.command}` — {r.description}")
            if not r.passed:
                lines.append(f"   \u26a0\ufe0f {r.detail}")
        footer = "\U0001f389 All checks passed!" if passed == total else "\u26a0\ufe0f Some checks failed — see logs."
        lines += ["", footer]
        text = "\n".join(lines)

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.info("Summary sent to chat %s.", self.chat_id)
        except TelegramError:
            logger.exception("Failed to send summary to chat %s", self.chat_id)
            # Fallback: plain text
            try:
                plain = text.replace("*", "").replace("`", "")
                await self.bot.send_message(chat_id=self.chat_id, text=plain)
            except Exception:
                logger.exception("Fallback send also failed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set — cannot send summary.")
        return 1

    chat_id_str = os.environ.get("SMOKE_CHAT_ID", "")
    if not chat_id_str:
        logger.error("SMOKE_CHAT_ID is not set — don't know where to send the summary.")
        return 1

    chat_id = int(chat_id_str)
    tester = InProcessSmokeTester(chat_id=chat_id, bot_token=token)
    results = await tester.run_all()
    await tester.send_summary(results)

    failed = sum(1 for r in results if not r.passed)
    if failed:
        logger.warning("%d test(s) failed — bot will still continue.", failed)
    return 0


async def run_inprocess(
    chat_id: int,
    bot_token: str,
    send_summary: bool = False,
) -> tuple[bool, list[SmokeResult]]:
    """
    Programmatic entry point used by supervisor_loop after every successful build.

    Runs all registered tests in-process and returns (all_passed, results).
    Does NOT send a Telegram summary unless send_summary=True, because the
    supervisor loop handles its own notifications.
    """
    tester = InProcessSmokeTester(chat_id=chat_id, bot_token=bot_token)
    results = await tester.run_all()
    if send_summary:
        await tester.send_summary(results)
    all_passed = all(r.passed for r in results)
    return all_passed, results


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
