"""
supervisor_loop.py — State machine for the approval-gated coding loop.

States:
  IDLE → DESIGNING → AWAITING_APPROVAL → BUILDING → AWAITING_COMMIT_APPROVAL
                                                    ↓ approve_commit          ↓ reject_commit
                                                 COMMITTING               IDLE (files rolled back)
                                                    ↓
                                                  DONE → IDLE (on next /design)

  Failed build:   BUILDING → DONE (error preserved for /build_status)
  /reject:        any state → IDLE (rolls back files when in AWAITING_COMMIT_APPROVAL)
  /reset_build:   any state → IDLE (same rollback behaviour)

State is persisted to supervisor_state.json (gitignored) so it survives bot restarts.
All user-facing timestamps are displayed in SGT (GMT+8); internal storage is UTC.
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from claude_bridge import BUILD_TIMEOUT, DESIGN_TIMEOUT, BuildResult, ClaudeBridge

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.resolve()
STATE_FILE = REPO_ROOT / "supervisor_state.json"

# All user-facing timestamps are displayed in SGT (GMT+8).
# Internal storage remains UTC ISO strings.
_TZ_DISPLAY = ZoneInfo("Asia/Singapore")

# launchd plist that owns the supervisor/bot process.
_SUPERVISOR_PLIST = Path.home() / "Library/LaunchAgents/com.eugene.supervisor.plist"

# How old a state must be (seconds) before it is considered stale.
STALE_THRESHOLDS_SECONDS: dict[str, int] = {
    "DESIGNING": 600,                # 10 min
    "BUILDING": 1800,                # 30 min
    "AWAITING_COMMIT_APPROVAL": 3600,  # 1 hr  (user may be away)
    "COMMITTING": 120,               # 2 min  (git ops are fast)
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
    "proposed_commit_msg": None,
    "pre_build_head": None,
    "commit_hash": None,
    "pushed_branch": None,
    "build_summary": None,
    "error": None,
    "created_at": None,
    "updated_at": None,
}

# States that block a new /design request.
# DONE is intentionally absent — a completed job must not block the next request.
_ACTIVE_STATES = frozenset({
    "DESIGNING",
    "AWAITING_APPROVAL",
    "BUILDING",
    "AWAITING_COMMIT_APPROVAL",
    "COMMITTING",
})


class SupervisorLoop:
    def __init__(self):
        self._bridge: ClaudeBridge | None = None  # lazy — avoids failing on import
        self._lock = asyncio.Lock()

    def _get_bridge(self) -> ClaudeBridge:
        if self._bridge is None:
            self._bridge = ClaudeBridge()
        return self._bridge

    # ── Timezone display ─────────────────────────────────────────────────────

    @staticmethod
    def _fmt_ts(ts_str: str | None) -> str:
        """Parse a UTC ISO timestamp and return it formatted in SGT (GMT+8)."""
        if not ts_str:
            return "—"
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_TZ_DISPLAY).strftime("%Y-%m-%d %H:%M SGT")
        except Exception:
            return ts_str  # fall back to raw string rather than crashing

    # ── Git helpers ──────────────────────────────────────────────────────────

    def _git(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """Run a git command in REPO_ROOT, inheriting the full process environment."""
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
        )

    def _current_head(self) -> str | None:
        r = self._git(["git", "rev-parse", "HEAD"])
        return r.stdout.strip() if r.returncode == 0 else None

    def _current_branch(self) -> str:
        r = self._git(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        return r.stdout.strip() if r.returncode == 0 else "unknown"

    def _rollback_build_files(self, changed_files: list[str]) -> tuple[bool, str]:
        """
        Revert working-tree changes written by the build step.

        Rollback strategy:
          • Files tracked in git at HEAD  →  git restore <file>  (reverts to HEAD)
          • New files not yet in git      →  delete from disk
        This only touches files the build wrote — unrelated working-tree changes
        are not affected.
        """
        if not changed_files:
            return True, "No files to roll back."

        errors: list[str] = []
        for rel_path in changed_files:
            # Check whether the file was tracked at HEAD before the build touched it
            tracked = self._git(["git", "ls-files", "--error-unmatch", rel_path])
            if tracked.returncode == 0:
                res = self._git(["git", "restore", rel_path])
                if res.returncode != 0:
                    errors.append(f"restore {rel_path}: {res.stderr.strip()}")
            else:
                # New file created by the build — remove it
                try:
                    (REPO_ROOT / rel_path).unlink(missing_ok=True)
                except Exception as exc:
                    errors.append(f"delete {rel_path}: {exc}")

        return (False, "\n".join(errors)) if errors else (True, "")

    def _do_git_commit(
        self, changed_files: list[str], commit_msg: str
    ) -> tuple[bool, str]:
        """
        Stage → commit → push changed_files.

        Returns (True, "<hash> <branch>") on full success.
        Returns (False, "<error message>")  on any failure.
        If push fails the local commit is reverted (git reset --soft HEAD~1) so
        the working tree is preserved for inspection or a manual retry.
        """
        # 1. Verify there are actual changes to stage
        files_arg = (["--"] + changed_files) if changed_files else []
        status = self._git(["git", "status", "--porcelain"] + files_arg)
        if not status.stdout.strip():
            return False, "No changes detected in the built files — nothing to commit."

        # 2. Stage
        stage_cmd = (["git", "add", "--"] + changed_files) if changed_files else ["git", "add", "-A"]
        add = self._git(stage_cmd)
        if add.returncode != 0:
            return False, f"git add failed:\n{add.stderr.strip() or add.stdout.strip()}"

        # 3. Commit
        commit = self._git(["git", "commit", "-m", commit_msg])
        if commit.returncode != 0:
            return False, f"git commit failed:\n{commit.stderr.strip() or commit.stdout.strip()}"

        commit_hash = self._git(["git", "rev-parse", "--short", "HEAD"]).stdout.strip() or "unknown"
        branch = self._current_branch()

        # 4. Push
        push = self._git(["git", "push", "origin", branch])
        if push.returncode != 0:
            # Keep working-tree changes but undo the local commit
            self._git(["git", "reset", "--soft", "HEAD~1"])
            err = push.stderr.strip() or push.stdout.strip()
            return False, (
                f"git push failed (local commit reverted; build files still on disk):\n{err}"
            )

        return True, f"{commit_hash} {branch}"

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
        proposal_text is None when the caller should show only the status_msg.
        """
        async with self._lock:
            state = self.load_state()
            if state["state"] in _ACTIVE_STATES:
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
        notify_callback(msg) is called when the build finishes.
        Returns an immediate acknowledgement string.
        """
        async with self._lock:
            state = self.load_state()
            if state["state"] != "AWAITING_APPROVAL":
                return f"Nothing to approve right now (state: {state['state']})."
            # Snapshot HEAD so rollback has a reference point even if git restore suffices
            state["pre_build_head"] = self._current_head()
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

    # ── Build runner ─────────────────────────────────────────────────────────

    async def _run_build(self, build_request: dict, notify_callback) -> None:
        try:
            result: BuildResult = await self._get_bridge().execute_build(build_request)
        except Exception as e:
            logger.exception("Unexpected exception escaping execute_build")
            result = BuildResult(success=False, error=f"Unexpected error: {e}")

        proposed_commit_msg = ""
        async with self._lock:
            state = self.load_state()
            if result.success:
                feature_name = state.get("feature_name") or "feature"
                files_list = "\n".join(f"  - {f}" for f in result.changed_files) or "  (none)"
                proposed_commit_msg = (
                    f"feat({feature_name}): implement {feature_name.replace('_', ' ')}\n\n"
                    f"Files changed:\n{files_list}"
                )
                state.update({
                    "state": "AWAITING_COMMIT_APPROVAL",
                    "changed_files": result.changed_files,
                    "build_summary": result.summary,
                    "proposed_commit_msg": proposed_commit_msg,
                    "error": None,
                })
            else:
                # Failed build → DONE so /build_status shows the error,
                # but DONE is not in _ACTIVE_STATES so the next /design is not blocked.
                state.update({
                    "state": "DONE",
                    "changed_files": [],
                    "build_summary": None,
                    "error": result.error,
                })
            self.save_state(state)

        if result.success:
            files = "\n".join(f"  • {f}" for f in result.changed_files) or "  (no files changed)"
            msg = (
                "Build complete ✅\n\n"
                f"Changed files:\n{files}\n\n"
                f"Proposed commit message:\n{proposed_commit_msg}\n\n"
                "Approve to commit & push, or reject to roll back."
            )
        else:
            msg = f"Build failed ❌\n\nError: {result.error}"

        try:
            await notify_callback(msg)
        except Exception:
            logger.exception("notify_callback failed after build")

    # ── Commit approval ──────────────────────────────────────────────────────

    async def approve_commit(self, notify_callback) -> str:
        """
        Kick off the commit/push in a background task.
        Returns an immediate acknowledgement string.
        """
        async with self._lock:
            state = self.load_state()
            if state["state"] != "AWAITING_COMMIT_APPROVAL":
                return f"Nothing to commit right now (state: {state['state']})."
            changed_files = list(state.get("changed_files") or [])
            commit_msg = state.get("proposed_commit_msg") or "feat: supervisor bot build"
            state["state"] = "COMMITTING"
            self.save_state(state)

        asyncio.create_task(self._run_commit(changed_files, commit_msg, notify_callback))
        return "Committing and pushing... ⏳"

    async def _run_commit(
        self, changed_files: list[str], commit_msg: str, notify_callback
    ) -> None:
        success, info = await asyncio.to_thread(
            self._do_git_commit, changed_files, commit_msg
        )

        async with self._lock:
            state = self.load_state()
            if success:
                parts = info.split(" ", 1)
                commit_hash = parts[0]
                branch = parts[1] if len(parts) > 1 else "unknown"
                state.update({
                    "state": "DONE",
                    "commit_hash": commit_hash,
                    "pushed_branch": branch,
                    "error": None,
                })
            else:
                state.update({"state": "DONE", "error": info})
            self.save_state(state)

        if success:
            parts = info.split(" ", 1)
            commit_hash, branch = parts[0], (parts[1] if len(parts) > 1 else "unknown")
            files_str = ", ".join(changed_files) or "(none)"
            msg = (
                "✅ Committed and pushed!\n\n"
                f"Commit: {commit_hash}\n"
                f"Branch: {branch}\n"
                f"Files:  {files_str}\n\n"
                "🔄 Restarting supervisor service in ~3 seconds...\n"
                "The bot will reconnect automatically."
            )
            try:
                await notify_callback(msg)
            except Exception:
                logger.exception("notify_callback failed after commit success")
            self._schedule_restart()
        else:
            msg = (
                "❌ Commit/push failed.\n\n"
                f"{info}\n\n"
                "Build files are preserved on disk.\n"
                "Use the ❌ Rollback button or /reject to clean up."
            )
            try:
                await notify_callback(msg)
            except Exception:
                logger.exception("notify_callback failed after commit failure")

    def _schedule_restart(self) -> None:
        """
        Spawn a detached subprocess that unloads then reloads the launchd service.
        The 3-second sleep ensures the Telegram success message is delivered before
        the current bot process is killed by launchctl unload.
        launchd's KeepAlive will restart the bot after load.
        """
        if not _SUPERVISOR_PLIST.exists():
            logger.warning("Supervisor plist not found at %s — skipping restart", _SUPERVISOR_PLIST)
            return
        cmd = (
            f"sleep 3 "
            f"&& launchctl unload '{_SUPERVISOR_PLIST}' "
            f"&& sleep 1 "
            f"&& launchctl load '{_SUPERVISOR_PLIST}'"
        )
        subprocess.Popen(
            ["bash", "-c", cmd],
            start_new_session=True,  # detach from current process group
            close_fds=True,
        )
        logger.info("Service restart scheduled via detached subprocess (~3s)")

    # ── Commit rejection / rollback ──────────────────────────────────────────

    def reject_commit(self) -> str:
        """Roll back build files and reset to IDLE."""
        state = self.load_state()
        if state["state"] != "AWAITING_COMMIT_APPROVAL":
            return f"Nothing to roll back right now (state: {state['state']})."

        changed_files = list(state.get("changed_files") or [])
        ok, err_msg = self._rollback_build_files(changed_files)

        new_state = dict(_DEFAULT_STATE)
        if not ok:
            new_state["error"] = f"Rollback incomplete: {err_msg}"
        self.save_state(new_state)

        rolled = "\n".join(f"  • {f}" for f in changed_files) or "  (none)"
        if ok:
            return (
                "🔄 Build rolled back.\n\n"
                f"Reverted:\n{rolled}\n\n"
                "State reset to IDLE. Send /design <request> to start over."
            )
        return (
            f"⚠️ Rollback had errors:\n{err_msg}\n\n"
            f"Files attempted:\n{rolled}\n\n"
            "State reset to IDLE."
        )

    # ── /reset_build ─────────────────────────────────────────────────────────

    def force_reset(self) -> str:
        """Force-reset to IDLE from any state, including stuck DESIGNING/BUILDING."""
        state = self.load_state()
        prev = state.get("state", "IDLE")
        rollback_note = ""
        if prev == "AWAITING_COMMIT_APPROVAL":
            changed_files = list(state.get("changed_files") or [])
            ok, err_msg = self._rollback_build_files(changed_files)
            rollback_note = (
                "\n✅ Build files rolled back." if ok
                else f"\n⚠️ Rollback errors: {err_msg}"
            )
        new_state = dict(_DEFAULT_STATE)
        self.save_state(new_state)
        return f"Force-reset from {prev} → IDLE.{rollback_note}\nSend /design <request> to start over."

    # ── /reject ──────────────────────────────────────────────────────────────

    def reject(self, reason: str = "") -> str:
        """Reset to IDLE from any state, rolling back build files if needed."""
        state = self.load_state()
        prev = state.get("state", "IDLE")

        rollback_note = ""
        if prev == "AWAITING_COMMIT_APPROVAL":
            changed_files = list(state.get("changed_files") or [])
            ok, err_msg = self._rollback_build_files(changed_files)
            rollback_note = (
                "\n✅ Build files rolled back." if ok
                else f"\n⚠️ Rollback errors: {err_msg}"
            )

        new_state = dict(_DEFAULT_STATE)
        self.save_state(new_state)

        if prev == "IDLE":
            return "Nothing in progress. Send /design <request> to start."
        suffix = f"\nReason: {reason}" if reason.strip() else ""
        return f"Reset to IDLE.{suffix}{rollback_note}\n\nSend /design <request> to start over."

    # ── /build_status ────────────────────────────────────────────────────────

    def get_status(self) -> str:
        state = self.load_state()
        s = state.get("state", "IDLE")
        if s == "IDLE":
            return "State: IDLE\nNo active request. Send /design <request> to begin."

        lines = [f"State: {s}"]

        # Age / staleness
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
        if s == "AWAITING_COMMIT_APPROVAL" and state.get("proposed_commit_msg"):
            first_line = state["proposed_commit_msg"].splitlines()[0]
            lines.append(f"Commit msg: {first_line}")
        if state.get("commit_hash"):
            lines.append(f"Commit: {state['commit_hash']} → {state.get('pushed_branch', '?')}")
        if state.get("error"):
            lines.append(f"Error: {state['error']}")
        if state.get("created_at"):
            lines.append(f"Started: {self._fmt_ts(state['created_at'])}")
        if state.get("updated_at"):
            lines.append(f"Updated: {self._fmt_ts(state['updated_at'])}")
        return "\n".join(lines)
