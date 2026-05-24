from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import commands.logs as logs


def test_parse_args_defaults_to_bot():
    assert logs.parse_args([]) == ("bot", logs.DEFAULT_LINES, None)


def test_parse_args_accepts_alias_and_lines():
    assert logs.parse_args(["smoke", "80"]) == ("smoke", 80, None)


def test_parse_args_clamps_line_count():
    assert logs.parse_args(["bot", "999"]) == ("bot", logs.MAX_LINES, None)
    assert logs.parse_args(["bot", "0"]) == ("bot", 1, None)


def test_parse_args_all_defaults_to_compact_lines():
    assert logs.parse_args(["all"]) == ("all", 8, None)


def test_parse_args_rejects_unknown_alias():
    alias, _, error = logs.parse_args(["/tmp/secrets"])
    assert alias == "/tmp/secrets"
    assert "Unknown log alias" in error
    assert "bot" in error


def test_parse_args_rejects_bad_line_count():
    _, _, error = logs.parse_args(["bot", "many"])
    assert error == "Line count must be a number."


def test_redact_removes_known_secret_shapes():
    text = "\n".join([
        "https://api.telegram.org/bot123:ABC/getUpdates",
        "ANTHROPIC_API_KEY=sk-ant-secret",
        "TELEGRAM_BOT_TOKEN=123:secret",
        "Authorization: Bearer abc.def.ghi",
        "X-Api-Key: topsecret",
    ])
    redacted = logs.redact(text)
    assert "bot[REDACTED]/getUpdates" in redacted
    assert "ANTHROPIC_API_KEY=[REDACTED]" in redacted
    assert "TELEGRAM_BOT_TOKEN=[REDACTED]" in redacted
    assert "Authorization: Bearer [REDACTED]" in redacted
    assert "X-Api-Key: [REDACTED]" in redacted
    assert "sk-ant-secret" not in redacted
    assert "topsecret" not in redacted


def test_tail_lines_returns_last_n_lines_and_redacts(tmp_path):
    path = tmp_path / "bot.log"
    path.write_text(
        "\n".join([
            "line 1",
            "line 2",
            "https://api.telegram.org/bot123:ABC/sendMessage",
        ]),
        encoding="utf-8",
    )
    body, error = logs.tail_lines(path, 2)
    assert error is None
    assert "line 1" not in body
    assert "line 2" in body
    assert "bot[REDACTED]/sendMessage" in body


def test_tail_lines_missing_file(tmp_path):
    body, error = logs.tail_lines(tmp_path / "missing.log", 10)
    assert body == ""
    assert error == "log file not found"


def test_truncate_message_keeps_newest_content():
    text = "old-" + ("x" * 50) + "-new"
    truncated = logs.truncate_message(text, limit=35)
    assert truncated.startswith("...(truncated")
    assert truncated.endswith("-new")
    assert "old-" not in truncated


def test_format_single_log_uses_alias_path(tmp_path, monkeypatch):
    path = tmp_path / "bot.log"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    monkeypatch.setattr(logs, "LOG_ALIASES", {"bot": path})
    report = logs.format_single_log("bot", 2)
    assert "Logs: bot" in report
    assert "Path: bot.log" in report
    assert "Lines: 2" in report
    assert "one" not in report
    assert "two" in report
    assert "three" in report


def test_format_all_logs_compacts_known_aliases(tmp_path, monkeypatch):
    aliases = {}
    for alias in ("bot", "news", "health", "smoke"):
        path = tmp_path / f"{alias}.log"
        path.write_text(f"{alias} line\n", encoding="utf-8")
        aliases[alias] = path
    monkeypatch.setattr(logs, "LOG_ALIASES", aliases)
    monkeypatch.setattr(logs, "ALL_ALIASES", ("bot", "news", "health", "smoke"))
    report = logs.format_all_logs(1)
    assert "Logs: all" in report
    for alias in aliases:
        assert f"== {alias}" in report
        assert f"{alias} line" in report


def test_build_logs_report_returns_error_for_unknown_alias():
    report = logs.build_logs_report(["secret"])
    assert "Unknown log alias" in report


def test_build_logs_report_defaults_to_bot(tmp_path, monkeypatch):
    path = tmp_path / "bot.log"
    path.write_text("Application started\n", encoding="utf-8")
    monkeypatch.setattr(logs, "LOG_ALIASES", {"bot": path})
    report = logs.build_logs_report([])
    assert "Logs: bot" in report
    assert "Application started" in report


async def test_logs_handler_rejects_unauthorized(monkeypatch):
    monkeypatch.setattr(logs, "_is_allowed", lambda user_id: False)
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []

    await logs.logs_handler(update, context)

    reply = update.message.reply_text.call_args.args[0]
    assert "not authorized" in reply.lower()


async def test_logs_handler_replies_with_report(monkeypatch):
    monkeypatch.setattr(logs, "_is_allowed", lambda user_id: True)
    monkeypatch.setattr(logs, "build_logs_report", lambda args: f"Logs: {args}")
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["bot", "5"]

    await logs.logs_handler(update, context)

    update.message.reply_text.assert_called_once_with("Logs: ['bot', '5']")
