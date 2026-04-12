#!/usr/bin/env python3
"""
Telegram sendMessage helper for GitHub Actions.

Modes (argv[1]):
  ci-failed-main     — CI failed on main (env: GITHUB_*, git available)
  rollback-success   — auto-revert pushed (env: FAILED_SHA, WORKFLOW_RUN_URL, GITHUB_*)
  rollback-failed    — revert failed, issue filed (env: FAILED_SHA, WORKFLOW_RUN_URL, GITHUB_*)

No argv / stdin: send stdin or argv[1+] as message (legacy).

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (skip quietly if missing).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

MAX_LEN = 4000


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, cwd=".").strip()


def send_text(text: str) -> None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        print("telegram_notify: TELEGRAM_* secrets unset; skipping send", file=sys.stderr)
        return

    text = text.strip()
    if not text:
        return
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 1] + "…"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            if not body.get("ok"):
                print(f"telegram_notify: API ok=false {body}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        print(f"telegram_notify: HTTP {e.code} {e.read().decode()[:500]}", file=sys.stderr)
    except Exception as e:
        print(f"telegram_notify: {e}", file=sys.stderr)


def message_ci_failed_main() -> str:
    sha = os.environ["GITHUB_SHA"]
    repo = os.environ["GITHUB_REPOSITORY"]
    subj = _git("log", "-1", "--pretty=%s", sha)
    body = _git("log", "-1", "--pretty=%B", sha)[:900]
    run_url = (
        f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/"
        f"{os.environ['GITHUB_RUN_ID']}"
    )
    return (
        "🔴 CI failed on main\n\n"
        f"Repo: {repo}\n"
        "Branch: main\n"
        "Workflow: CI\n"
        f"Commit: {sha}\n"
        f"Message: {subj}\n\n"
        f"Commit body (trimmed):\n{body}\n\n"
        f"Run: {run_url}"
    )


def message_rollback_success() -> str:
    repo = os.environ["GITHUB_REPOSITORY"]
    failed_sha = os.environ["FAILED_SHA"]
    subj = _git("log", "-1", "--pretty=%s", failed_sha)
    new_sha = _git("rev-parse", "HEAD")
    ci_run = os.environ["WORKFLOW_RUN_URL"]
    rb_run = (
        f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/"
        f"{os.environ['GITHUB_RUN_ID']}"
    )
    return (
        "✅ Auto-rollback succeeded\n\n"
        "The failed commit was reverted on main.\n\n"
        f"Repo: {repo}\n"
        "Branch: main\n"
        "Workflow: Auto Rollback on CI Failure\n"
        f"Reverted commit: {failed_sha}\n"
        f"Its message: {subj}\n"
        f"New HEAD: {new_sha}\n"
        f"Failed CI run: {ci_run}\n"
        f"Rollback run: {rb_run}"
    )


def message_rollback_failed() -> str:
    repo = os.environ["GITHUB_REPOSITORY"]
    failed_sha = os.environ["FAILED_SHA"]
    subj = _git("log", "-1", "--pretty=%s", failed_sha)
    ci_run = os.environ["WORKFLOW_RUN_URL"]
    rb_run = (
        f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/"
        f"{os.environ['GITHUB_RUN_ID']}"
    )
    return (
        "⚠️ Auto-rollback failed — manual attention needed\n\n"
        "Could not complete git revert; a GitHub issue was opened.\n\n"
        f"Repo: {repo}\n"
        "Branch: main\n"
        "Workflow: Auto Rollback on CI Failure\n"
        f"Failed commit: {failed_sha}\n"
        f"Message: {subj}\n"
        f"Failed CI run: {ci_run}\n"
        f"Rollback run: {rb_run}"
    )


def main() -> None:
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        if mode == "ci-failed-main":
            send_text(message_ci_failed_main())
            return
        if mode == "rollback-success":
            send_text(message_rollback_success())
            return
        if mode == "rollback-failed":
            send_text(message_rollback_failed())
            return
        send_text("\n".join(sys.argv[1:]))
        return

    text = sys.stdin.read()
    send_text(text)


if __name__ == "__main__":
    main()
    sys.exit(0)
