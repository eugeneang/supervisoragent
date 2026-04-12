"""
Watch the daily AI news digest: after push time + 10 minutes (Singapore by default),
if today's digest was not recorded in ai_news_config.json, alert and run recovery.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from datetime import time as time_type
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

CONFIG_FILE = Path("/Users/eugene/Agents/supervisoragent/ai_news_config.json")
PROJECT_ROOT = CONFIG_FILE.parent
AI_NEWS_PUSH = PROJECT_ROOT / "ai_news_push.py"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

LOG = logging.getLogger("health_monitor")

# Long-running: news fetch + Ollama summarization
RECOVERY_TIMEOUT_SEC = 900
TELEGRAM_MAX_LEN = 4000


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [health_monitor] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    LOG.setLevel(logging.INFO)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "enabled": True,
        "daily_push_time": "09:00",
        "timezone": "Asia/Singapore",
        "last_sent_date": "",
    }


def resolve_timezone(name: str) -> ZoneInfo:
    raw = (name or "Asia/Singapore").strip()
    try:
        return ZoneInfo(raw)
    except Exception:
        LOG.warning("Invalid timezone %r; using Asia/Singapore", raw)
        return ZoneInfo("Asia/Singapore")


def parse_push_time(target_raw: str) -> time_type:
    s = str(target_raw).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        raise ValueError(f"invalid daily_push_time: {target_raw!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"hour/minute out of range: {target_raw!r}")
    return time_type(hour=h, minute=mi)


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set")

    body = text if len(text) <= TELEGRAM_MAX_LEN else text[: TELEGRAM_MAX_LEN - 20] + "\n…(truncated)"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": body}

    try:
        r = requests.post(url, json=payload, timeout=30)
    except requests.RequestException as e:
        LOG.error("Telegram request failed: %s", e, exc_info=True)
        raise

    if not r.ok:
        LOG.error("Telegram HTTP %s: %s", r.status_code, (r.text or "")[:2000])
        r.raise_for_status()

    data = r.json()
    if not data.get("ok"):
        LOG.error("Telegram API ok=false: %s", json.dumps(data)[:2000])
        raise RuntimeError("Telegram sendMessage failed")


def threshold_datetime(now: datetime, push_t: time_type, tz: ZoneInfo) -> datetime:
    """First instant we consider a 'miss' check: push wall time + 10 minutes (same TZ)."""
    base = datetime.combine(now.date(), push_t, tzinfo=tz)
    return base + timedelta(minutes=10)


def main() -> None:
    setup_logging()
    LOG.info("Health monitor run started")

    config = load_config()
    if not config.get("enabled", True):
        LOG.info("AI news push disabled in config; nothing to monitor")
        return

    tz = resolve_timezone(config.get("timezone", "Asia/Singapore"))
    now = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")

    try:
        push_t = parse_push_time(config.get("daily_push_time", "09:00"))
    except ValueError as e:
        LOG.error("Bad config daily_push_time: %s", e)
        return

    cutoff = threshold_datetime(now, push_t, tz)
    if now < cutoff:
        LOG.info(
            "Before miss-check window (now=%s, need >= %s %s); exiting",
            now.strftime("%H:%M"),
            cutoff.strftime("%H:%M"),
            tz.key,
        )
        return

    last_sent = (config.get("last_sent_date") or "").strip()
    if last_sent == today:
        LOG.info("Digest already recorded for today (%s); OK", today)
        return

    LOG.warning(
        "Missed digest detected: last_sent_date=%r today=%s (after %s %s)",
        last_sent,
        today,
        cutoff.strftime("%H:%M"),
        tz.key,
    )

    try:
        send_telegram(
            f"⚠️ Health monitor: daily AI news digest was not recorded for {today} "
            f"after {cutoff.strftime('%H:%M')} ({tz.key}). Attempting recovery with "
            f"`python ai_news_push.py --force`…"
        )
    except Exception as e:
        LOG.error("Failed to send missed-digest alert: %s", e, exc_info=True)

    python_exe = sys.executable
    cmd = [python_exe, str(AI_NEWS_PUSH), "--force"]
    LOG.info("Running recovery: %s (cwd=%s)", cmd, PROJECT_ROOT)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=RECOVERY_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        LOG.error("Recovery timed out after %s s", RECOVERY_TIMEOUT_SEC)
        try:
            send_telegram(
                f"❌ Health monitor: recovery timed out after {RECOVERY_TIMEOUT_SEC // 60} minutes "
                f"for {today}. Check Ollama / network / ai_news_push.log."
            )
        except Exception as e:
            LOG.error("Failed to send timeout Telegram: %s", e, exc_info=True)
        sys.exit(1)
    except Exception as e:
        LOG.exception("Recovery subprocess failed to run: %s", e)
        try:
            send_telegram(f"❌ Health monitor: could not start recovery: {e!s}"[:TELEGRAM_MAX_LEN])
        except Exception:
            pass
        sys.exit(1)

    out_tail = (proc.stdout or "")[-2500:]
    err_tail = (proc.stderr or "")[-2500:]
    if proc.stdout:
        LOG.info("ai_news_push stdout (tail):\n%s", out_tail)
    if proc.stderr:
        LOG.warning("ai_news_push stderr (tail):\n%s", err_tail)

    config_after = load_config()
    sent_now = (config_after.get("last_sent_date") or "").strip() == today

    if proc.returncode == 0 and sent_now:
        LOG.info("Recovery succeeded (exit 0, last_sent_date=%s)", today)
        try:
            send_telegram(
                f"✅ Health monitor: recovery succeeded for {today}. "
                f"Daily AI news digest was sent and config updated."
            )
        except Exception as e:
            LOG.error("Failed to send success Telegram: %s", e, exc_info=True)
        return

    summary = (
        f"exit_code={proc.returncode}\n"
        f"last_sent_date_after={config_after.get('last_sent_date')!r}\n"
        f"stderr_tail:\n{err_tail or '(empty)'}\n"
        f"stdout_tail:\n{out_tail or '(empty)'}"
    )
    LOG.error("Recovery failed or config not updated:\n%s", summary[:5000])
    try:
        send_telegram(
            "❌ Health monitor: recovery failed for "
            f"{today}.\n\n{summary[:3500]}"
        )
    except Exception as e:
        LOG.error("Failed to send failure Telegram: %s", e, exc_info=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
