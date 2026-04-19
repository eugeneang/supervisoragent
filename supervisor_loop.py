"""
supervisor_loop.py — State machine for the approval-gated coding loop.

States:
  IDLE → DESIGNING → AWAITING_APPROVAL → BUILDING → TESTING
                                                    ↓ pass                ↓ fail
                                         AWAITING_COMMIT_APPROVAL   AWAITING_FIX_APPROVAL
                                                    ↓ approve_commit       ↓ approve (fix)
                                                 COMMITTING           BUILDING (fix) → TESTING
                                                    ↓
                                                  DONE → IDLE (on next /design)

  Failed build:            BUILDING → DONE
  Tests pass:              TESTING → AWAITING_COMMIT_APPROVAL
  Tests fail (< max):      TESTING → AWAITING_FIX_APPROVAL  (Claude proposes a fix)
  Fix approved:            AWAITING_FIX_APPROVAL → BUILDING → TESTING  (loop repeats)
  Fix attempts exhausted:  TESTING → DONE  (user must reject or reset)
  /reject:                 any state → IDLE  (rolls back files from AWAITING_COMMIT_APPROVAL
                                              and AWAITING_FIX_APPROVAL)
  /reset_build:            any state → IDLE  (same rollback behaviour)

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
_TZ_DISPLAY = ZoneInfo("Asia/Singapore")

# launchd plist that owns the bot process.
# com.eugene.telegram_bot is the authoritative job: it has WorkingDirectory set
# correctly and is the only service that should start telegram_bot.py.
# (com.eugene.supervisor was the old launcher and must remain unloaded.)
_SUPERVISOR_PLIST = Path.home() / "Library/LaunchAgents/com.eugene.telegram_bot.plist"

# How old a state must be (seconds) before it is considered stale.
STALE_THRESHOLDS_SECONDS: dict[str, int] = {
    "DESIGNING": 600,
    "BUILDING": 1800,
    "TESTING": 300,                    # 5 min is plenty for the in-process suite
    "AWAITING_FIX_APPROVAL": 3600,     # user may be away
    "AWAITING_COMMIT_APPROVAL": 3600,
    "COMMITTING": 120,
}

# Constraints passed to the build step.
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
    # Testing fields
    "test_results": [],          # list of {command, passed, detail} dicts
    "fix_proposal_text": None,   # Claude's fix proposal after test failure
    "fix_attempt": 0,            # number of fix iterations attempted so far
    "created_at": None,
    "updated_at": None,
}

# States that block a new /design request.
# DONE is intentionally absent — a completed job must not block the next request.
_ACTIVE_STATES = frozenset({
    "DESIGNING",
    "AWAITING_APPROVAL",
    "BUILDING",
    "TESTING",
    "AWAITING_FIX_APPROVAL",
    "AWAITING_COMMIT_APPROVAL",
    "COMMITTING",
})

# Maximum automated fix iterations before requiring manual intervention.
MAX_FIX_ATTEMPTS = 2


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
            return ts_str

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

    def _diff_changed_files(self, changed_files: list[str], max_chars: int = 2800) -> str:
        """
        Return a truncated unified diff of the working-tree changes for the
        given files.  Used to show a preview before the user clicks commit.
        max_chars keeps the Telegram message well under the 4096-char limit.
        """
        if not changed_files:
            return ""
        result = self._git(["git", "diff", "--"] + changed_files)
        diff = result.stdout
        if not diff.strip():
            # Files may be untracked (new files) — show a short summary instead
            new_files = [f for f in changed_files if not self._git(["git", "ls-files", "--error-unmatch", f]).returncode == 0]
            if new_files:
                return "New files (no diff — not yet tracked by git):\n" + "\n".join(f"  + {f}" for f in new_files)
            return ""
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n…(truncated, {len(diff) - max_chars} more chars)"
        return diff

    def _rollback_build_files(self, changed_files: list[str]) -> tuple[bool, str]:
        """
        Revert working-tree changes written by the build step.

        Strategy:
          • Files tracked in git at HEAD  →  git restore <file>
          • New files not yet in git      →  delete from disk
        """
        if not changed_files:
            return True, "No files to roll back."

        errors: list[str] = []
        for rel_path in changed_files:
            tracked = self._git(["git", "ls-files", "--error-unmatch", rel_path])
            if tracked.returncode == 0:
                res = self._git(["git", "restore", rel_path])
                if res.returncode != 0:
                    errors.append(f"restore {rel_path}: {res.stderr.strip()}")
            else:
                try:
                    (REPO_ROOT / rel_path).unlink(missing_ok=True)
                except Exception as exc:
                    errors.append(f"delete {rel_path}: {exc}")

        return (False, "\n".join(errors)) if errors else (True, "")

    def _do_git_commit(
        self, changed_files: list[str], commit_msg: str
    ) -> tuple[bool, str]:
        """Stage → commit → push. Returns (True, "<hash> <branch>") or (False, error)."""
        files_arg = (["--"] + changed_files) if changed_files else []
        status = self._git(["git", "status", "--porcelain"] + files_arg)
        if not status.stdout.strip():
            return False, "No changes detected in the built files — nothing to commit."

        stage_cmd = (["git", "add", "--"] + changed_files) if changed_files else ["git", "add", "-A"]
        add = self._git(stage_cmd)
        if add.returncode != 0:
            return False, f"git add failed:\n{add.stderr.strip() or add.stdout.strip()}"

        commit = self._git(["git", "commit", "-m", commit_msg])
        if commit.returncode != 0:
            return False, f"git commit failed:\n{commit.stderr.strip() or commit.stdout.strip()}"

        commit_hash = self._git(["git", "rev-parse", "--short", "HEAD"]).stdout.strip() or "unknown"
        branch = self._current_branch()

        push = self._git(["git", "push", "origin", branch])
        if push.returncode != 0:
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
        Returns (proposal_text, status_msg); proposal_text is None on error.
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

    async def approve(self, chat_id: int, notify_callback, test_callback=None) -> str:
        """
        Start the build (or fix build) in an asyncio background task.

        Handles two states:
          AWAITING_APPROVAL     — initial build from the design proposal
          AWAITING_FIX_APPROVAL — fix build after test failure

        test_callback: async () → (bool, list) — injected by telegram_bot.py.
        When provided, the build pipeline runs the test suite after a successful build
        instead of going directly to AWAITING_COMMIT_APPROVAL.
        """
        async with self._lock:
            state = self.load_state()
            current = state["state"]

            if current == "AWAITING_APPROVAL":
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
                ack = "Build started... Claude is writing code. This may take 30–60s."

            elif current == "AWAITING_FIX_APPROVAL":
                state["state"] = "BUILDING"
                self.save_state(state)
                fix_attempt = state.get("fix_attempt", 1)
                build_request = {
                    "feature_name": state.get("feature_name", "fix"),
                    "request_text": state.get("request_text", ""),
                    "proposal_text": state.get("fix_proposal_text", ""),
                    "repo_path": str(REPO_ROOT),
                    "constraints": BUILD_CONSTRAINTS,
                }
                ack = (
                    f"Applying fix (attempt {fix_attempt}/{MAX_FIX_ATTEMPTS})... "
                    "Claude is rewriting the failing code. This may take 30–60s."
                )

            else:
                return f"Nothing to approve right now (state: {current})."

        asyncio.create_task(self._run_build(build_request, notify_callback, test_callback))
        return ack

    # ── Build runner ─────────────────────────────────────────────────────────

    async def _run_build(self, build_request: dict, notify_callback, test_callback=None) -> None:
        try:
            result: BuildResult = await self._get_bridge().execute_build(build_request)
        except Exception as e:
            logger.exception("Unexpected exception escaping execute_build")
            result = BuildResult(success=False, error=f"Unexpected error: {e}")

        async with self._lock:
            state = self.load_state()
            if result.success:
                # Accumulate changed_files across all build iterations (handles fix loops).
                existing = set(state.get("changed_files") or [])
                all_changed = sorted(existing | set(result.changed_files))
                state.update({
                    "changed_files": all_changed,
                    "build_summary": result.summary,
                    "error": None,
                })
                if test_callback is None:
                    # No test gate — go straight to commit approval.
                    feature_name = state.get("feature_name") or "feature"
                    files_list = "\n".join(f"  - {f}" for f in all_changed) or "  (none)"
                    proposed_commit_msg = (
                        f"feat({feature_name}): implement {feature_name.replace('_', ' ')}\n\n"
                        f"Files changed:\n{files_list}"
                    )
                    state.update({
                        "state": "AWAITING_COMMIT_APPROVAL",
                        "proposed_commit_msg": proposed_commit_msg,
                    })
            else:
                state.update({
                    "state": "DONE",
                    "build_summary": None,
                    "error": result.error,
                })
            self.save_state(state)
            all_changed = list(state.get("changed_files") or [])

        if not result.success:
            try:
                await notify_callback(f"Build failed ❌\n\nError: {result.error}")
            except Exception:
                logger.exception("notify_callback failed after build failure")
            return

        if test_callback is None:
            async with self._lock:
                state = self.load_state()
                proposed_commit_msg = state.get("proposed_commit_msg", "")
            files = "\n".join(f"  • {f}" for f in all_changed) or "  (no files changed)"
            diff_preview = self._diff_changed_files(all_changed)
            diff_section = (
                f"\n\nDiff preview:\n```\n{diff_preview}\n```"
                if diff_preview else ""
            )
            msg = (
                "Build complete ✅\n\n"
                f"Changed files:\n{files}\n\n"
                f"Proposed commit message:\n{proposed_commit_msg}"
                f"{diff_section}\n\n"
                "Approve to commit & push, or reject to roll back."
            )
            try:
                await notify_callback(msg)
            except Exception:
                logger.exception("notify_callback failed after build success")
        else:
            await self._run_tests(notify_callback, test_callback)

    # ── Test runner ──────────────────────────────────────────────────────────

    async def _run_tests(self, notify_callback, test_callback) -> None:
        """Transition to TESTING, run test_callback, then route on pass/fail."""
        async with self._lock:
            state = self.load_state()
            state["state"] = "TESTING"
            self.save_state(state)

        try:
            await notify_callback("🧪 Running automated tests...")
        except Exception:
            logger.exception("notify_callback failed before test run")

        try:
            all_passed, raw_results = await test_callback()
        except Exception as exc:
            logger.exception("test_callback raised an exception")
            all_passed = False
            raw_results = [{"command": "smoke_runner", "passed": False, "detail": str(exc)}]

        # Normalise to plain dicts for state persistence (handles SmokeResult dataclasses).
        result_dicts: list[dict] = []
        for r in raw_results:
            if hasattr(r, "command"):
                result_dicts.append({"command": r.command, "passed": r.passed, "detail": r.detail})
            else:
                result_dicts.append(dict(r))

        async with self._lock:
            state = self.load_state()
            state["test_results"] = result_dicts
            self.save_state(state)

        if all_passed:
            passed_count = len(result_dicts)
            async with self._lock:
                state = self.load_state()
                feature_name = state.get("feature_name") or "feature"
                all_changed = list(state.get("changed_files") or [])
                files_list = "\n".join(f"  - {f}" for f in all_changed) or "  (none)"
                proposed_commit_msg = (
                    f"feat({feature_name}): implement {feature_name.replace('_', ' ')}\n\n"
                    f"Files changed:\n{files_list}"
                )
                state.update({
                    "state": "AWAITING_COMMIT_APPROVAL",
                    "proposed_commit_msg": proposed_commit_msg,
                })
                self.save_state(state)

            files = "\n".join(f"  • {f}" for f in all_changed) or "  (no files changed)"
            diff_preview = self._diff_changed_files(all_changed)
            diff_section = (
                f"\n\nDiff preview:\n```\n{diff_preview}\n```"
                if diff_preview else ""
            )
            msg = (
                f"✅ Build complete — all {passed_count} test(s) passed!\n\n"
                f"Changed files:\n{files}\n\n"
                f"Proposed commit message:\n{proposed_commit_msg}"
                f"{diff_section}\n\n"
                "Approve to commit & push, or reject to roll back."
            )
            try:
                await notify_callback(msg)
            except Exception:
                logger.exception("notify_callback failed after tests passed")
        else:
            await self._handle_test_failure(result_dicts, notify_callback)

    # ── Test failure → fix proposal ──────────────────────────────────────────

    async def _handle_test_failure(self, result_dicts: list[dict], notify_callback) -> None:
        """
        On test failure: generate a Claude fix proposal and transition to
        AWAITING_FIX_APPROVAL.  After MAX_FIX_ATTEMPTS, give up and go to DONE.
        """
        async with self._lock:
            state = self.load_state()
            fix_attempt = state.get("fix_attempt", 0)
            request_text = state.get("request_text", "")
            proposal_text = state.get("proposal_text", "")
            changed_files = list(state.get("changed_files") or [])

        failed = [r for r in result_dicts if not r.get("passed")]
        failures_text = "\n".join(
            f"  ❌ {r['command']}: {r.get('detail', '')}" for r in failed
        )

        if fix_attempt >= MAX_FIX_ATTEMPTS:
            async with self._lock:
                state = self.load_state()
                state.update({
                    "state": "DONE",
                    "error": (
                        f"Tests failed after {fix_attempt} fix attempt(s). "
                        "Manual intervention required."
                    ),
                })
                self.save_state(state)
            try:
                await notify_callback(
                    f"❌ Tests failed after {fix_attempt} fix attempt(s).\n\n"
                    f"Failing tests:\n{failures_text}\n\n"
                    "Use /reject to roll back or /reset_build to start over."
                )
            except Exception:
                logger.exception("notify_callback failed after max fix attempts")
            return

        # Notify user: tests failed, generating a fix proposal.
        try:
            await notify_callback(
                f"❌ {len(failed)}/{len(result_dicts)} test(s) failed.\n\n"
                f"Failing:\n{failures_text}\n\n"
                "Analyzing failures and generating a fix proposal... ⏳"
            )
        except Exception:
            logger.exception("notify_callback failed before fix proposal generation")

        fix_result = await self._get_bridge().generate_fix_proposal(
            failed_tests=failed,
            original_request=request_text,
            original_proposal=proposal_text,
            changed_files=changed_files,
        )

        async with self._lock:
            state = self.load_state()
            if fix_result.error:
                state.update({"state": "DONE", "error": fix_result.error})
                self.save_state(state)
                try:
                    await notify_callback(
                        f"Failed to generate fix proposal:\n{fix_result.error}"
                    )
                except Exception:
                    logger.exception("notify_callback failed after fix proposal error")
                return

            state.update({
                "state": "AWAITING_FIX_APPROVAL",
                "fix_proposal_text": fix_result.proposal_text,
                "fix_attempt": fix_attempt + 1,
            })
            self.save_state(state)
            current_attempt = fix_attempt + 1

        try:
            await notify_callback(
                f"🔍 Fix Proposal (attempt {current_attempt}/{MAX_FIX_ATTEMPTS}):\n\n"
                f"{fix_result.proposal_text}\n\n"
                "Approve to apply the fix, or reject to roll back the entire build."
            )
        except Exception:
            logger.exception("notify_callback failed after fix proposal ready")

    # ── Commit approval ──────────────────────────────────────────────────────

    async def approve_commit(self, notify_callback) -> str:
        """Kick off commit/push in a background task."""
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
        """Spawn a detached subprocess that restarts the launchd service after 3s."""
        if not _SUPERVISOR_PLIST.exists():
            logger.warning(
                "Supervisor plist not found at %s — skipping restart", _SUPERVISOR_PLIST
            )
            return
        cmd = (
            f"sleep 3 "
            f"&& launchctl unload '{_SUPERVISOR_PLIST}' "
            f"&& sleep 1 "
            f"&& launchctl load '{_SUPERVISOR_PLIST}'"
        )
        subprocess.Popen(
            ["bash", "-c", cmd],
            start_new_session=True,
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
        """Force-reset to IDLE from any state, rolling back build files when needed."""
        state = self.load_state()
        prev = state.get("state", "IDLE")
        rollback_note = ""
        if prev in ("AWAITING_COMMIT_APPROVAL", "AWAITING_FIX_APPROVAL"):
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
        """Reset to IDLE from any state, rolling back build files when needed."""
        state = self.load_state()
        prev = state.get("state", "IDLE")

        rollback_note = ""
        if prev in ("AWAITING_COMMIT_APPROVAL", "AWAITING_FIX_APPROVAL"):
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

        # Test results summary
        test_results = state.get("test_results") or []
        if test_results:
            passed = sum(1 for r in test_results if r.get("passed"))
            total = len(test_results)
            lines.append(f"Tests: {passed}/{total} passed")
            for r in test_results:
                if not r.get("passed"):
                    lines.append(f"  ❌ {r['command']}: {r.get('detail', '')[:60]}")

        if s == "AWAITING_COMMIT_APPROVAL" and state.get("proposed_commit_msg"):
            first_line = state["proposed_commit_msg"].splitlines()[0]
            lines.append(f"Commit msg: {first_line}")
        if s == "AWAITING_FIX_APPROVAL":
            fix_attempt = state.get("fix_attempt", 0)
            lines.append(f"Fix attempt: {fix_attempt}/{MAX_FIX_ATTEMPTS}")
        if state.get("commit_hash"):
            lines.append(f"Commit: {state['commit_hash']} → {state.get('pushed_branch', '?')}")
        if state.get("error"):
            lines.append(f"Error: {state['error']}")
        if state.get("created_at"):
            lines.append(f"Started: {self._fmt_ts(state['created_at'])}")
        if state.get("updated_at"):
            lines.append(f"Updated: {self._fmt_ts(state['updated_at'])}")
        return "\n".join(lines)
