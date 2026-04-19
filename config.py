import os
from pathlib import Path

# Absolute path to the supervisoragent repo root (the directory this file lives in).
_REPO_ROOT = Path(__file__).parent.resolve()

# Smoke test log is written one level up from the repo, in the shared Agents dir.
SMOKE_LOG_PATH = os.getenv("SMOKE_LOG_PATH", str(_REPO_ROOT.parent / "smoke_tests.log"))

# Repo root for git commands — always absolute so subprocess cwd is reliable.
GIT_REPO_PATH = os.getenv("GIT_REPO_PATH", str(_REPO_ROOT))
