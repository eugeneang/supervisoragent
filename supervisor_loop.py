"""
supervisor_loop.py — State machine for the approval-gated coding loop.

States:
  IDLE → DESIGNING → AWAITING_APPROVAL → BUILDING → DONE → IDLE

State is persisted to supervisor_state.json (gitignored) so it survives
bot restarts. /reject always resets to IDLE from any state.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from claude_bridge import BUILD_TIMEOUT, DESIGN_TIMEOUT, BuildResult, ClaudeBridge

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.resolve()
STATE_FILE = REPO_ROOT / "supervisor_state.json"

# How old a state must be (seconds) before it is considered stale.
STALE_THRESHOLDS_SECONDS: dict[str, int] = {
    "DESIGNING": 600,   # 10 min  (design normally completes in < 2 min)
    "BUILDING": 1800,   # 30 min  (build normally completes in < 5 min)
}

# Constraints passed to the build step — protects all existing workflows.
BUILD_CONSTRAINTS = [
    "Do not modify ai_news_push.py",
    "Do not modify health_monitor.py",
    "Do not modify agents/ai_news_agent.py",
    "Do not modify smart_commit.py",
    "Do not modify local_chat.py",
    "Do not modify whatsapp_app.py",
    "Do not modify claude_bridge.py",
    "Do not modify supervisor_loop.py",
    "Do not modify any file under .github/",
    "Do not modify any launchd plist",
    "Output ONLY a JSON array of file edits, nothing else",
]

_DEFAULT_STATE: dict = {
    "state": "IDLE",
    "requester_chat_id": None,
    "request_text": None,
    "proposal_text": None,
    "feature_name": None,
    "changed_files": [],
    "build_summary": None,
    "error": None,
    "created_at": None,
    "updated_at": None,
}


class SupervisorLoop:
    def __init__(self):
        self._bridge: ClaudeBridge | None = None  # lazy — avoids failing on import
        self._lock = asyncio.Lock()

    def _get_bridge(self) -> ClaudeBridge:
        if self._bridge is None:
            self._bridge = ClaudeBridge()
        return self._bridge

    # ── Stale-state helpers ──────────────────────────────────────────────────

    def _state_age_seconds(self, state: dict) -> float | None:
        ts_str = state.get("updated_at") or state.get("created_at")
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(ts_str)
            return (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:
            return None

    def _is_stale(self, state: dict) -> bool:
        threshold = STALE_THRESHOLDS_SECONDS.get(state.get("state", "IDLE"))
        if threshold is None:
            return False
        age = self._state_age_seconds(state)
        return age is not None and age > threshold

    # ── State persistence ────────────────────────────────────────────────────

    def load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("Could not read state file; resetting to IDLE")
        return dict(_DEFAULT_STATE)

    def save_state(self, state: dict) -> None:
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)

    # ── /design ──────────────────────────────────────────────────────────────

    async def start_design(self, chat_id: int, request_text: str) -> tuple[str | None, str]:
        """
        Kick off a design proposal.

        Returns (proposal_text, status_msg).
        proposal_text is None when the caller should show only the status_msg
        (either an error or a guard rejection).
        """
        async with self._lock:
            state = self.load_state()
            if state["state"] != "IDLE":
                return None, (
                    f"A request is already in progress (state: {state['state']}).\n"
                    "Use /build_status to check, or /reject to reset."
                )
            state.update({
                **_DEFAULT_STATE,
                "state": "DESIGNING",
                "requester_chat_id": chat_id,
                "request_text": request_text,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            self.save_state(state)

        try:
            result = await self._get_bridge().generate_proposal(request_text)
        except Exception as e:
            # Belt-and-suspenders: generate_proposal should not raise, but if it
            # does we must reset state so the machine doesn't stay stuck.
            err = str(e)
            logger.exception("Unexpected exception escaping generate_proposal")
            async with self._lock:
                s = self.load_state()
                s.update({"state": "IDLE", "error": err})
                self.save_state(s)
            return None, f"Design failed (unexpected error):\n{err}"

        async with self._lock:
            state = self.load_state()
            if result.error:
                state.update({"state": "IDLE", "error": result.error})
                self.save_state(state)
                return None, f"Failed to generate proposal:\n{result.error}"
            state.update({
                "state": "AWAITING_APPROVAL",
                "proposal_text": result.proposal_text,
                "feature_name": result.feature_name,
            })
            self.save_state(state)
            return result.proposal_text, "Proposal ready."

    # ── /approve ─────────────────────────────────────────────────────────────

    async def approve(self, chat_id: int, notify_callback) -> str:
        """
        Start the build in an asyncio background task.
        notify_callback(msg: str) is awaited when the build finishes.
        Returns an immediate acknowledgement string.
        """
        async with self._lock:
            state = self.load_state()
            if state["state"] != "AWAITING_APPROVAL":
                return f"Nothing to approve right now (state: {state['state']})."
            state["state"] = "BUILDING"
            self.save_state(state)
            build_request = {
                "feature_name": state["feature_name"],
                "request_text": state["request_text"],
                "proposal_text": state["proposal_text"],
                "repo_path": str(REPO_ROOT),
                "constraints": BUILD_CONSTRAINTS,
            }

        asyncio.create_task(self._run_build(build_request, notify_callback))
        return "Build started... Claude is writing code. This may take 30–60s."

    async def _run_build(self, build_request: dict, notify_callback) -> None:
        try:
            result: BuildResult = await self._get_bridge().execute_build(build_request)
        except Exception as e:
            logger.exception("Unexpected exception escaping execute_build")
            result = BuildResult(success=False, error=f"Unexpected error: {e}")

        async with self._lock:
            state = self.load_state()
            if result.success:
                state.update({
                    "state": "DONE",
                    "changed_files": result.changed_files,
                    "build_summary": result.summary,
                    "error": None,
                })
            else:
                state.update({
                    "state": "DONE",
                    "changed_files": [],
                    "build_summary": None,
                    "error": result.error,
                })
            self.save_state(state)

        if result.success:
            files = "\n".join(f"  • {f}" for f in result.changed_files) or "  (no files changed)"
            diff_targets = " ".join(result.changed_files)
            msg = (
                "Build complete ✅\n\n"
                f"Changed files:\n{files}\n\n"
                "Review before committing:\n"
                f"  git diff {diff_targets}"
            )
        else:
            msg = f"Build failed ❌\n\nError: {result.error}"

        try:
            await notify_callback(msg)
        except Exception:
            logger.exception("notify_callback failed after build")

    # ── /reset_build ─────────────────────────────────────────────────────────

    def force_reset(self) -> str:
        """Force-reset to IDLE from any state, including stuck DESIGNING/BUILDING."""
        state = self.load_state()
        prev = state.get("state", "IDLE")
        new_state = dict(_DEFAULT_STATE)
        self.save_state(new_state)
        return f"Force-reset from {prev} → IDLE.\nSend /design <request> to start over."

    # ── /reject ──────────────────────────────────────────────────────────────

    def reject(self, reason: str = "") -> str:
        """Reset to IDLE from any state."""
        state = self.load_state()
        prev = state.get("state", "IDLE")
        new_state = dict(_DEFAULT_STATE)
        self.save_state(new_state)
        if prev == "IDLE":
            return "Nothing in progress. Send /design <request> to start."
        suffix = f"\nReason: {reason}" if reason.strip() else ""
        return f"Reset to IDLE.{suffix}\n\nSend /design <request> to start over."

    # ── /build_status ────────────────────────────────────────────────────────

    def get_status(self) -> str:
        state = self.load_state()
        s = state.get("state", "IDLE")
        if s == "IDLE":
            return "State: IDLE\nNo active request. Send /design <request> to begin."

        lines = [f"State: {s}"]

        # Age and staleness
        age = self._state_age_seconds(state)
        if age is not None:
            mins, secs = divmod(int(age), 60)
            age_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            stale = self._is_stale(state)
            stale_tag = "  ⚠️ STALE" if stale else ""
            lines.append(f"Age: {age_str}{stale_tag}")
            if stale:
                lines.append("Use /reset_build to force-reset to IDLE.")

        if state.get("feature_name"):
            lines.append(f"Feature: {state['feature_name']}")
        if state.get("request_text"):
            snippet = state["request_text"][:100]
            if len(state["request_text"]) > 100:
                snippet += "..."
            lines.append(f"Request: {snippet}")
        if state.get("changed_files"):
            lines.append("Changed: " + ", ".join(state["changed_files"]))
        if state.get("build_summary"):
            lines.append(f"Build: {state['build_summary']}")
        if state.get("error"):
            lines.append(f"Error: {state['error']}")
        if state.get("updated_at"):
            lines.append(f"Updated: {state['updated_at']}")
        return "\n".join(lines)
