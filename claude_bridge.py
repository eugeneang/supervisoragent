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
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
REPO_ROOT = Path(__file__).parent.resolve()

# Timeout constants (seconds)
DESIGN_TIMEOUT = 120.0   # max time for proposal generation
BUILD_TIMEOUT = 300.0    # max time for code generation + file writes

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
