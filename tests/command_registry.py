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
        command="/start",
        args="",
        pattern=re.compile(r"hello", re.IGNORECASE),
        description="Bot greeting on /start",
    ),
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
        command="/build_status",
        args="",
        pattern=re.compile(r"state|IDLE|build|status", re.IGNORECASE),
        description="Build status shows current supervisor state",
    ),
    CommandSpec(
        command="/design",
        args="smoke test feature — add a hello world endpoint",
        pattern=re.compile(
            r"Design Proposal|proposal|Overview|Files to change|Constraints|Summary",
            re.IGNORECASE,
        ),
        description="/design generates a structured design proposal",
        timeout_seconds=60,   # LLM generation can take longer
        retry_attempts=2,
    ),
]


def get_testable_commands() -> list[CommandSpec]:
    """Return only commands that are not in the skip list."""
    return [
        spec for spec in COMMAND_REGISTRY
        if spec.command not in SKIP_COMMANDS
    ]
