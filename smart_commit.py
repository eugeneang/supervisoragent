#!/usr/bin/env python3
"""
Read staged git diff and print one conventional-commit line via local Ollama.
"""
from __future__ import annotations

import re
import subprocess
import sys

import ollama

MODEL = "qwen2.5:7b"
MAX_DIFF_CHARS = 100_000


def git_root() -> str:
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        print("smart_commit: not a git repository", file=sys.stderr)
        sys.exit(1)
    return r.stdout.strip()


def staged_diff(cwd: str) -> str:
    r = subprocess.run(
        ["git", "diff", "--cached", "--no-color"],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    if r.returncode != 0:
        print("smart_commit: git diff --cached failed", file=sys.stderr)
        sys.exit(1)
    return r.stdout


def normalize_line(raw: str) -> str:
    if not (raw or "").strip():
        return ""
    line = raw.strip().splitlines()[0].strip()
    line = re.sub(r"^[`'\"]+|[`'\"]+$", "", line)
    if ":" in line:
        head, _, tail = line.partition(":")
        head, tail = head.strip(), tail.strip()
        m = re.match(r"^([A-Za-z]+)(\(.*\))?$", head)
        if m:
            head = m.group(1).lower() + (m.group(2) or "")
            line = f"{head}: {tail}" if tail else head
    return line.strip()


def generate_message(diff: str) -> str:
    truncated = False
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[diff truncated]\n"
        truncated = True

    system = (
        "You write git commit messages. Reply with EXACTLY ONE LINE and nothing else: "
        "a Conventional Commits title: type(optional-scope): imperative lowercase subject. "
        "No period at end. No quotes or backticks. Max ~72 chars for the subject. "
        "Allowed types: feat, fix, docs, style, refactor, perf, test, chore, ci, build."
    )
    user = (
        "Staged git diff:\n\n"
        + diff
        + ("\n\nNote: diff was truncated.\n" if truncated else "")
    )

    try:
        resp = ollama.chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception as e:
        print(f"smart_commit: Ollama error ({MODEL}): {e}", file=sys.stderr)
        sys.exit(1)

    content = (resp.get("message") or {}).get("content") or ""
    line = normalize_line(content)
    if not line:
        print("smart_commit: model returned an empty message", file=sys.stderr)
        sys.exit(1)
    return line


def main() -> None:
    root = git_root()
    diff = staged_diff(root)
    if not diff.strip():
        print("smart_commit: no staged changes (git add first)", file=sys.stderr)
        sys.exit(1)

    msg = generate_message(diff).splitlines()[0].strip()
    print(msg)


if __name__ == "__main__":
    main()
