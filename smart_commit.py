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

# One line: type(optional-scope): subject — subject is short, imperative, no prose.
COMMIT_LINE_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|chore|ci|build)"
    r"(\([^)]{1,48}\))?"
    r":\s[^\s].{0,88}$"
)

OLLAMA_OPTIONS = {"temperature": 0.2, "num_predict": 80}


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


def fallback_from_stat(cwd: str) -> str:
    r = subprocess.run(
        ["git", "diff", "--cached", "--stat"],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return "chore: update repository"
    m = re.search(r"(\d+) files? changed", r.stdout)
    if m:
        n = m.group(1)
        return f"chore: update {n} files"
    return "chore: update repository"


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


def is_valid_conventional_line(line: str) -> bool:
    if len(line) > 100 or len(line) < 8:
        return False
    if not COMMIT_LINE_RE.match(line):
        return False
    _, _, subject = line.partition(":")
    subject = subject.strip().lower()
    words = subject.split()
    if len(words) > 14:
        return False
    # Block essay-style openings the model often produces
    prose_starts = (
        "this ",
        "these ",
        "the following",
        "the set",
        "running ",
        "here ",
    )
    if any(subject.startswith(p) for p in prose_starts):
        return False
    if " introduces" in subject or " including" in subject or " several " in subject:
        return False
    return True


def ollama_line(messages: list[dict]) -> str:
    try:
        resp = ollama.chat(
            model=MODEL,
            messages=messages,
            options=OLLAMA_OPTIONS,
        )
    except Exception as e:
        print(f"smart_commit: Ollama error ({MODEL}): {e}", file=sys.stderr)
        sys.exit(1)
    content = (resp.get("message") or {}).get("content") or ""
    return normalize_line(content)


def generate_message(diff: str, cwd: str) -> str:
    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[diff truncated]\n"

    system_strict = (
        "Output a single git commit title only. No preamble. No explanation. No bullet points. "
        "Format MUST be exactly: type: few-word subject\n"
        "type is one of: feat, fix, docs, style, refactor, perf, test, chore, ci, build\n"
        "Optional scope in parentheses: feat(api): subject\n"
        "Subject: imperative mood, lowercase, max ~10 words, no period at end, no 'this' or 'these'."
    )
    user = "Staged diff:\n\n" + diff + ("\n\n(diff truncated)\n" if truncated else "")

    line = ollama_line(
        [
            {"role": "system", "content": system_strict},
            {"role": "user", "content": user},
        ]
    )
    if is_valid_conventional_line(line):
        return line

    retry_user = (
        "Invalid. Reply with ONE LINE ONLY matching this pattern (replace words):\n"
        "feat: add health monitor\n"
        "or: fix: correct scheduler window\n"
        "Max 12 words after the colon. No other text.\n\n"
        "Same diff summary — pick the best type and a short subject:\n\n"
        + diff[:8000]
    )
    line2 = ollama_line(
        [
            {"role": "system", "content": system_strict},
            {"role": "user", "content": retry_user},
        ]
    )
    if is_valid_conventional_line(line2):
        return line2

    fb = fallback_from_stat(cwd)
    print(
        f"smart_commit: model output not conventional; using fallback: {fb!r}",
        file=sys.stderr,
    )
    return fb


def main() -> None:
    root = git_root()
    diff = staged_diff(root)
    if not diff.strip():
        print("smart_commit: no staged changes (git add first)", file=sys.stderr)
        sys.exit(1)

    msg = generate_message(diff, root).splitlines()[0].strip()
    print(msg)


if __name__ == "__main__":
    main()
