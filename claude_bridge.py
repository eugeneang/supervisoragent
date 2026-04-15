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
import json
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
            "**Constraints respected:**\n"
            "- list key constraints\n\n"
            "Keep the proposal under 300 words. Be concrete and specific."
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
            "HARD CONSTRAINTS (never violate these):\n"
            f"{constraints_text}\n\n"
            "OUTPUT FORMAT:\n"
            "Respond with ONLY a valid JSON array — no prose, no markdown fences, no explanation.\n"
            'Schema: [{"path": "relative/path/from/repo/root.py", "content": "full file content"}]\n\n'
            "Rules:\n"
            "- Include the COMPLETE file content for every file you create or modify.\n"
            "- Only include files that actually need to change.\n"
            "- Paths are relative to the repo root (e.g. telegram_bot.py, not /abs/path).\n"
            "- Do not output anything outside the JSON array.\n"
            "- If no changes are needed, output an empty array: []"
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
                    max_tokens=8192,
                    system=[{
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_content}],
                ),
                timeout=BUILD_TIMEOUT,
            )
            raw = response.content[0].text.strip()
            edits = self._parse_edits(raw)
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

    def _gather_context(self) -> str:
        """Read key repo files to give Claude implementation context."""
        include = ["telegram_bot.py", "requirements.txt"]
        parts = []
        for rel in include:
            p = REPO_ROOT / rel
            if p.exists():
                parts.append(f"=== {rel} ===\n{p.read_text(encoding='utf-8')}")
        return "\n\n".join(parts)

    def _parse_edits(self, raw: str) -> list[dict]:
        """Parse JSON edits from Claude's response, stripping accidental fences."""
        text = raw
        if text.startswith("```"):
            lines = text.splitlines()
            # Drop opening fence line and closing fence if present
            start = 1
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            text = "\n".join(lines[start:end])
        return json.loads(text)

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
