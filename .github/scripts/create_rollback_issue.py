#!/usr/bin/env python3
"""Create a GitHub issue when auto-rollback cannot git revert. Env: see main."""
from __future__ import annotations

import json
import os
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: create_rollback_issue.py <full-message-file>", file=sys.stderr)
        sys.exit(2)

    full_msg = Path(sys.argv[1]).read_text(encoding="utf-8")
    sha = os.environ["ISSUE_SHA"]
    subject = os.environ["ISSUE_SUBJECT"]
    run_url = os.environ.get("ISSUE_WORKFLOW_URL", "(not available)")
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    title = f"Auto-rollback failed: could not revert {sha[:7]}"
    fence = "~~~~" if "```" in full_msg else "```"
    body = textwrap.dedent(
        f"""
        ## Auto-rollback could not complete

        The workflow tried to revert the commit that failed CI on `main`, but **`git revert` failed** (often merge conflicts or a dirty history). Please revert manually or fix `main` as needed.

        ### Failed commit
        - **SHA:** `{sha}`
        - **Subject:** {subject}
        - **Failed CI workflow run:** {run_url}

        ### Full commit message
        {fence}
        {full_msg.rstrip()}
        {fence}
        """
    ).strip()

    payload = json.dumps({"title": title, "body": body}).encode("utf-8")
    req = urllib.request.Request(
        f"{api}/repos/{repo}/issues",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            out = json.loads(resp.read().decode())
            print(f"Created issue #{out.get('number')}: {out.get('html_url')}")
    except urllib.error.HTTPError as e:
        print(e.read().decode(), file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
