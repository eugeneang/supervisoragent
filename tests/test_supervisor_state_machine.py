"""Unit tests for SupervisorLoop state machine (supervisor_loop.py).

Covers: state persistence, timestamp formatting, stale detection,
/reject, /reset_build, /build_status, rollback helpers, and async
/design + /approve flows — all without touching the real state file
or making any network calls.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import supervisor_loop as sl
from supervisor_loop import SupervisorLoop, _DEFAULT_STATE, STALE_THRESHOLDS_SECONDS
from claude_bridge import ProposalResult, BuildResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Redirect STATE_FILE to a temp path so tests never touch the real file."""
    f = tmp_path / "supervisor_state.json"
    monkeypatch.setattr(sl, "STATE_FILE", f)
    return f


@pytest.fixture
def loop(state_file):
    """Fresh SupervisorLoop backed by an isolated state file."""
    return SupervisorLoop()


def _write_state(state_file: Path, **overrides) -> dict:
    """Write a state dict (with _DEFAULT_STATE base) to the temp file."""
    state = {**_DEFAULT_STATE, **overrides}
    state_file.write_text(json.dumps(state), encoding="utf-8")
    return state


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def test_load_state_returns_default_when_file_absent(loop, state_file):
    assert not state_file.exists()
    s = loop.load_state()
    assert s["state"] == "IDLE"


def test_save_and_load_roundtrip(loop, state_file):
    state = {**_DEFAULT_STATE, "state": "AWAITING_APPROVAL", "feature_name": "my_feature"}
    loop.save_state(state)
    loaded = loop.load_state()
    assert loaded["state"] == "AWAITING_APPROVAL"
    assert loaded["feature_name"] == "my_feature"


def test_save_state_writes_via_temp_file(loop, state_file):
    """save_state should write atomically; no leftover .tmp file."""
    loop.save_state({**_DEFAULT_STATE, "state": "IDLE"})
    assert state_file.exists()
    assert not state_file.with_suffix(".json.tmp").exists()


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------

def test_fmt_ts_converts_utc_to_sgt():
    # 2024-01-01 00:00 UTC → 2024-01-01 08:00 SGT
    result = SupervisorLoop._fmt_ts("2024-01-01T00:00:00+00:00")
    assert "2024-01-01" in result
    assert "08:00" in result
    assert "SGT" in result


def test_fmt_ts_none_returns_dash():
    assert SupervisorLoop._fmt_ts(None) == "—"


def test_fmt_ts_invalid_string_returns_raw():
    result = SupervisorLoop._fmt_ts("not-a-timestamp")
    assert result == "not-a-timestamp"


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------

def test_stale_when_age_exceeds_designing_threshold(loop):
    old = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
    state = {**_DEFAULT_STATE, "state": "DESIGNING", "updated_at": old}
    assert loop._is_stale(state) is True


def test_not_stale_when_age_under_designing_threshold(loop):
    recent = datetime.now(timezone.utc).isoformat()
    state = {**_DEFAULT_STATE, "state": "DESIGNING", "updated_at": recent}
    assert loop._is_stale(state) is False


def test_awaiting_approval_has_no_stale_threshold(loop):
    """AWAITING_APPROVAL is intentionally excluded from stale thresholds."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    state = {**_DEFAULT_STATE, "state": "AWAITING_APPROVAL", "updated_at": old}
    assert loop._is_stale(state) is False


def test_stale_committing_threshold_is_120s(loop):
    just_over = (datetime.now(timezone.utc) - timedelta(seconds=125)).isoformat()
    state = {**_DEFAULT_STATE, "state": "COMMITTING", "updated_at": just_over}
    assert loop._is_stale(state) is True


# ---------------------------------------------------------------------------
# /reject
# ---------------------------------------------------------------------------

def test_reject_from_idle_returns_nothing_in_progress(loop, state_file):
    _write_state(state_file, state="IDLE")
    result = loop.reject()
    assert "Nothing in progress" in result
    assert loop.load_state()["state"] == "IDLE"


def test_reject_from_awaiting_approval_resets_to_idle(loop, state_file):
    _write_state(state_file, state="AWAITING_APPROVAL", feature_name="f")
    result = loop.reject("not needed")
    assert "Reset to IDLE" in result
    assert loop.load_state()["state"] == "IDLE"


def test_reject_includes_reason_in_reply(loop, state_file):
    _write_state(state_file, state="AWAITING_APPROVAL")
    result = loop.reject("wrong direction")
    assert "wrong direction" in result


def test_reject_from_awaiting_commit_calls_rollback(loop, state_file):
    _write_state(state_file, state="AWAITING_COMMIT_APPROVAL", changed_files=["foo.py"])
    with patch.object(loop, "_rollback_build_files", return_value=(True, "")) as mock_rb:
        result = loop.reject()
    mock_rb.assert_called_once_with(["foo.py"])
    assert "rolled back" in result
    assert loop.load_state()["state"] == "IDLE"


def test_reject_from_awaiting_commit_reports_rollback_error(loop, state_file):
    _write_state(state_file, state="AWAITING_COMMIT_APPROVAL", changed_files=["foo.py"])
    with patch.object(loop, "_rollback_build_files", return_value=(False, "git error")):
        result = loop.reject()
    assert "error" in result.lower() or "⚠️" in result


# ---------------------------------------------------------------------------
# /reset_build
# ---------------------------------------------------------------------------

def test_force_reset_from_building_to_idle(loop, state_file):
    _write_state(state_file, state="BUILDING")
    result = loop.force_reset()
    assert "BUILDING" in result
    assert "IDLE" in result
    assert loop.load_state()["state"] == "IDLE"


def test_force_reset_from_awaiting_commit_rolls_back(loop, state_file):
    _write_state(state_file, state="AWAITING_COMMIT_APPROVAL", changed_files=["bar.py"])
    with patch.object(loop, "_rollback_build_files", return_value=(True, "")) as mock_rb:
        loop.force_reset()
    mock_rb.assert_called_once_with(["bar.py"])


def test_force_reset_clears_all_state_fields(loop, state_file):
    _write_state(state_file, state="BUILDING", feature_name="x", error="old error")
    loop.force_reset()
    s = loop.load_state()
    assert s["state"] == "IDLE"
    assert s["feature_name"] is None
    assert s["error"] is None


# ---------------------------------------------------------------------------
# /build_status
# ---------------------------------------------------------------------------

def test_get_status_idle(loop, state_file):
    _write_state(state_file, state="IDLE")
    status = loop.get_status()
    assert "IDLE" in status
    assert "design" in status.lower()


def test_get_status_awaiting_approval_shows_feature_and_request(loop, state_file):
    now = datetime.now(timezone.utc).isoformat()
    _write_state(
        state_file,
        state="AWAITING_APPROVAL",
        feature_name="my_feature",
        request_text="add something cool",
        created_at=now,
        updated_at=now,
    )
    status = loop.get_status()
    assert "AWAITING_APPROVAL" in status
    assert "my_feature" in status
    assert "add something cool" in status


def test_get_status_truncates_long_request_at_100_chars(loop, state_file):
    _write_state(state_file, state="AWAITING_APPROVAL", request_text="x" * 200)
    status = loop.get_status()
    assert "..." in status


def test_get_status_shows_error_field(loop, state_file):
    _write_state(state_file, state="DONE", error="Claude API returned 529")
    status = loop.get_status()
    assert "Claude API returned 529" in status


def test_get_status_shows_stale_warning(loop, state_file):
    old = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
    _write_state(state_file, state="DESIGNING", updated_at=old)
    status = loop.get_status()
    assert "STALE" in status


# ---------------------------------------------------------------------------
# Rollback helpers
# ---------------------------------------------------------------------------

def test_rollback_empty_file_list_succeeds(loop):
    ok, msg = loop._rollback_build_files([])
    assert ok is True
    assert msg == "No files to roll back."


def test_rollback_tracked_file_calls_git_restore(loop):
    git_mock = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    with patch.object(loop, "_git", git_mock):
        ok, _ = loop._rollback_build_files(["tracked.py"])
    assert ok is True
    called_cmds = [tuple(call.args[0]) for call in git_mock.call_args_list]
    assert any("restore" in cmd for cmd in called_cmds)


def test_rollback_untracked_file_is_deleted(loop, tmp_path, monkeypatch):
    new_file = tmp_path / "generated.py"
    new_file.write_text("# build output")
    monkeypatch.setattr(sl, "REPO_ROOT", tmp_path)

    # ls-files returncode=1 → file not tracked by git
    git_mock = MagicMock(return_value=MagicMock(returncode=1, stderr=""))
    with patch.object(loop, "_git", git_mock):
        ok, _ = loop._rollback_build_files(["generated.py"])
    assert ok is True
    assert not new_file.exists()


def test_rollback_restore_failure_returns_error(loop):
    def _git_side_effect(cmd):
        if "ls-files" in cmd:
            return MagicMock(returncode=0, stderr="")  # tracked
        if "restore" in cmd:
            return MagicMock(returncode=1, stderr="fatal: pathspec error")
        return MagicMock(returncode=0, stderr="")

    with patch.object(loop, "_git", side_effect=_git_side_effect):
        ok, err = loop._rollback_build_files(["bad.py"])
    assert ok is False
    assert "restore bad.py" in err


# ---------------------------------------------------------------------------
# /design (async state transitions)
# ---------------------------------------------------------------------------

async def test_start_design_transitions_to_awaiting_approval(loop, state_file):
    mock_bridge = MagicMock()
    mock_bridge.generate_proposal = AsyncMock(
        return_value=ProposalResult(
            feature_name="test_feature",
            proposal_text="## Design\nSome proposal",
        )
    )
    with patch.object(loop, "_get_bridge", return_value=mock_bridge):
        proposal, _ = await loop.start_design(chat_id=123, request_text="add tests")

    assert proposal == "## Design\nSome proposal"
    s = loop.load_state()
    assert s["state"] == "AWAITING_APPROVAL"
    assert s["feature_name"] == "test_feature"
    assert s["request_text"] == "add tests"


async def test_start_design_blocked_when_already_active(loop, state_file):
    _write_state(state_file, state="AWAITING_APPROVAL")
    proposal, msg = await loop.start_design(chat_id=123, request_text="another feature")
    assert proposal is None
    assert "in progress" in msg.lower()
    assert loop.load_state()["state"] == "AWAITING_APPROVAL"  # unchanged


async def test_start_design_resets_to_idle_on_bridge_error(loop, state_file):
    mock_bridge = MagicMock()
    mock_bridge.generate_proposal = AsyncMock(
        return_value=ProposalResult(feature_name="unknown", proposal_text="", error="API timeout")
    )
    with patch.object(loop, "_get_bridge", return_value=mock_bridge):
        proposal, msg = await loop.start_design(chat_id=123, request_text="add tests")

    assert proposal is None
    assert "API timeout" in msg
    assert loop.load_state()["state"] == "IDLE"


async def test_approve_returns_error_when_not_awaiting_approval(loop, state_file):
    _write_state(state_file, state="IDLE")
    notify = AsyncMock()
    result = await loop.approve(chat_id=123, notify_callback=notify)
    assert "Nothing to approve" in result


async def test_approve_commit_returns_error_when_not_awaiting_commit(loop, state_file):
    _write_state(state_file, state="AWAITING_APPROVAL")
    notify = AsyncMock()
    result = await loop.approve_commit(notify_callback=notify)
    assert "Nothing to commit" in result
