"""Read-only home-network guardian with approval-gated action scaffolding.

The guardian deliberately does not scan ports, alter DNS, block clients, or change
router state. It observes the local ARP cache and runs a few low-impact health
checks. Any future mutating integration must enter the action proposal workflow.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import shutil
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo


_ARP_RE = re.compile(
    r"^\? \((?P<ip>[^)]+)\) at (?P<mac>[^ ]+) on (?P<interface>[^ ]+)"
)


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now().astimezone()).isoformat()


def _safe_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_private
    except ValueError:
        return False


@dataclass(frozen=True)
class ObservedDevice:
    ip: str
    mac: str
    interface: str

    @property
    def identity(self) -> str:
        return self.mac.lower() if self.mac not in {"(incomplete)", "ff:ff:ff:ff:ff:ff"} else self.ip


@dataclass(frozen=True)
class HealthSnapshot:
    gateway_ip: str | None
    gateway_ok: bool
    dns_ok: bool
    internet_ok: bool
    services: dict[str, bool]


@dataclass(frozen=True)
class NetworkSnapshot:
    observed_at: str
    devices: tuple[ObservedDevice, ...]
    health: HealthSnapshot


@dataclass(frozen=True)
class GuardianAlert:
    id: str
    kind: str
    severity: str
    title: str
    detail: str
    created_at: str

    def telegram_text(self) -> str:
        icon = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}.get(self.severity, "⚠️")
        return f"{icon} Network Guardian\n\n{self.title}\n{self.detail}\n\nAlert ID: {self.id}"


@dataclass(frozen=True)
class SpeedTestResult:
    tested_at: str
    success: bool
    download_mbps: float | None = None
    upload_mbps: float | None = None
    latency_ms: float | None = None
    jitter_ms: float | None = None
    packet_loss_percent: float | None = None
    server: str | None = None
    isp: str | None = None
    error: str | None = None


class SpeedTestRunner:
    """Run Ookla when installed, otherwise Apple's built-in networkQuality."""

    def __init__(self, executable: str | None = None, timeout_seconds: int = 180):
        self.executable = executable or shutil.which("speedtest") or (
            "/usr/bin/networkQuality" if Path("/usr/bin/networkQuality").exists() else None
        )
        self.timeout_seconds = timeout_seconds

    def run(self, now: datetime | None = None) -> SpeedTestResult:
        tested_at = _now_iso(now)
        if not self.executable:
            return SpeedTestResult(tested_at, False, error="No supported speed-test tool is installed")
        is_apple = Path(self.executable).name == "networkQuality"
        command = (
            [self.executable, "-c"]
            if is_apple
            else [
                self.executable,
                "--accept-license",
                "--accept-gdpr",
                "--format=json",
            ]
        )
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return SpeedTestResult(tested_at, False, error="Speed test timed out")
        except OSError as exc:
            return SpeedTestResult(tested_at, False, error=f"Could not start Speedtest: {exc}")
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "unknown error").strip().splitlines()[-1]
            return SpeedTestResult(tested_at, False, error=f"Speedtest failed: {detail[:200]}")
        try:
            payload = json.loads(process.stdout)
            if is_apple:
                return SpeedTestResult(
                    tested_at=tested_at,
                    success=True,
                    download_mbps=round(float(payload["dl_throughput"]) / 1_000_000, 2),
                    upload_mbps=round(float(payload["ul_throughput"]) / 1_000_000, 2),
                    latency_ms=round(float(payload["base_rtt"]), 2),
                    server="Apple networkQuality",
                )
            download = float(payload["download"]["bandwidth"]) / 125_000
            upload = float(payload["upload"]["bandwidth"]) / 125_000
            ping = payload.get("ping", {})
            server = payload.get("server", {})
            server_label = " — ".join(
                value for value in (server.get("name"), server.get("location")) if value
            ) or None
            packet_loss = payload.get("packetLoss")
            return SpeedTestResult(
                tested_at=tested_at,
                success=True,
                download_mbps=round(download, 2),
                upload_mbps=round(upload, 2),
                latency_ms=round(float(ping["latency"]), 2) if ping.get("latency") is not None else None,
                jitter_ms=round(float(ping["jitter"]), 2) if ping.get("jitter") is not None else None,
                packet_loss_percent=round(float(packet_loss), 2) if packet_loss is not None else None,
                server=server_label,
                isp=payload.get("isp"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return SpeedTestResult(tested_at, False, error=f"Invalid Speedtest result: {exc}")


class NetworkCollector:
    """Low-impact collectors suitable for an always-on home network."""

    def __init__(self, service_targets: dict[str, tuple[str, int]] | None = None):
        self.service_targets = service_targets or {}

    @staticmethod
    def parse_arp(output: str) -> tuple[ObservedDevice, ...]:
        devices: dict[str, ObservedDevice] = {}
        for line in output.splitlines():
            match = _ARP_RE.match(line.strip())
            if not match:
                continue
            values = match.groupdict()
            if not _safe_ip(values["ip"]) or values["mac"] in {"(incomplete)", "ff:ff:ff:ff:ff:ff"}:
                continue
            device = ObservedDevice(**values)
            devices[device.identity] = device
        return tuple(sorted(devices.values(), key=lambda item: tuple(int(p) for p in item.ip.split("."))))

    def collect_devices(self) -> tuple[ObservedDevice, ...]:
        try:
            result = subprocess.run(
                ["/usr/sbin/arp", "-an"], capture_output=True, text=True, timeout=5, check=False
            )
            return self.parse_arp(result.stdout)
        except (OSError, subprocess.SubprocessError):
            return ()

    @staticmethod
    def default_gateway() -> str | None:
        try:
            result = subprocess.run(
                ["/sbin/route", "-n", "get", "default"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"^\s*gateway:\s*(\S+)", result.stdout, re.MULTILINE)
        return match.group(1) if match and _safe_ip(match.group(1)) else None

    @staticmethod
    def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def ping_reachable(host: str) -> bool:
        """Send one ICMP echo; no discovery sweep or repeated traffic."""
        try:
            result = subprocess.run(
                ["/sbin/ping", "-c", "1", "-W", "1000", host],
                capture_output=True,
                timeout=3,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def dns_works() -> bool:
        try:
            socket.getaddrinfo("example.com", 443, type=socket.SOCK_STREAM)
            return True
        except OSError:
            return False

    def collect(self) -> NetworkSnapshot:
        gateway = self.default_gateway()
        services = {
            name: self.tcp_reachable(host, port)
            for name, (host, port) in self.service_targets.items()
        }
        return NetworkSnapshot(
            observed_at=_now_iso(),
            devices=self.collect_devices(),
            health=HealthSnapshot(
                gateway_ip=gateway,
                gateway_ok=bool(gateway and self.ping_reachable(gateway)),
                dns_ok=self.dns_works(),
                internet_ok=self.tcp_reachable("1.1.1.1", 443),
                services=services,
            ),
        )


def parse_service_targets(raw: str | None) -> dict[str, tuple[str, int]]:
    """Parse ``Name=host:port,Other=host:port``; ignore malformed entries."""
    targets: dict[str, tuple[str, int]] = {}
    for item in (raw or "").split(","):
        if not item.strip() or "=" not in item or ":" not in item:
            continue
        name, endpoint = item.split("=", 1)
        host, port_raw = endpoint.rsplit(":", 1)
        try:
            port = int(port_raw)
        except ValueError:
            continue
        if name.strip() and host.strip() and 1 <= port <= 65535:
            targets[name.strip()] = (host.strip(), port)
    return targets


class GuardianStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return self.empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else self.empty()
        except (OSError, json.JSONDecodeError):
            return self.empty()

    @staticmethod
    def empty() -> dict:
        return {
            "initialized": False,
            "last_scan": None,
            "devices": {},
            "health": {},
            "alerts": [],
            "actions": {},
            "speed_tests": [],
            "last_summary_date": None,
        }

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


class NetworkGuardian:
    FAILURE_THRESHOLD = 2

    def __init__(self, store: GuardianStore, collector: NetworkCollector):
        self.store = store
        self.collector = collector
        self._lock = asyncio.Lock()

    async def scan(self) -> list[GuardianAlert]:
        snapshot = await asyncio.to_thread(self.collector.collect)
        async with self._lock:
            state = self.store.load()
            alerts = self._apply_snapshot(state, snapshot)
            self.store.save(state)
        return alerts

    def _apply_snapshot(self, state: dict, snapshot: NetworkSnapshot) -> list[GuardianAlert]:
        alerts: list[GuardianAlert] = []
        first_run = not state.get("initialized")
        devices = state.setdefault("devices", {})
        for observed in snapshot.devices:
            existing = devices.get(observed.identity)
            if existing is None:
                devices[observed.identity] = {
                    **asdict(observed),
                    "name": None,
                    "first_seen": snapshot.observed_at,
                    "last_seen": snapshot.observed_at,
                    "observations": 1,
                }
                if not first_run:
                    alerts.append(self._new_device_alert(observed, snapshot.observed_at))
            else:
                existing.update(asdict(observed))
                existing["last_seen"] = snapshot.observed_at
                existing["observations"] = int(existing.get("observations", 0)) + 1

        old_health = state.setdefault("health", {})
        checks = {
            "gateway": snapshot.health.gateway_ok,
            "dns": snapshot.health.dns_ok,
            "internet": snapshot.health.internet_ok,
            **{f"service:{name}": value for name, value in snapshot.health.services.items()},
        }
        for name, ok in checks.items():
            previous = old_health.get(name, {"ok": None, "failures": 0})
            failures = 0 if ok else int(previous.get("failures", 0)) + 1
            if not first_run and not ok and failures == self.FAILURE_THRESHOLD:
                alerts.append(self._health_alert(name, snapshot.observed_at))
            elif not first_run and ok and previous.get("ok") is False and int(previous.get("failures", 0)) >= self.FAILURE_THRESHOLD:
                alerts.append(self._recovery_alert(name, snapshot.observed_at))
            old_health[name] = {"ok": ok, "failures": failures, "checked_at": snapshot.observed_at}

        state["initialized"] = True
        state["last_scan"] = snapshot.observed_at
        state["gateway_ip"] = snapshot.health.gateway_ip
        stored_alerts = state.setdefault("alerts", [])
        stored_alerts.extend(asdict(alert) for alert in alerts)
        state["alerts"] = stored_alerts[-200:]
        return alerts

    @staticmethod
    def _alert_id(kind: str, created_at: str) -> str:
        compact = datetime.fromisoformat(created_at).strftime("%Y%m%d%H%M%S")
        return f"{kind.replace(':', '-')}-{compact}"

    def _new_device_alert(self, device: ObservedDevice, created_at: str) -> GuardianAlert:
        return GuardianAlert(
            id=self._alert_id("new-device", created_at),
            kind="new_device",
            severity="warning",
            title="New network device observed",
            detail=f"IP: {device.ip}\nMAC: {device.mac}\nInterface: {device.interface}\nNo action was taken.",
            created_at=created_at,
        )

    def _health_alert(self, name: str, created_at: str) -> GuardianAlert:
        return GuardianAlert(
            id=self._alert_id(f"health-{name}", created_at),
            kind="health_down",
            severity="critical" if name in {"gateway", "internet"} else "warning",
            title=f"{name.replace('service:', '').title()} health check failed twice",
            detail="The guardian confirmed the failure on two consecutive scans. No corrective action was taken.",
            created_at=created_at,
        )

    def _recovery_alert(self, name: str, created_at: str) -> GuardianAlert:
        return GuardianAlert(
            id=self._alert_id(f"recovered-{name}", created_at),
            kind="health_recovered",
            severity="info",
            title=f"{name.replace('service:', '').title()} recovered",
            detail="The latest health check succeeded again.",
            created_at=created_at,
        )

    def status_text(self) -> str:
        state = self.store.load()
        if not state.get("initialized"):
            return "🛡 Network Guardian\n\nNo baseline yet. Run /net_scan or wait for the background scan."
        health = state.get("health", {})
        lines = ["🛡 Network Guardian", "", f"Last scan: {state.get('last_scan', 'unknown')}"]
        lines.append(f"Known devices: {len(state.get('devices', {}))}")
        for name in ("gateway", "internet", "dns"):
            record = health.get(name, {})
            lines.append(f"{name.title()}: {'✅ healthy' if record.get('ok') else '❌ failing'}")
        services = sorted(key for key in health if key.startswith("service:"))
        for key in services:
            lines.append(f"{key.split(':', 1)[1]}: {'✅ healthy' if health[key].get('ok') else '❌ failing'}")
        lines.append("Mode: read-only; intrusive actions require explicit approval")
        return "\n".join(lines)

    def devices_text(self) -> str:
        devices = list(self.store.load().get("devices", {}).values())
        if not devices:
            return "No devices observed yet. Run /net_scan first."
        devices.sort(key=lambda item: tuple(int(p) for p in item["ip"].split(".")))
        lines = [f"📡 Known devices ({len(devices)})", ""]
        for item in devices[:50]:
            label = item.get("name") or "Unknown"
            lines.append(f"• {label} — {item['ip']} — {item['mac']}")
        if len(devices) > 50:
            lines.append(f"…and {len(devices) - 50} more")
        return "\n".join(lines)

    def alerts_text(self, limit: int = 10) -> str:
        alerts = self.store.load().get("alerts", [])[-limit:]
        if not alerts:
            return "✅ No Network Guardian alerts recorded."
        lines = [f"⚠️ Recent network alerts ({len(alerts)})", ""]
        for alert in reversed(alerts):
            lines.append(f"• [{alert['severity']}] {alert['title']} — {alert['created_at']}")
        return "\n".join(lines)

    def summary_text(self, now: datetime | None = None) -> str:
        now = now or datetime.now().astimezone()
        state = self.store.load()
        since = now - timedelta(hours=24)
        new_devices = [
            item for item in state.get("devices", {}).values()
            if datetime.fromisoformat(item["first_seen"]) >= since
        ]
        alerts = [
            item for item in state.get("alerts", [])
            if datetime.fromisoformat(item["created_at"]) >= since
        ]
        health = state.get("health", {})
        failing = [key.replace("service:", "") for key, value in health.items() if not value.get("ok")]
        return (
            "☀️ 9 AM Network Guardian Summary\n\n"
            f"Known devices: {len(state.get('devices', {}))}\n"
            f"New in last 24h: {len(new_devices)}\n"
            f"Alerts in last 24h: {len(alerts)}\n"
            f"Currently failing: {', '.join(failing) if failing else 'none'}\n"
            f"Last scan: {state.get('last_scan') or 'not yet scanned'}\n\n"
            f"{self.speed_summary_text(now)}\n\n"
            "No network changes were made."
        )

    def record_speed_test(self, result: SpeedTestResult, trigger: str = "scheduled") -> None:
        state = self.store.load()
        history = state.setdefault("speed_tests", [])
        history.append({**asdict(result), "trigger": trigger})
        state["speed_tests"] = history[-90:]
        self.store.save(state)

    def speed_attempts_on(self, local_date: str, timezone: ZoneInfo) -> list[dict]:
        attempts = []
        for item in self.store.load().get("speed_tests", []):
            tested = datetime.fromisoformat(item["tested_at"]).astimezone(timezone)
            if tested.date().isoformat() == local_date and item.get("trigger", "scheduled") == "scheduled":
                attempts.append(item)
        return attempts

    def speed_summary_text(self, now: datetime) -> str:
        timezone = now.tzinfo or datetime.now().astimezone().tzinfo
        today = now.astimezone(timezone).date().isoformat()
        history = self.store.load().get("speed_tests", [])
        all_today_results = [
            item for item in history
            if datetime.fromisoformat(item["tested_at"]).astimezone(timezone).date().isoformat() == today
        ]
        scheduled_results = [
            item for item in all_today_results if item.get("trigger", "scheduled") == "scheduled"
        ]
        today_results = scheduled_results or all_today_results
        if not today_results:
            return "📶 Internet performance — 8:30 AM\nNo speed test result is available today."
        current = next((item for item in reversed(today_results) if item.get("success")), today_results[-1])
        if not current.get("success"):
            previous = next((
                item for item in reversed(history)
                if item.get("success")
                and datetime.fromisoformat(item["tested_at"]).astimezone(timezone).date().isoformat() != today
            ), None)
            lines = ["📶 Internet performance — 8:30 AM", current.get("error") or "Speed test failed"]
            if previous:
                lines.append(
                    f"Last successful: {previous['download_mbps']:.1f} Mbps down / "
                    f"{previous['upload_mbps']:.1f} Mbps up"
                )
            return "\n".join(lines)

        current_time = datetime.fromisoformat(current["tested_at"])
        cutoff = current_time - timedelta(days=7)
        previous = [
            item for item in history
            if item.get("success")
            and item.get("trigger", "scheduled") == "scheduled"
            and cutoff <= datetime.fromisoformat(item["tested_at"]) < current_time
            and datetime.fromisoformat(item["tested_at"]).date() != current_time.date()
        ]
        lines = [
            "📶 Internet performance — 8:30 AM",
            f"Download: {current['download_mbps']:.1f} Mbps",
            f"Upload: {current['upload_mbps']:.1f} Mbps",
            f"Latency: {current['latency_ms']:.1f} ms" if current.get("latency_ms") is not None else "Latency: unavailable",
            f"Jitter: {current['jitter_ms']:.1f} ms" if current.get("jitter_ms") is not None else "Jitter: unavailable",
            (
                f"Packet loss: {current['packet_loss_percent']:.1f}%"
                if current.get("packet_loss_percent") is not None
                else "Packet loss: unavailable"
            ),
        ]
        if previous:
            median_down = statistics.median(item["download_mbps"] for item in previous)
            median_up = statistics.median(item["upload_mbps"] for item in previous)
            down_delta = ((current["download_mbps"] - median_down) / median_down * 100) if median_down else 0
            up_delta = ((current["upload_mbps"] - median_up) / median_up * 100) if median_up else 0
            lines.append(f"Vs 7-day median: download {down_delta:+.0f}%, upload {up_delta:+.0f}%")
        else:
            lines.append("Trend: baseline not established yet")
        return "\n".join(lines)

    def pending_actions_text(self) -> str:
        actions = self.store.load().get("actions", {})
        pending = [value for value in actions.values() if value.get("status") == "pending"]
        if not pending:
            return "✅ No intrusive network actions are pending approval."
        return "\n".join(
            ["🔐 Pending network actions", ""]
            + [f"• {item['id']}: {item['description']}" for item in pending]
        )

    def decide_action(self, action_id: str, approve: bool) -> str:
        state = self.store.load()
        action = state.get("actions", {}).get(action_id)
        if not action:
            return f"No network action found with ID {action_id}."
        if action.get("status") != "pending":
            return f"Action {action_id} is already {action.get('status')}."
        action["status"] = "approved" if approve else "rejected"
        action["decided_at"] = _now_iso()
        self.store.save(state)
        if approve:
            return (
                f"Action {action_id} approved, but not executed. "
                "No router-specific mutating executor is installed in this read-only release."
            )
        return f"Action {action_id} rejected. Nothing was changed."

    def mark_summary_sent(self, local_date: str) -> None:
        state = self.store.load()
        state["last_summary_date"] = local_date
        self.store.save(state)


class GuardianScheduler:
    def __init__(
        self,
        guardian: NetworkGuardian,
        send: Callable[[str], Awaitable[None]],
        timezone: str = "Asia/Singapore",
        scan_interval_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
        speed_test_runner: SpeedTestRunner | None = None,
    ):
        self.guardian = guardian
        self.send = send
        self.timezone = ZoneInfo(timezone)
        self.scan_interval_seconds = max(30, scan_interval_seconds)
        self.now = now or (lambda: datetime.now(self.timezone))
        self.speed_test_runner = speed_test_runner or SpeedTestRunner()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def tick(self) -> None:
        current = self.now().astimezone(self.timezone)
        today = current.date().isoformat()
        if current.hour == 8 and current.minute >= 30:
            attempts = self.guardian.speed_attempts_on(today, self.timezone)
            should_run = not attempts or (
                current.minute >= 40
                and len(attempts) < 2
                and not any(item.get("success") for item in attempts)
            )
            if should_run:
                result = await asyncio.to_thread(self.speed_test_runner.run, current)
                self.guardian.record_speed_test(result)

        alerts = await self.guardian.scan()
        for alert in alerts:
            await self.send(alert.telegram_text())
        state = self.guardian.store.load()
        if current.hour >= 9 and state.get("last_summary_date") != today:
            await self.send(self.guardian.summary_text(current))
            self.guardian.mark_summary_sent(today)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The host application owns logging; one failed scan must not stop monitoring.
                pass
            current = self.now().astimezone(self.timezone)
            schedule_times = [
                current.replace(hour=8, minute=30, second=0, microsecond=0),
                current.replace(hour=8, minute=40, second=0, microsecond=0),
                current.replace(hour=9, minute=0, second=0, microsecond=0),
                (current + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0),
            ]
            next_schedule = min(item for item in schedule_times if item > current)
            until_schedule = max(1.0, (next_schedule - current).total_seconds())
            wait_seconds = min(self.scan_interval_seconds, until_schedule)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=wait_seconds)
            except TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="network-guardian")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await self._task
