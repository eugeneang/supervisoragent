"""Registry of all testable Telegram bot commands with expected response patterns."""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CommandSpec:
    """Specification for a single testable command."""
    command: str                    # e.g. "/ping"
    args: str                       # extra args to append, e.g. "test feature request"
    pattern: re.Pattern             # compiled regex the response must match
    description: str                # human-readable description of what is being tested
    timeout_seconds: int = 15       # per-command timeout
    retry_attempts: int = 3         # max retries on failure
    # Optional cleanup: sent after a PASSING test to restore bot state.
    # e.g. send /reject after /design to avoid leaving AWAITING_APPROVAL state.
    cleanup_command: Optional[str] = None
    cleanup_pattern: Optional[re.Pattern] = None


# Commands that must NEVER be executed by the test agent.
# These are destructive / state-mutating operations.
SKIP_COMMANDS: set[str] = {
    "/approve",
    "/reject",
    "/commit",
    "/merge",
    "/deploy",
    "/reset_build",       # force-resets supervisor state — skip
}

# All testable commands ordered from safest to most complex.
COMMAND_REGISTRY: list[CommandSpec] = [
    CommandSpec(
        command="/ping",
        args="",
        pattern=re.compile(r"pong", re.IGNORECASE),
        description="Liveness check — expects 'pong'",
    ),
    CommandSpec(
        command="/help",
        args="",
        pattern=re.compile(r"/ping|/design|Available commands", re.IGNORECASE),
        description="Help text lists known commands",
    ),
    CommandSpec(
        command="/id",
        args="",
        pattern=re.compile(r"Telegram user ID", re.IGNORECASE),
        description="/id echoes the caller's user ID",
    ),
    CommandSpec(
        command="/start",
        args="",
        pattern=re.compile(r"hello", re.IGNORECASE),
        description="Bot greeting on /start",
    ),
    CommandSpec(
        command="/build_status",
        args="",
        pattern=re.compile(r"state|IDLE|build|status", re.IGNORECASE),
        description="Build status shows current supervisor state",
    ),
    CommandSpec(
        command="/ai",
        args="",
        pattern=re.compile(r"AI|news|digest|model|openai|anthropic|summary", re.IGNORECASE),
        description="/ai fetches and returns an AI news digest",
        timeout_seconds=45,   # fetches external news + runs LLM summary
        retry_attempts=2,
    ),
    CommandSpec(
        command="/design",
        args="smoke test feature — add a hello world endpoint",
        pattern=re.compile(
            r"Design Proposal|proposal|Overview|Files to change|Constraints|Summary",
            re.IGNORECASE,
        ),
        description="/design generates a structured design proposal",
        timeout_seconds=90,   # cold Claude API can be slow
        retry_attempts=2,
        # Cleanup: /reject resets supervisor to IDLE after the proposal is generated.
        # Without this the bot stays in AWAITING_APPROVAL and blocks the next /design.
        cleanup_command="/reject smoke-test cleanup",
        cleanup_pattern=re.compile(r"Reset to IDLE|Nothing in progress", re.IGNORECASE),
    ),
]


def get_testable_commands() -> list[CommandSpec]:
    """Return only commands that are not in the skip list."""
    return [
        spec for spec in COMMAND_REGISTRY
        if spec.command not in SKIP_COMMANDS
    ]
