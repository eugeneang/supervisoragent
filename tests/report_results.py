"""Format smoke-test results and send a summary to the test chat."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    command: str
    description: str
    passed: bool
    detail: str = ""          # error message or matched snippet
    attempts: int = 1


def format_summary(results: list[TestResult]) -> str:
    """Build a human-readable summary string."""
    lines: list[str] = ["\U0001f916 *Telegram Bot Smoke Test Results*\n"]

    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    for r in results:
        icon = "\u2705" if r.passed else "\u274c"
        attempt_tag = f" (attempt {r.attempts})"
        lines.append(f"{icon} `{r.command}` — {r.description}{attempt_tag}")
        if not r.passed and r.detail:
            lines.append(f"   \u26a0\ufe0f {r.detail}")

    lines.append("")
    lines.append(
        f"Summary: {len(passed)}/{len(results)} passed"
        + (" \U0001f389" if not failed else " — some failures, check logs")
    )
    return "\n".join(lines)


async def send_summary(
    bot,          # telegram.Bot instance
    chat_id: int,
    results: list[TestResult],
) -> None:
    """Send the formatted summary back to the test chat."""
    text = format_summary(results)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("Failed to send smoke-test summary to chat %s", chat_id)
        # Fallback: plain text without markdown
        try:
            plain = format_summary(results).replace("*", "").replace("`", "")
            await bot.send_message(chat_id=chat_id, text=plain)
        except Exception:
            logger.exception("Fallback plain-text send also failed")
