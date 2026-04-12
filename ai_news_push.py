this is not valid python
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from agents.ai_news_agent import get_ai_news_digest

CONFIG_FILE = Path("/Users/eugene/Agents/supervisoragent/ai_news_config.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

LOG = logging.getLogger("ai_news_push")


def setup_logging() -> None:
    """Console logging for launchd (stdout → StandardOutPath)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [ai_news_push] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    LOG.setLevel(logging.DEBUG)


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "enabled": True,
        "daily_push_time": "09:00",
        "timezone": "Asia/Singapore",
        "last_sent_date": "",
    }


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def resolve_timezone(name: str) -> ZoneInfo:
    raw = (name or "UTC").strip()
    try:
        return ZoneInfo(raw)
    except Exception as e:
        LOG.error("Invalid timezone %r (%s); falling back to UTC", raw, e)
        return ZoneInfo("UTC")


def parse_daily_push_time(target_raw: str) -> time:
    """
    Accepts '09:00', '9:00', optional surrounding whitespace.
    """
    s = target_raw.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        raise ValueError(f"expected HH:MM, got {target_raw!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"hour/minute out of range: {target_raw!r}")
    return time(hour=hour, minute=minute)


def time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def after_or_at_push_time(now_local: datetime, target_t: time) -> tuple[bool, str]:
    """
    True when wall-clock time in now_local's zone is at or after daily_push_time
    (first eligible run that day; works with launchd every few minutes).
    """
    now_t = now_local.time()
    now_m = time_to_minutes(now_t)
    tgt_m = time_to_minutes(target_t)
    if now_m < tgt_m:
        return False, "before_push_time"
    return True, "at_or_after_push_time"


def should_send_now(config: dict, now: datetime, force: bool) -> bool:
    """
    Whether the schedule allows sending now. When force is True, always True.
    `now` must be timezone-aware (config timezone).
    """
    if force:
        LOG.info("should_send_now: force=True (schedule bypassed)")
        return True

    target_raw = str(config.get("daily_push_time", "09:00")).strip()
    try:
        target_t = parse_daily_push_time(target_raw)
    except ValueError as e:
        raise RuntimeError(
            f"Invalid daily_push_time format in config: {target_raw!r}. "
            "Use HH:MM, e.g. 09:00 or 9:00"
        ) from e

    ok, reason = after_or_at_push_time(now, target_t)
    LOG.info(
        "should_send_now: now=%s target=%s -> %s (%s)",
        now.strftime("%H:%M"),
        target_t.strftime("%H:%M"),
        ok,
        reason,
    )
    return ok


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        msg = "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set"
        LOG.error(msg)
        raise RuntimeError(msg)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    LOG.debug(
        "POST Telegram sendMessage chat_id=%s text_len=%d",
        TELEGRAM_CHAT_ID,
        len(text),
    )

    try:
        response = requests.post(url, json=payload, timeout=30)
    except requests.RequestException as e:
        LOG.error("Telegram request failed: %s", e, exc_info=True)
        raise

    if not response.ok:
        body_preview = (response.text or "")[:2000]
        LOG.error(
            "Telegram API HTTP %s: %s",
            response.status_code,
            body_preview,
        )
        response.raise_for_status()

    try:
        data = response.json()
    except json.JSONDecodeError:
        LOG.warning("Telegram response not JSON: %s", response.text[:500])
        return

    if not data.get("ok"):
        LOG.error("Telegram sendMessage ok=false: %s", json.dumps(data)[:2000])
        raise RuntimeError(f"Telegram API error: {data!r}")

    mid = (data.get("result") or {}).get("message_id")
    LOG.info("Telegram sendMessage OK message_id=%s", mid)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Daily AI news digest → Telegram")
    parser.add_argument(
        "--force",
        "--send-now",
        action="store_true",
        help="Bypass disabled flag, 'already sent today', and schedule; still sends for real unless --dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build digest and print it; do not call Telegram or update last_sent_date.",
    )
    args = parser.parse_args()

    config = load_config()
    tz = resolve_timezone(config.get("timezone", "UTC"))
    now = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    force = bool(args.force)
    dry_run = bool(args.dry_run)

    last_sent = (config.get("last_sent_date") or "").strip()

    LOG.info(
        "Run start: now=%s (%s) today=%s last_sent_date=%r force=%s dry_run=%s enabled=%s",
        now.isoformat(),
        tz.key,
        today,
        last_sent,
        force,
        dry_run,
        config.get("enabled", True),
    )

    if not config.get("enabled", True) and not force:
        LOG.info("AI news push disabled in config; exiting")
        return

    if last_sent == today and not force:
        if dry_run:
            LOG.info("Dry-run: bypassing already-sent-today check")
        else:
            LOG.info("Already sent today")
            return

    if not should_send_now(config, now, force):
        LOG.info("Not within scheduled window")
        return

    try:
        LOG.info("Building digest via get_ai_news_digest() …")
        digest = get_ai_news_digest()
        message = "Daily AI News Digest\n\n" + digest
        LOG.info("Digest ready, length=%d chars", len(digest))

        if dry_run:
            print(message)
            LOG.info("Dry-run: printed digest to stdout; no Telegram, no last_sent_date update")
            return

        send_telegram_message(message)
        config["last_sent_date"] = today
        save_config(config)
        LOG.info("AI news digest sent; last_sent_date=%s", today)
    except Exception as e:
        LOG.exception("Failed to send AI news digest: %s", e)
        raise


if __name__ == "__main__":
    main()
