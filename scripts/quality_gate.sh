#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"

echo "quality-gate: syntax"
"$PYTHON" -m compileall -q -x '(^|/)(\.git|\.venv)/' .

echo "quality-gate: lint"
"$PYTHON" -m ruff check .

echo "quality-gate: tests"
"$PYTHON" -m pytest -q

echo "quality-gate: dependency vulnerabilities"
"$PYTHON" -m pip_audit -r requirements.txt

echo "quality-gate: application security"
"$PYTHON" -m bandit -q -r . \
  -x ./.git,./.venv,./tests,./__pycache__ \
  --severity-level medium --confidence-level medium

echo "quality-gate: PASS"
