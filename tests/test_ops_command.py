import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import commands.ops as ops


def _completed(stdout="", stderr="", returncode=0):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


def test_redact_masks_telegram_bot_tokens():
    text = "POST https://api.telegram.org/bot123:ABC/getUpdates ok"
    assert ops._redact(text) == "POST https://api.telegram.org/bot[REDACTED]/getUpdates ok"


def test_parse_launchctl_extracts_jobs():
    output = (
        "PID\tStatus\tLabel\n"
        "123\t0\tcom.eugene.telegram_bot\n"
        "-\t0\tcom.eugene.ai_news_push\n"
        "-\t1\tcom.eugene.health_monitor\n"
    )
    jobs = ops._parse_launchctl(output)
    assert jobs["com.eugene.telegram_bot"].state == "running pid=123"
    assert jobs["com.eugene.ai_news_push"].state == "loaded"
    assert jobs["com.eugene.health_monitor"].state == "exited status=1"


def test_tail_nonempty_line_returns_redacted_last_line(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "\nold line\nPOST https://api.telegram.org/bot123:ABC/getUpdates ok\n",
        encoding="utf-8",
    )
    assert ops._tail_nonempty_line(log) == "POST https://api.telegram.org/bot[REDACTED]/getUpdates ok"


def test_tail_nonempty_line_handles_missing_file(tmp_path):
    assert ops._tail_nonempty_line(tmp_path / "missing.log") == "no log found"


def test_read_json_handles_missing_and_invalid(tmp_path):
    assert ops._read_json(tmp_path / "missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert ops._read_json(bad) == {}


def test_git_status_clean_with_upstream(monkeypatch):
    responses = {
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): _completed(stdout="main\n"),
        ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): _completed(stdout="origin/main\n"),
        ("git", "status", "--porcelain"): _completed(stdout=""),
        ("git", "log", "-1", "--format=%h %s"): _completed(stdout="abc123 test commit\n"),
        ("git", "rev-list", "--left-right", "--count", "main...origin/main"): _completed(stdout="2\t1\n"),
    }

    def fake_run(cmd, cwd=None):
        return responses[tuple(cmd)]

    monkeypatch.setattr(ops, "_run_command", fake_run)
    status = ops._git_status()
    assert status == {
        "branch": "main",
        "upstream": "origin/main",
        "status": "clean",
        "ahead_behind": "2/1",
        "last_commit": "abc123 test commit",
    }


def test_git_status_dirty_without_upstream(monkeypatch):
    responses = {
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): _completed(stdout="feature\n"),
        ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): _completed(returncode=1, stderr="no upstream"),
        ("git", "status", "--porcelain"): _completed(stdout=" M file.py\n"),
        ("git", "log", "-1", "--format=%h %s"): _completed(stdout="def456 work\n"),
    }

    def fake_run(cmd, cwd=None):
        return responses[tuple(cmd)]

    monkeypatch.setattr(ops, "_run_command", fake_run)
    status = ops._git_status()
    assert status["upstream"] == "none"
    assert status["status"] == "dirty"
    assert status["ahead_behind"] == "unknown"


def test_build_ops_report_includes_sections_and_config(tmp_path, monkeypatch):
    config = tmp_path / "ai_news_config.json"
    config.write_text(json.dumps({
        "enabled": False,
        "daily_push_time": "10:15",
        "timezone": "Asia/Singapore",
        "last_sent_date": "2026-05-24",
    }), encoding="utf-8")
    logs = {}
    for name in ("bot", "news", "health", "smoke"):
        path = tmp_path / f"{name}.log"
        path.write_text(f"{name} ok\n", encoding="utf-8")
        logs[name] = path

    jobs = {
        "com.eugene.telegram_bot": ops.LaunchdJob("10", "0", "com.eugene.telegram_bot"),
        "com.eugene.ai_news_push": ops.LaunchdJob("-", "0", "com.eugene.ai_news_push"),
        "com.eugene.health_monitor": ops.LaunchdJob("-", "1", "com.eugene.health_monitor"),
    }

    monkeypatch.setattr(ops, "AI_NEWS_CONFIG", config)
    monkeypatch.setattr(ops, "LOG_PATHS", logs)
    monkeypatch.setattr(ops, "_launchd_jobs", lambda: (jobs, None))
    monkeypatch.setattr(ops, "_git_status", lambda: {
        "branch": "main",
        "upstream": "origin/main",
        "status": "clean",
        "ahead_behind": "0/0",
        "last_commit": "abc123 done",
    })

    report = ops.build_ops_report()
    assert "Ops Status" in report
    assert "Telegram Bot" in report
    assert "AI News" in report
    assert "Config: disabled" in report
    assert "Schedule: 10:15 Asia/Singapore" in report
    assert "Last sent: 2026-05-24" in report
    assert "Health Monitor" in report
    assert "Smoke Tests" in report
    assert "Git" in report
    assert "Ahead/behind: 0/0" in report


async def test_ops_handler_rejects_unauthorized(monkeypatch):
    monkeypatch.setattr(ops, "_is_allowed", lambda user_id: False)
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await ops.ops_handler(update, context)

    reply = update.message.reply_text.call_args.args[0]
    assert "not authorized" in reply.lower()


async def test_ops_handler_replies_with_report(monkeypatch):
    monkeypatch.setattr(ops, "_is_allowed", lambda user_id: True)
    monkeypatch.setattr(ops, "build_ops_report", lambda: "Ops Status\nGit")
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await ops.ops_handler(update, context)

    update.message.reply_text.assert_called_once_with("Ops Status\nGit")
