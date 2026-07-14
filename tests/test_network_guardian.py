from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from network_guardian import (
    GuardianScheduler,
    GuardianStore,
    HealthSnapshot,
    NetworkCollector,
    NetworkGuardian,
    NetworkSnapshot,
    ObservedDevice,
    SpeedTestResult,
    SpeedTestRunner,
    parse_service_targets,
)


SGT = ZoneInfo("Asia/Singapore")


def snapshot(
    when: str,
    devices: tuple[ObservedDevice, ...] = (),
    *,
    gateway: bool = True,
    dns: bool = True,
    internet: bool = True,
    services: dict[str, bool] | None = None,
) -> NetworkSnapshot:
    return NetworkSnapshot(
        observed_at=when,
        devices=devices,
        health=HealthSnapshot("192.168.1.1", gateway, dns, internet, services or {}),
    )


class SequenceCollector:
    def __init__(self, *snapshots: NetworkSnapshot):
        self.snapshots = list(snapshots)

    def collect(self) -> NetworkSnapshot:
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


def guardian(tmp_path: Path, *snapshots: NetworkSnapshot) -> NetworkGuardian:
    return NetworkGuardian(GuardianStore(tmp_path / "guardian.json"), SequenceCollector(*snapshots))


def test_parse_arp_filters_invalid_public_incomplete_and_broadcast_entries():
    output = """
? (192.168.1.1) at aa:bb:cc:dd:ee:01 on en0 ifscope [ethernet]
? (192.168.1.15) at aa:bb:cc:dd:ee:15 on en0 ifscope [ethernet]
? (192.168.1.20) at (incomplete) on en0 ifscope [ethernet]
? (192.168.1.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]
? (8.8.8.8) at aa:bb:cc:dd:ee:88 on en0 ifscope [ethernet]
garbage
"""
    devices = NetworkCollector.parse_arp(output)
    assert [(device.ip, device.mac) for device in devices] == [
        ("192.168.1.1", "aa:bb:cc:dd:ee:01"),
        ("192.168.1.15", "aa:bb:cc:dd:ee:15"),
    ]


def test_parse_service_targets_accepts_valid_and_ignores_malformed_values():
    result = parse_service_targets(
        "NAS=192.168.1.10:443,bad,Zero=host:0,TooHigh=host:99999,HA=home.local:8123"
    )
    assert result == {"NAS": ("192.168.1.10", 443), "HA": ("home.local", 8123)}


def test_speedtest_runner_parses_official_json_and_converts_bytes_per_second(monkeypatch):
    payload = {
        "ping": {"latency": 5.75, "jitter": 0.61},
        "download": {"bandwidth": 62_500_000},
        "upload": {"bandwidth": 25_000_000},
        "packetLoss": 0,
        "isp": "Example ISP",
        "server": {"name": "Example Server", "location": "Singapore"},
    }
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr("network_guardian.subprocess.run", lambda *args, **kwargs: completed)
    result = SpeedTestRunner("/usr/local/bin/speedtest").run(
        datetime(2026, 7, 14, 8, 30, tzinfo=SGT)
    )
    assert result.success
    assert result.download_mbps == 500
    assert result.upload_mbps == 200
    assert result.latency_ms == 5.75
    assert result.server == "Example Server — Singapore"


def test_speedtest_runner_fails_cleanly_when_not_installed(monkeypatch):
    monkeypatch.setattr("network_guardian.shutil.which", lambda executable: None)
    monkeypatch.setattr("network_guardian.Path.exists", lambda path: False)
    result = SpeedTestRunner(executable=None).run()
    assert "No supported speed-test tool" in result.error


def test_speedtest_runner_parses_apple_network_quality_json(monkeypatch):
    payload = {
        "dl_throughput": 330_323_168,
        "ul_throughput": 150_000_000,
        "base_rtt": 13.396,
    }
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr("network_guardian.subprocess.run", lambda *args, **kwargs: completed)
    result = SpeedTestRunner("/usr/bin/networkQuality").run(
        datetime(2026, 7, 14, 8, 30, tzinfo=SGT)
    )
    assert result.success
    assert result.download_mbps == 330.32
    assert result.upload_mbps == 150
    assert result.latency_ms == 13.4
    assert result.server == "Apple networkQuality"


@pytest.mark.asyncio
async def test_first_scan_seeds_baseline_without_alerting_on_existing_household(tmp_path: Path):
    device = ObservedDevice("192.168.1.10", "aa:bb:cc:00:00:10", "en0")
    service = guardian(tmp_path, snapshot("2026-07-14T08:00:00+08:00", (device,)))
    alerts = await service.scan()
    state = service.store.load()
    assert alerts == []
    assert state["initialized"] is True
    assert len(state["devices"]) == 1
    assert state["alerts"] == []


@pytest.mark.asyncio
async def test_new_device_after_baseline_creates_one_immediate_alert(tmp_path: Path):
    known = ObservedDevice("192.168.1.10", "aa:bb:cc:00:00:10", "en0")
    newcomer = ObservedDevice("192.168.1.22", "aa:bb:cc:00:00:22", "en0")
    service = guardian(
        tmp_path,
        snapshot("2026-07-14T08:00:00+08:00", (known,)),
        snapshot("2026-07-14T08:05:00+08:00", (known, newcomer)),
        snapshot("2026-07-14T08:10:00+08:00", (known, newcomer)),
    )
    await service.scan()
    alerts = await service.scan()
    repeated = await service.scan()
    assert len(alerts) == 1
    assert alerts[0].kind == "new_device"
    assert "192.168.1.22" in alerts[0].detail
    assert repeated == []


@pytest.mark.asyncio
async def test_health_failure_requires_two_scans_then_recovery_alerts(tmp_path: Path):
    service = guardian(
        tmp_path,
        snapshot("2026-07-14T08:00:00+08:00"),
        snapshot("2026-07-14T08:05:00+08:00", dns=False),
        snapshot("2026-07-14T08:10:00+08:00", dns=False),
        snapshot("2026-07-14T08:15:00+08:00", dns=False),
        snapshot("2026-07-14T08:20:00+08:00", dns=True),
    )
    assert await service.scan() == []
    assert await service.scan() == []
    down = await service.scan()
    assert len(down) == 1 and down[0].kind == "health_down"
    assert await service.scan() == []
    recovered = await service.scan()
    assert len(recovered) == 1 and recovered[0].kind == "health_recovered"


@pytest.mark.asyncio
async def test_configured_service_failure_is_named_in_alert(tmp_path: Path):
    service = guardian(
        tmp_path,
        snapshot("2026-07-14T08:00:00+08:00", services={"NAS": True}),
        snapshot("2026-07-14T08:05:00+08:00", services={"NAS": False}),
        snapshot("2026-07-14T08:10:00+08:00", services={"NAS": False}),
    )
    await service.scan()
    await service.scan()
    alerts = await service.scan()
    assert alerts[0].title == "Nas health check failed twice"


def test_corrupt_state_file_fails_safe_to_empty_state(tmp_path: Path):
    path = tmp_path / "guardian.json"
    path.write_text("not-json")
    assert GuardianStore(path).load()["initialized"] is False


@pytest.mark.asyncio
async def test_summary_reports_last_24_hours_and_read_only_guarantee(tmp_path: Path):
    new_device = ObservedDevice("192.168.1.30", "aa:bb:cc:00:00:30", "en0")
    service = guardian(
        tmp_path,
        snapshot("2026-07-14T08:00:00+08:00"),
        snapshot("2026-07-14T08:30:00+08:00", (new_device,)),
    )
    await service.scan()
    await service.scan()
    report = service.summary_text(datetime(2026, 7, 14, 9, 0, tzinfo=SGT))
    assert "New in last 24h: 1" in report
    assert "Alerts in last 24h: 1" in report
    assert "No network changes were made" in report


def test_intrusive_action_cannot_execute_and_requires_explicit_decision(tmp_path: Path):
    store = GuardianStore(tmp_path / "guardian.json")
    state = store.empty()
    state["actions"]["block-1"] = {
        "id": "block-1",
        "description": "Block device 192.168.1.50",
        "status": "pending",
    }
    store.save(state)
    service = NetworkGuardian(store, SequenceCollector(snapshot("2026-07-14T08:00:00+08:00")))
    assert "block-1" in service.pending_actions_text()
    reply = service.decide_action("block-1", approve=True)
    assert "approved, but not executed" in reply
    assert store.load()["actions"]["block-1"]["status"] == "approved"
    assert "already approved" in service.decide_action("block-1", approve=True)


def test_rejected_action_records_decision_without_execution(tmp_path: Path):
    store = GuardianStore(tmp_path / "guardian.json")
    state = store.empty()
    state["actions"]["restart-1"] = {
        "id": "restart-1",
        "description": "Restart router",
        "status": "pending",
    }
    store.save(state)
    service = NetworkGuardian(store, SequenceCollector(snapshot("2026-07-14T08:00:00+08:00")))
    assert "rejected" in service.decide_action("restart-1", approve=False)
    assert store.load()["actions"]["restart-1"]["status"] == "rejected"


@pytest.mark.asyncio
async def test_scheduler_sends_alerts_and_summary_once_per_day(tmp_path: Path):
    known = ObservedDevice("192.168.1.10", "aa:bb:cc:00:00:10", "en0")
    newcomer = ObservedDevice("192.168.1.20", "aa:bb:cc:00:00:20", "en0")
    service = guardian(
        tmp_path,
        snapshot("2026-07-14T08:55:00+08:00", (known,)),
        snapshot("2026-07-14T09:00:00+08:00", (known, newcomer)),
        snapshot("2026-07-14T09:05:00+08:00", (known, newcomer)),
    )
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    current = datetime(2026, 7, 14, 8, 55, tzinfo=SGT)
    scheduler = GuardianScheduler(service, send, now=lambda: current)
    await scheduler.tick()
    assert sent == []
    current = datetime(2026, 7, 14, 9, 0, tzinfo=SGT)
    await scheduler.tick()
    assert sum("New network device" in item for item in sent) == 1
    assert sum("9 AM Network Guardian Summary" in item for item in sent) == 1
    await scheduler.tick()
    assert sum("9 AM Network Guardian Summary" in item for item in sent) == 1


@pytest.mark.asyncio
async def test_scheduler_retries_summary_if_send_fails_before_marking_sent(tmp_path: Path):
    service = guardian(tmp_path, snapshot("2026-07-14T09:00:00+08:00"))
    calls = 0

    async def fail_once(text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("telegram unavailable")

    current = datetime(2026, 7, 14, 9, 0, tzinfo=SGT)
    scheduler = GuardianScheduler(service, fail_once, now=lambda: current)
    with pytest.raises(RuntimeError):
        await scheduler.tick()
    assert service.store.load()["last_summary_date"] is None
    await scheduler.tick()
    assert service.store.load()["last_summary_date"] == "2026-07-14"


class FakeSpeedTestRunner:
    def __init__(self, results: list[SpeedTestResult]):
        self.results = results
        self.calls = 0

    def run(self, now: datetime) -> SpeedTestResult:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return SpeedTestResult(**{**result.__dict__, "tested_at": now.isoformat()})


@pytest.mark.asyncio
async def test_speedtest_runs_at_830_once_and_is_in_9am_summary(tmp_path: Path):
    service = guardian(tmp_path, snapshot("2026-07-15T08:30:00+08:00"))
    successful = SpeedTestResult(
        "placeholder", True, 500.0, 200.0, 5.0, 0.5, 0.0, "Singapore", "ISP"
    )
    runner = FakeSpeedTestRunner([successful])
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    current = datetime(2026, 7, 15, 8, 30, tzinfo=SGT)
    scheduler = GuardianScheduler(service, send, now=lambda: current, speed_test_runner=runner)
    await scheduler.tick()
    await scheduler.tick()
    assert runner.calls == 1
    current = datetime(2026, 7, 15, 9, 0, tzinfo=SGT)
    await scheduler.tick()
    summary = next(text for text in sent if "9 AM Network Guardian Summary" in text)
    assert "Download: 500.0 Mbps" in summary
    assert "Upload: 200.0 Mbps" in summary
    assert "Latency: 5.0 ms" in summary


@pytest.mark.asyncio
async def test_failed_830_speedtest_retries_once_after_840(tmp_path: Path):
    service = guardian(
        tmp_path,
        snapshot("2026-07-15T08:30:00+08:00"),
        snapshot("2026-07-15T08:40:00+08:00"),
        snapshot("2026-07-15T08:45:00+08:00"),
    )
    failed = SpeedTestResult("placeholder", False, error="server unavailable")
    recovered = SpeedTestResult("placeholder", True, 450.0, 180.0, 6.0, 0.8, 0.0)
    runner = FakeSpeedTestRunner([failed, recovered])

    async def send(text: str) -> None:
        pass

    current = datetime(2026, 7, 15, 8, 30, tzinfo=SGT)
    scheduler = GuardianScheduler(service, send, now=lambda: current, speed_test_runner=runner)
    await scheduler.tick()
    current = datetime(2026, 7, 15, 8, 40, tzinfo=SGT)
    await scheduler.tick()
    current = datetime(2026, 7, 15, 8, 45, tzinfo=SGT)
    await scheduler.tick()
    assert runner.calls == 2
    assert len(service.speed_attempts_on("2026-07-15", SGT)) == 2
    assert "Download: 450.0 Mbps" in service.speed_summary_text(current)


def test_speed_summary_compares_current_result_with_prior_seven_day_median(tmp_path: Path):
    service = guardian(tmp_path, snapshot("2026-07-15T08:30:00+08:00"))
    service.record_speed_test(
        SpeedTestResult("2026-07-13T08:30:00+08:00", True, 400.0, 200.0, 5.0, 0.5, 0.0)
    )
    service.record_speed_test(
        SpeedTestResult("2026-07-14T08:30:00+08:00", True, 600.0, 300.0, 5.0, 0.5, 0.0)
    )
    service.record_speed_test(
        SpeedTestResult("2026-07-15T08:30:00+08:00", True, 450.0, 200.0, 6.0, 0.8, 0.0)
    )
    text = service.speed_summary_text(datetime(2026, 7, 15, 9, 0, tzinfo=SGT))
    assert "download -10%" in text
    assert "upload -20%" in text


def test_adhoc_result_does_not_count_as_scheduled_attempt(tmp_path: Path):
    service = guardian(tmp_path, snapshot("2026-07-15T07:30:00+08:00"))
    service.record_speed_test(
        SpeedTestResult("2026-07-15T07:30:00+08:00", True, 300.0, 100.0, 8.0),
        trigger="adhoc",
    )
    assert service.speed_attempts_on("2026-07-15", SGT) == []
    assert "Download: 300.0 Mbps" in service.speed_summary_text(
        datetime(2026, 7, 15, 8, 0, tzinfo=SGT)
    )
