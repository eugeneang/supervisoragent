"""
claude_bridge.py — Anthropic SDK wrapper for the supervisor loop.

Provides two operations:
  1. generate_proposal(request_text)  →  ProposalResult
  2. execute_build(build_request)     →  BuildResult

Uses claude-sonnet-4-6 for both. System prompts use prompt caching
(cache_control: ephemeral) since they are long and repeated.

All file writes are validated to stay inside REPO_ROOT and never touch
the protected file list.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
REPO_ROOT = Path(__file__).parent.resolve()

# Timeout constants (seconds)
DESIGN_TIMEOUT = 120.0   # max time for proposal generation
BUILD_TIMEOUT = 300.0    # max time for code generation + file writes
# Retry once for transient upstream Anthropic/API failures.
_RETRYABLE_STATUS = frozenset({500, 502, 503, 529})
_RETRY_DELAY = 2.0

# Files the build step must never overwrite
_PROTECTED_FILES = frozenset({
    "ai_news_push.py",
    "health_monitor.py",
    "smart_commit.py",
    "local_chat.py",
    "whatsapp_app.py",
    "agents/ai_news_agent.py",
    "claude_bridge.py",
    "supervisor_loop.py",
})
_PROTECTED_PREFIXES = (".github/", "launchd/")


def _extract_status_code(exc: Exception) -> int | None:
    """
    Best-effort extraction of HTTP status code from Anthropic/HTTP/client exceptions.
    """
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value

    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            value = getattr(response, attr, None)
            if isinstance(value, int):
                return value

    text = str(exc)
    match = re.search(r"\b(500|502|503|529)\b", text)
    if match:
        return int(match.group(1))

    return None


def _extract_request_id(exc: Exception) -> str | None:
    """
    Best-effort extraction of Anthropic request_id from exception fields or message text.
    """
    for attr in ("request_id",):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    response = getattr(exc, "response", None)
    if response is not None:
        # Some SDKs may expose headers or request IDs through response objects.
        headers = getattr(response, "headers", None)
        if headers:
            for key in ("request-id", "x-request-id", "anthropic-request-id"):
                value = headers.get(key) or headers.get(key.upper())
                if value:
                    return str(value).strip()

        for attr in ("request_id",):
            value = getattr(response, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()

    text = str(exc)
    match = re.search(r"request_id[\"'=:\s]+([A-Za-z0-9_\-]+)", text)
    if match:
        return match.group(1).strip()

    # Matches things like req_011Ca5kU4hnjG1ZxKf3o1YXy
    match = re.search(r"\b(req_[A-Za-z0-9]+)\b", text)
    if match:
        return match.group(1).strip()

    return None


def _is_retryable_anthropic_error(exc: Exception) -> bool:
    status = _extract_status_code(exc)
    return status in _RETRYABLE_STATUS


def _format_api_error(exc: Exception) -> str:
    """
    Build a user-visible error string preserving request ID and status code when available.
    """
    status = _extract_status_code(exc)
    request_id = _extract_request_id(exc)
    message = str(exc).strip() or exc.__class__.__name__

    parts = ["Anthropic API error"]
    if status is not None:
        parts.append(f"status={status}")
    if request_id:
        parts.append(f"request_id={request_id}")

    prefix = " ".join(parts)
    return f"{prefix}: {message}"


def _call_api(fn, *args, **kwargs):
    """
    Execute an Anthropic API call with one retry for transient upstream failures.
    Intended to run inside asyncio.to_thread(...), so blocking sleep is acceptable.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if not _is_retryable_anthropic_error(exc):
            raise

        status = _extract_status_code(exc)
        request_id = _extract_request_id(exc)
        logging.warning(
            "Anthropic API transient failure on first attempt; retrying once. "
            "status=%s request_id=%s error=%s",
            status,
            request_id,
            exc,
        )

        time.sleep(_RETRY_DELAY)

        try:
            return fn(*args, **kwargs)
        except Exception as retry_exc:
            retry_status = _extract_status_code(retry_exc)
            retry_request_id = _extract_request_id(retry_exc)
            logging.error(
                "Anthropic API failed after retry. "
                "status=%s request_id=%s error=%s",
                retry_status,
                retry_request_id,
                retry_exc,
            )
            raise RuntimeError(_format_api_error(retry_exc)) from retry_exc

@dataclass
class ProposalResult:
    feature_name: str
    proposal_text: str
    error: str | None = None


@dataclass
class BuildResult:
    success: bool
    changed_files: list = field(default_factory=list)
    summary: str = ""
    error: str | None = None


class ClaudeBridge:
    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to ai_news_push.env and restart."
            )
        self.client = anthropic.Anthropic(api_key=api_key)

    # ── Design proposal ──────────────────────────────────────────────────────

    async def generate_proposal(self, request_text: str) -> ProposalResult:
        system = (
            "You are a software architect. Produce a concise design proposal for the requested feature.\n\n"
            "Format your response exactly as follows (include the FEATURE_NAME line):\n\n"
            "FEATURE_NAME: <short_slug_no_spaces_or_hyphens>\n\n"
            "## Design Proposal: <human-readable title>\n\n"
            "**Summary:** one or two sentences.\n\n"
            "**Files to change:**\n"
            "- filename.py — what changes\n\n"
            "**Automated tests to add:**\n"
            "- tests/telegram_smoke_tester.py — add TestSpec for /<new_command> "
            "with a pattern matching the expected response text\n\n"
            "**Constraints respected:**\n"
            "- list key constraints\n\n"
            "Keep the proposal under 400 words. Be concrete and specific.\n\n"
            "IMPORTANT: Every proposal MUST include an 'Automated tests to add' section. "
            "If the feature adds or modifies a Telegram command, specify the TestSpec "
            "entry (command, description, handler_attr, pattern) to add to "
            "tests/telegram_smoke_tester.py. If it changes existing behaviour, update "
            "the matching pattern."
        )
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.messages.create,
                    model=MODEL,
                    max_tokens=1024,
                    system=[{
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": request_text}],
                ),
                timeout=DESIGN_TIMEOUT,
            )
            raw = response.content[0].text.strip()
            feature_name, proposal_text = self._parse_proposal(raw, request_text)
            return ProposalResult(feature_name=feature_name, proposal_text=proposal_text)
        except asyncio.TimeoutError:
            logger.error("generate_proposal timed out after %.0fs", DESIGN_TIMEOUT)
            return ProposalResult(
                feature_name="unknown",
                proposal_text="",
                error=f"Design generation timed out after {DESIGN_TIMEOUT:.0f}s",
            )
        except Exception as e:
            logger.exception("generate_proposal failed")
            return ProposalResult(feature_name="unknown", proposal_text="", error=str(e))

    def _parse_proposal(self, raw: str, fallback_request: str) -> tuple[str, str]:
        feature_name = ""
        body_lines = []
        for line in raw.splitlines():
            if line.startswith("FEATURE_NAME:"):
                feature_name = line.split(":", 1)[1].strip().lower().replace(" ", "_").replace("-", "_")
            else:
                body_lines.append(line)
        proposal_text = "\n".join(body_lines).strip() or raw
        if not feature_name:
            words = [w.strip(".,!?") for w in fallback_request.lower().split()[:4] if w.isalpha()]
            feature_name = "_".join(words)[:30] or "feature"
        return feature_name, proposal_text

    # ── Build execution ──────────────────────────────────────────────────────

    async def execute_build(self, build_request: dict) -> BuildResult:
        context = self._gather_context()
        constraints_text = "\n".join(f"- {c}" for c in build_request.get("constraints", []))

        system = (
            "You are an expert Python engineer implementing a feature in an existing codebase.\n\n"
            "SYSTEM FACTS (always true — never contradict these):\n"
            "- macOS with launchd for service management (NOT supervisord/supervisorctl)\n"
            "- Services managed via launchctl and plists in ~/Library/LaunchAgents/\n"
            "- Relevant launchd labels: com.eugene.telegram_bot, com.eugene.ollama, "
            "com.eugene.smoke_tests, com.eugene.ai_news_push, com.eugene.health_monitor\n"
            "- Smoke test log: /Users/eugene/Agents/smoke_tests.log\n"
            "- Repo root: /Users/eugene/Agents/supervisoragent/\n"
            "- Python venv: /Users/eugene/Agents/supervisoragent/.venv/bin/python\n"
            "- Bot runs as a single telegram_bot.py process (not a worker cluster)\n"
            "- Config values (paths, env vars) should be derived from config.py in repo root\n\n"
            "HARD CONSTRAINTS (never violate these):\n"
            f"{constraints_text}\n\n"
            "AUTOMATED TESTING REQUIREMENT:\n"
            "Every build that adds or changes a Telegram command MUST also update tests:\n"
            "  1. If adding a new command: register CommandHandler in telegram_bot.py's main(),\n"
            "     AND add a TestSpec entry to the _TEST_SPECS list in tests/telegram_smoke_tester.py.\n"
            "  2. If changing existing command behaviour: update the matching TestSpec pattern.\n"
            "Use the exact TestSpec dataclass format already used in tests/telegram_smoke_tester.py.\n\n"
            "OUTPUT FORMAT:\n"
            "Use ONLY the file-block format below — no JSON, no markdown fences, no prose.\n"
            "Each file you create or modify must appear as one block:\n\n"
            "=== FILE: relative/path/from/repo/root.py ===\n"
            "<complete file content — every line, exactly as it should be written to disk>\n"
            "=== END FILE ===\n\n"
            "Rules:\n"
            "- Include the COMPLETE file content for every file you create or modify.\n"
            "- Only include files that actually need to change.\n"
            "- Paths are relative to the repo root (e.g. telegram_bot.py, not /abs/path).\n"
            "- Do not output ANYTHING outside the file blocks (no explanations, no comments).\n"
            "- If no changes are needed, output nothing at all.\n"
            "- NEVER nest or escape the sentinel lines — they will be parsed literally."
        )

        user_content = (
            f"Feature request:\n{build_request['request_text']}\n\n"
            f"Design proposal:\n{build_request['proposal_text']}\n\n"
            f"Current repo files for context:\n\n{context}"
        )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.messages.create,
                    model=MODEL,
                    max_tokens=16384,
                    system=[{
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_content}],
                ),
                timeout=BUILD_TIMEOUT,
            )
            logger.info(
                "execute_build API response: stop_reason=%s usage=%s",
                response.stop_reason,
                getattr(response, "usage", None),
            )
            if response.stop_reason == "max_tokens":
                return BuildResult(
                    success=False,
                    error=(
                        "Claude's response was truncated (hit the output token limit). "
                        "No files were written. Try splitting the request into a smaller "
                        "feature scope and run /design again."
                    ),
                )
            raw = response.content[0].text
            edits = self._parse_file_blocks(raw)
            if not edits and raw.strip():
                logger.warning(
                    "execute_build: response was non-empty (%d chars) but no file blocks "
                    "were parsed. Claude may have used the wrong output format. "
                    "First 200 chars: %r",
                    len(raw),
                    raw[:200],
                )
                return BuildResult(
                    success=False,
                    error=(
                        "Claude did not follow the file-block output format. "
                        "No files were written. Please try /design again."
                    ),
                )
            changed = self._apply_edits(edits)
            return BuildResult(
                success=True,
                changed_files=changed,
                summary=f"Applied {len(changed)} file edit(s).",
            )
        except asyncio.TimeoutError:
            logger.error("execute_build timed out after %.0fs", BUILD_TIMEOUT)
            return BuildResult(success=False, error=f"Build timed out after {BUILD_TIMEOUT:.0f}s")
        except Exception as e:
            logger.exception("execute_build failed")
            return BuildResult(success=False, error=str(e))

    # ── Context helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_section(text: str, start_marker: str, end_marker: str) -> str:
        """Return the text between two markers (inclusive), or '' if not found."""
        s = text.find(start_marker)
        if s == -1:
            return ""
        e = text.find(end_marker, s)
        return text[s: e + len(end_marker)] if e != -1 else text[s:]

    def _focused_telegram_bot_context(self) -> str:
        """
        Return only the parts of telegram_bot.py that Claude needs to implement
        a new command:
          • imports block (to know existing imports and avoid duplicates)
          • main() function (to know where and how to register CommandHandlers)

        Passing the full 500-line file wastes ~5K tokens on handler implementations
        that Claude doesn't need to read or reproduce.
        """
        p = REPO_ROOT / "telegram_bot.py"
        if not p.exists():
            return ""
        src = p.read_text(encoding="utf-8")
        lines = src.splitlines()

        # Imports block: everything up to (and including) the first non-import line
        # that follows a blank line after imports.
        import_lines = []
        for line in lines:
            import_lines.append(line)
            # Stop after the supervisor = SupervisorLoop() instantiation
            if line.strip().startswith("supervisor = SupervisorLoop()"):
                break

        # main() function: from 'def main():' to end of file
        main_lines: list[str] = []
        in_main = False
        for line in lines:
            if line.startswith("def main():"):
                in_main = True
            if in_main:
                main_lines.append(line)

        parts = []
        if import_lines:
            parts.append("# --- imports + module-level setup ---\n" + "\n".join(import_lines))
        if main_lines:
            parts.append("# --- main() handler registration ---\n" + "\n".join(main_lines))

        return "\n\n".join(parts)

    def _focused_smoke_tester_context(self) -> str:
        """
        Return only the TestSpec dataclass definition and _TEST_SPECS list from
        tests/telegram_smoke_tester.py.  Claude only needs these to add a new
        TestSpec entry; it doesn't need the runner, mock helpers, or entry point.
        """
        p = REPO_ROOT / "tests" / "telegram_smoke_tester.py"
        if not p.exists():
            return ""
        src = p.read_text(encoding="utf-8")

        # Extract @dataclass class TestSpec through the closing ']' of _TEST_SPECS
        start = src.find("@dataclass\nclass TestSpec:")
        if start == -1:
            start = src.find("class TestSpec:")
        end = src.find("\n\n\n", src.find("_TEST_SPECS"))  # triple-newline ends the list block
        if start == -1 or end == -1:
            return src  # fall back to full file if markers not found
        return src[start:end].strip()

    def _gather_context(self) -> str:
        """
        Assemble focused context for the build prompt.

        Provides only the information Claude needs:
          - telegram_bot.py: imports block + main() only (~60 lines vs 530)
          - requirements.txt: full (short)
          - tests/telegram_smoke_tester.py: TestSpec class + _TEST_SPECS only (~80 lines vs 345)
          - commands/ directory listing: so Claude knows what command modules exist

        NOTE: context headers use '--- CONTEXT: path ---' (NOT '=== FILE: path ===')
        to avoid Claude confusing input context with output file-block sentinels.
        """
        parts: list[str] = []

        # 1. Focused telegram_bot.py
        tb_ctx = self._focused_telegram_bot_context()
        if tb_ctx:
            parts.append(f"--- CONTEXT: telegram_bot.py (focused: imports + main) ---\n{tb_ctx}\n--- END CONTEXT ---")

        # 2. requirements.txt (short — always include fully)
        req = REPO_ROOT / "requirements.txt"
        if req.exists():
            parts.append(f"--- CONTEXT: requirements.txt ---\n{req.read_text(encoding='utf-8')}\n--- END CONTEXT ---")

        # 3. Focused smoke tester — TestSpec class + _TEST_SPECS list only
        st_ctx = self._focused_smoke_tester_context()
        if st_ctx:
            parts.append(f"--- CONTEXT: tests/telegram_smoke_tester.py (TestSpec + _TEST_SPECS only) ---\n{st_ctx}\n--- END CONTEXT ---")

        # 4. commands/ directory listing so Claude knows existing modules
        cmds_dir = REPO_ROOT / "commands"
        if cmds_dir.is_dir():
            cmd_files = sorted(p.name for p in cmds_dir.iterdir() if p.suffix == ".py")
            listing = "\n".join(f"  commands/{f}" for f in cmd_files)
            parts.append(f"--- CONTEXT: commands/ directory ---\n{listing}\n--- END CONTEXT ---")

        return "\n\n".join(parts)

    # ── Fix proposal (after test failure) ────────────────────────────────────

    async def generate_fix_proposal(
        self,
        failed_tests: list[dict],
        original_request: str,
        original_proposal: str,
        changed_files: list[str],
    ) -> ProposalResult:
        """
        Given a list of failing tests, produce a targeted fix proposal.
        failed_tests: list of {command, detail} dicts from the smoke tester.
        """
        context = self._gather_context()
        failures_text = "\n".join(
            f"  - {r['command']}: {r.get('detail', '')}" for r in failed_tests
        )
        changed_text = "\n".join(f"  - {f}" for f in changed_files) or "  (none)"

        system = (
            "You are a software architect. Automated smoke tests failed after a build. "
            "Analyze the failures and produce a targeted fix proposal.\n\n"
            "Format your response exactly as follows (include the FEATURE_NAME line):\n\n"
            "FEATURE_NAME: <same_slug_or_add_fix_suffix>\n\n"
            "## Fix Proposal: <human-readable title>\n\n"
            "**Root cause:** one sentence.\n\n"
            "**Files to fix:**\n"
            "- filename.py — what specifically needs to change\n\n"
            "**Test corrections needed:**\n"
            "- tests/telegram_smoke_tester.py — describe any TestSpec changes needed, "
            "or 'none' if the test spec is correct and only the implementation needs fixing\n\n"
            "**Constraints respected:**\n"
            "- list key constraints\n\n"
            "Keep the proposal under 300 words. Be concrete and specific."
        )

        user_content = (
            f"Original feature request:\n{original_request}\n\n"
            f"Original design proposal:\n{original_proposal}\n\n"
            f"Files changed by the build:\n{changed_text}\n\n"
            f"Failing tests:\n{failures_text}\n\n"
            f"Current repo files for context:\n\n{context}"
        )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    _call_api,
                    self.client.messages.create,
                    model=MODEL,
                    max_tokens=1024,
                    system=[{
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_content}],
                ),
                timeout=DESIGN_TIMEOUT,
            )
            raw = response.content[0].text.strip()
            feature_name, proposal_text = self._parse_proposal(raw, original_request)
            return ProposalResult(feature_name=feature_name, proposal_text=proposal_text)
        except asyncio.TimeoutError:
            logger.error("generate_fix_proposal timed out after %.0fs", DESIGN_TIMEOUT)
            return ProposalResult(
                feature_name="unknown",
                proposal_text="",
                error=f"Fix proposal timed out after {DESIGN_TIMEOUT:.0f}s",
            )
        except Exception as e:
            logger.exception("generate_fix_proposal failed")
            return ProposalResult(feature_name="unknown", proposal_text="", error=str(e))

    def _parse_file_blocks(self, raw: str) -> list[dict]:
        """
        Parse file blocks from Claude's response.

        Expected format (no JSON, no escaping required):

            === FILE: relative/path.py ===
            <complete file content>
            === END FILE ===

        Returns a list of {"path": ..., "content": ...} dicts compatible with
        _apply_edits.  Raises ValueError if a block is structurally malformed.
        """
        _OPEN = re.compile(r'^=== FILE: (.+?) ===$', re.MULTILINE)
        _CLOSE = '=== END FILE ==='

        edits: list[dict] = []
        for open_match in _OPEN.finditer(raw):
            path = open_match.group(1).strip()
            content_start = open_match.end() + 1  # skip the newline after the header
            close_pos = raw.find(_CLOSE, content_start)
            if close_pos == -1:
                raise ValueError(
                    f"Unterminated file block for '{path}' — "
                    f"missing '=== END FILE ===' sentinel."
                )
            # Preserve trailing newline inside the block but strip the sentinel's newline
            content = raw[content_start:close_pos]
            edits.append({"path": path, "content": content})

        return edits

    def _apply_edits(self, edits: list[dict]) -> list[str]:
        """Write file edits to disk. Raises on any safety violation."""
        changed = []
        for edit in edits:
            rel_path = edit["path"].lstrip("/")
            target = (REPO_ROOT / rel_path).resolve()

            # Must stay inside repo
            if not str(target).startswith(str(REPO_ROOT) + os.sep) and target != REPO_ROOT:
                raise ValueError(f"Path escapes repo root: {rel_path}")

            # Must not touch protected files
            if rel_path in _PROTECTED_FILES:
                raise ValueError(f"Attempt to modify protected file: {rel_path}")
            if any(rel_path.startswith(prefix) for prefix in _PROTECTED_PREFIXES):
                raise ValueError(f"Attempt to modify protected path: {rel_path}")

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit["content"], encoding="utf-8")
            changed.append(rel_path)
            logger.info("Build wrote: %s", rel_path)
        return changed
