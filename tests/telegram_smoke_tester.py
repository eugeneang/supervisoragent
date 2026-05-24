"""
Telegram smoke tester — in-process async test harness.

Exports:
  _TEST_SPECS   : list[TestSpec]
  run_inprocess : async function that runs all specs against a bot module
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# TestSpec dataclass
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


# ---------------------------------------------------------------------------
# Test specifications
# ---------------------------------------------------------------------------

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
    TestSpec(
        command="/ops",
        description="Operational status dashboard",
        handler_attr="ops_handler",
        pattern=re.compile(r"Ops Status.*Telegram Bot.*AI News.*Git", re.IGNORECASE | re.DOTALL),
        needs_auth=True,
    ),
    TestSpec(
        command="/idea",
        description="Idea generation with approval flow",
        handler_attr="idea_command",
        pattern=re.compile(
            r"💡 \*Idea:\*.*🛠 \*Tools:\*.*_Approve, revise, or cancel\?_",
            re.DOTALL,
        ),
        needs_auth=True,
        timeout=10.0,
    ),
]

# ---------------------------------------------------------------------------
# Helpers for building mock Update / Context
# ---------------------------------------------------------------------------

SMOKE_USER_ID = 999_999_999
SMOKE_USER_NAME = "SmokeTest"


def _make_mock_update(spec: TestSpec) -> tuple[Any, Any]:
    """Build a minimal mock Update and Context for a given TestSpec."""
    # --- User ---
    user = MagicMock()
    user.id = SMOKE_USER_ID
    user.first_name = SMOKE_USER_NAME
    user.is_bot = False

    # --- Message ---
    message = MagicMock()
    message.text = spec.command + (
        (" " + " ".join(spec.context_args)) if spec.context_args else ""
    )

    # Capture all reply_text calls
    _replies: list[str] = []

    async def _reply_text(text: str, **kwargs):
        _replies.append(text)

    message.reply_text = _reply_text

    # --- Chat ---
    chat = MagicMock()
    chat.id = SMOKE_USER_ID

    # --- Update ---
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.message = message

    # --- Bot ---
    bot = MagicMock()

    async def _send_message(chat_id, text, **kwargs):
        _replies.append(text)

    bot.send_message = _send_message

    # --- Context ---
    context = MagicMock()
    context.args = spec.context_args[:]
    context.bot = bot
    context.user_data = {}
    # Attach reply store to context so _get_reply_text can find it
    context._smoke_replies = _replies
    # Also attach to message for _get_reply_text
    message._smoke_replies = _replies

    return update, context


def _get_reply_text(context: Any) -> str:
    """Return all captured reply texts joined as a single string."""
    replies: list[str] = getattr(context, "_smoke_replies", [])
    return "\n".join(replies)


# ---------------------------------------------------------------------------
# Patching helpers for auth-gated handlers
# ---------------------------------------------------------------------------

def _patch_auth(bot_module, spec: TestSpec):
    """
    Return a context-manager stack that makes _is_authorized return True
    and patches any external I/O that would block in-process execution.
    """
    import contextlib
    import unittest.mock as um

    patches = []

    # Force auth to pass for this user
    patches.append(
        um.patch.object(bot_module, "AUTHORIZED_USER_ID", SMOKE_USER_ID)
    )

    return contextlib.ExitStack(), patches


# ---------------------------------------------------------------------------
# run_inprocess — main entry point for the smoke runner
# ---------------------------------------------------------------------------

async def run_inprocess(bot_module) -> list[dict]:
    """
    Iterate over _TEST_SPECS, invoke each handler via a mock Update/Context,
    assert the reply text matches spec.pattern, and return a results list.

    Each result dict has keys:
      spec     : str   (command string)
      status   : str   ("PASS" | "FAIL" | "SKIP" | "ERROR")
      reply    : str   (captured reply, if available)
      reason   : str   (error/skip message, if applicable)
    """
    import contextlib
    import unittest.mock as um

    results: list[dict] = []

    for spec in _TEST_SPECS:
        handler = getattr(bot_module, spec.handler_attr, None)
        if handler is None:
            results.append({
                "spec": spec.command,
                "status": "SKIP",
                "reason": f"handler '{spec.handler_attr}' not found on bot module",
            })
            continue

        update, context = _make_mock_update(spec)

        # For auth-gated specs, patch AUTHORIZED_USER_ID so our smoke user passes
        auth_patch = (
            um.patch.object(bot_module, "AUTHORIZED_USER_ID", SMOKE_USER_ID)
            if spec.needs_auth
            else contextlib.nullcontext()
        )

        # Patch heavy external calls so tests stay fast & offline-safe
        _external_patches: list[um.patch] = []

        if spec.handler_attr == "ai_command":
            _external_patches.append(
                um.patch(
                    "agents.ai_news_agent.get_ai_news_digest",
                    return_value="AI news digest summary: LLM research GPT model",
                )
            )

        if spec.handler_attr == "idea_command":
            _external_patches.append(
                um.patch(
                    "commands.idea.generate_idea",
                    new=AsyncMock(
                        return_value=(
                            "💡 *Idea:* Smart News Aggregator\n\n"
                            "Pulls AI news and summarises it via Claude.\n\n"
                            "🛠 *Tools:* ddgs, Claude, Telegram bot\n\n"
                            "📈 *Effort:* Small\n\n"
                            "_Approve, revise, or cancel?_"
                        )
                    ),
                )
            )

        if spec.handler_attr == "recap_handler":
            _external_patches.append(
                um.patch(
                    "commands.recap.build_recap",
                    return_value=(
                        "📋 *Daily Recap*\n"
                        "Commits: 3\n"
                        "Supervisor: IDLE\n"
                        "Tests Today: 8 passed"
                    ),
                )
            )

        if spec.handler_attr == "ops_handler":
            _external_patches.append(
                um.patch(
                    "commands.ops.build_ops_report",
                    return_value=(
                        "Ops Status\n\n"
                        "Telegram Bot\n"
                        "AI News\n"
                        "Git"
                    ),
                )
            )

        try:
            with auth_patch:
                # Apply external patches
                stack = contextlib.ExitStack()
                for p in _external_patches:
                    stack.enter_context(p)
                with stack:
                    await asyncio.wait_for(
                        handler(update, context),
                        timeout=spec.timeout,
                    )

            reply = _get_reply_text(context)
            passed = bool(spec.pattern.search(reply))
            results.append({
                "spec": spec.command,
                "status": "PASS" if passed else "FAIL",
                "reply": reply,
            })

        except asyncio.TimeoutError:
            results.append({
                "spec": spec.command,
                "status": "ERROR",
                "reason": f"Timed out after {spec.timeout}s",
            })
        except Exception as exc:
            results.append({
                "spec": spec.command,
                "status": "ERROR",
                "reason": str(exc),
            })

    return results
