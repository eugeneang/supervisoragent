"""Telegram Bot Command Smoke Test Agent.

Runs after every bot service restart to verify that all registered commands
return expected responses. Destructive commands (approve, commit, etc.) are
never executed. Test failures emit warnings but do NOT block the bot startup.

Usage (standalone):
    python tests/telegram_smoke_tester.py

Environment variables (or config/smoke_test_config.yaml):
    SMOKE_BOT_TOKEN   — dedicated test-bot token (separate from production)
    SMOKE_CHAT_ID     — private test chat ID to send commands to
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import yaml  # PyYAML — listed in requirements

from telegram import Bot, Update
from telegram.error import TelegramError

# Local imports — work whether run from repo root or tests/ directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.command_registry import CommandSpec, get_testable_commands
from tests.report_results import TestResult, send_summary

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [smoke_tester] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "smoke_test_config.yaml"


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load YAML config, then override with environment variables."""
    cfg: dict = {
        "smoke_bot_token": "",
        "smoke_chat_id": 0,
        "timeout_seconds": 15,
        "retry_attempts": 3,
        "poll_interval": 1.5,   # seconds between update polls
        "poll_timeout": 30,     # long-poll timeout sent to Telegram
    }

    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            file_cfg = yaml.safe_load(fh) or {}
        cfg.update({k: v for k, v in file_cfg.items() if v is not None})

    # Environment variables take priority
    if os.environ.get("SMOKE_BOT_TOKEN"):
        cfg["smoke_bot_token"] = os.environ["SMOKE_BOT_TOKEN"]
    if os.environ.get("SMOKE_CHAT_ID"):
        cfg["smoke_chat_id"] = int(os.environ["SMOKE_CHAT_ID"])

    return cfg


# ---------------------------------------------------------------------------
# Core tester
# ---------------------------------------------------------------------------

class TelegramSmokeTester:
    """Sends each registered command and asserts the response matches a pattern."""

    def __init__(self, config: dict) -> None:
        self.config = config
        token = config.get("smoke_bot_token", "")
        if not token:
            raise ValueError(
                "No smoke bot token found. "
                "Set SMOKE_BOT_TOKEN env var or smoke_bot_token in "
                "config/smoke_test_config.yaml"
            )
        self.bot = Bot(token=token)
        self.chat_id: int = int(config.get("smoke_chat_id", 0))
        if not self.chat_id:
            raise ValueError(
                "No smoke chat ID found. "
                "Set SMOKE_CHAT_ID env var or smoke_chat_id in "
                "config/smoke_test_config.yaml"
            )
        self._last_update_id: Optional[int] = None

    async def _drain_pending_updates(self) -> None:
        """Consume any updates that arrived before the test run so we don't
        accidentally match old messages."""
        try:
            updates = await self.bot.get_updates(
                offset=self._last_update_id,
                timeout=2,
                allowed_updates=["message"],
            )
            if updates:
                self._last_update_id = updates[-1].update_id + 1
                logger.info("Drained %d pending update(s).", len(updates))
        except TelegramError:
            logger.warning("Could not drain pending updates.", exc_info=True)

    async def _send_command(self, spec: CommandSpec) -> None:
        """Send the command (with optional args) to the test chat."""
        text = spec.command
        if spec.args:
            text = f"{spec.command} {spec.args}"
        await self.bot.send_message(chat_id=self.chat_id, text=text)
        logger.info("Sent: %s", text)

    async def _wait_for_reply(
        self,
        spec: CommandSpec,
        deadline: float,
    ) -> Optional[str]:
        """Poll getUpdates until a non-command reply arrives or deadline passes."""
        poll_interval = float(self.config.get("poll_interval", 1.5))
        poll_timeout = int(self.config.get("poll_timeout", 30))

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            actual_timeout = min(poll_timeout, int(remaining))
            if actual_timeout < 1:
                actual_timeout = 1

            try:
                updates = await self.bot.get_updates(
                    offset=self._last_update_id,
                    timeout=actual_timeout,
                    allowed_updates=["message"],
                )
            except TelegramError:
                logger.warning("get_updates failed, retrying…", exc_info=True)
                await asyncio.sleep(poll_interval)
                continue

            for upd in updates:
                self._last_update_id = upd.update_id + 1
                msg = upd.message
                if msg is None:
                    continue
                # Only accept messages in our test chat
                if msg.chat.id != self.chat_id:
                    continue
                # Skip messages sent BY this bot (our own commands)
                if msg.text and msg.text.startswith("/"):
                    continue
                # Accept any text reply — this is the bot's response
                if msg.text:
                    return msg.text

            await asyncio.sleep(poll_interval)

        return None

    async def _run_single(self, spec: CommandSpec) -> TestResult:
        """Run one command test with retry logic."""
        max_attempts = spec.retry_attempts or int(self.config.get("retry_attempts", 3))
        timeout = spec.timeout_seconds or int(self.config.get("timeout_seconds", 15))
        last_detail = "No response received within timeout"

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "Testing %s (attempt %d/%d)…", spec.command, attempt, max_attempts
            )
            try:
                await self._drain_pending_updates()
                await self._send_command(spec)
                deadline = time.monotonic() + timeout
                reply = await self._wait_for_reply(spec, deadline)

                if reply is None:
                    last_detail = f"No reply received within {timeout}s"
                    logger.warning(
                        "%s — attempt %d: %s", spec.command, attempt, last_detail
                    )
                elif spec.pattern.search(reply):
                    snippet = reply[:120].replace("\n", " ")
                    logger.info("%s PASSED (matched: %r)", spec.command, snippet)
                    return TestResult(
                        command=spec.command,
                        description=spec.description,
                        passed=True,
                        detail=snippet,
                        attempts=attempt,
                    )
                else:
                    last_detail = (
                        f"Pattern {spec.pattern.pattern!r} not found in reply: "
                        f"{reply[:120]!r}"
                    )
                    logger.warning(
                        "%s — attempt %d: %s", spec.command, attempt, last_detail
                    )

            except TelegramError:
                last_detail = "TelegramError during test"
                logger.exception("%s — attempt %d failed with TelegramError", spec.command, attempt)
            except Exception:
                last_detail = "Unexpected error during test"
                logger.exception("%s — attempt %d unexpected error", spec.command, attempt)

            # Brief back-off before retry
            if attempt < max_attempts:
                await asyncio.sleep(2)

        return TestResult(
            command=spec.command,
            description=spec.description,
            passed=False,
            detail=last_detail,
            attempts=max_attempts,
        )

    async def run_all(self) -> list[TestResult]:
        """Execute every testable command in sequence and return results."""
        specs = get_testable_commands()
        if not specs:
            logger.warning("No testable commands found in registry.")
            return []

        logger.info(
            "Starting smoke tests: %d command(s) to test.", len(specs)
        )

        results: list[TestResult] = []
        for spec in specs:
            result = await self._run_single(spec)
            results.append(result)
            # Small gap between commands to avoid flooding the bot
            await asyncio.sleep(1)

        # Send summary back to the test chat
        try:
            await send_summary(self.bot, self.chat_id, results)
        except Exception:
            logger.exception("Failed to send summary")

        passed = sum(1 for r in results if r.passed)
        logger.info(
            "Smoke tests complete: %d/%d passed.", passed, len(results)
        )
        return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> int:
    """Return exit code 0 on success (all pass), 1 on any failure."""
    try:
        config = _load_config()
    except Exception as exc:
        logger.error("Failed to load smoke test config: %s", exc)
        return 1

    try:
        tester = TelegramSmokeTester(config)
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    results = await tester.run_all()

    any_failed = any(not r.passed for r in results)
    # Non-blocking: always return 0 so the bot service starts regardless.
    # Change to `return 1 if any_failed else 0` if you want hard failures.
    if any_failed:
        logger.warning(
            "Some smoke tests failed — bot service will still start. "
            "Check the test chat for details."
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
