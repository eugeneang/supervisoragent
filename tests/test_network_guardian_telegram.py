from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import telegram_bot as tb
from network_guardian import (
    GuardianStore,
    HealthSnapshot,
    NetworkGuardian,
    NetworkSnapshot,
    SpeedTestResult,
)


class Collector:
    def collect(self):
        return NetworkSnapshot(
            "2026-07-14T08:00:00+08:00",
            (),
            HealthSnapshot("192.168.1.1", True, True, True, {}),
        )


def make_update(user_id: int = 42):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        message=message,
    )
    return update


def make_context(*args: str):
    return SimpleNamespace(args=list(args))


@pytest.fixture
def isolated_guardian(tmp_path, monkeypatch):
    service = NetworkGuardian(GuardianStore(tmp_path / "state.json"), Collector())
    monkeypatch.setattr(tb, "_guardian", service)
    monkeypatch.setattr(tb, "AUTHORIZED_USER_ID", 42)
    return service


@pytest.mark.asyncio
async def test_status_scan_devices_alerts_summary_and_actions_commands(isolated_guardian):
    scan_update = make_update()
    await tb.net_scan_command(scan_update, make_context())
    assert scan_update.message.reply_text.await_count == 2
    assert "read-only network observation" in scan_update.message.reply_text.await_args_list[0].args[0]
    assert "No new alerts" in scan_update.message.reply_text.await_args_list[1].args[0]

    commands = [
        (tb.net_status_command, "Network Guardian"),
        (tb.net_devices_command, "No devices"),
        (tb.net_alerts_command, "No Network Guardian alerts"),
        (tb.net_summary_command, "Network Guardian Summary"),
        (tb.net_actions_command, "No intrusive network actions"),
    ]
    for handler, expected in commands:
        update = make_update()
        await handler(update, make_context())
        assert expected in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_network_commands_reject_unauthorized_user(isolated_guardian):
    update = make_update(user_id=999)
    await tb.net_status_command(update, make_context())
    assert update.message.reply_text.await_args.args[0] == "⛔ Unauthorized."


@pytest.mark.asyncio
async def test_approve_and_reject_require_action_id(isolated_guardian):
    for handler in (tb.net_approve_command, tb.net_reject_command):
        update = make_update()
        await handler(update, make_context())
        assert "Usage:" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_unknown_action_cannot_be_approved(isolated_guardian):
    update = make_update()
    await tb.net_approve_command(update, make_context("missing"))
    assert "No network action found" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_help_lists_network_guardian_commands():
    update = make_update()
    await tb.help_command(update, make_context())
    text = update.message.reply_text.await_args.args[0]
    assert "/net_status" in text
    assert "/net_approve" in text
    assert "/net_speed" in text


@pytest.mark.asyncio
async def test_adhoc_speed_command_records_and_replies(isolated_guardian, monkeypatch):
    runner = SimpleNamespace(run=lambda now: SpeedTestResult(
        now.isoformat(), True, 350.0, 225.0, 12.5, None, None, "Apple networkQuality"
    ))
    monkeypatch.setattr(tb, "_speed_test_runner", runner)
    update = make_update()
    await tb.net_speed_command(update, make_context())
    assert update.message.reply_text.await_count == 2
    reply = update.message.reply_text.await_args_list[1].args[0]
    assert "Download: 350.0 Mbps" in reply
    assert "scheduled 8:30 AM test will still run" in reply
    records = isolated_guardian.store.load()["speed_tests"]
    assert records[0]["trigger"] == "adhoc"


@pytest.mark.asyncio
async def test_adhoc_speed_command_reports_failure(isolated_guardian, monkeypatch):
    runner = SimpleNamespace(run=lambda now: SpeedTestResult(
        now.isoformat(), False, error="test server unavailable"
    ))
    monkeypatch.setattr(tb, "_speed_test_runner", runner)
    update = make_update()
    await tb.net_speed_command(update, make_context())
    assert "test server unavailable" in update.message.reply_text.await_args_list[1].args[0]
