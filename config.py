import os

SMOKE_LOG_PATH = os.getenv("SMOKE_LOG_PATH", "logs/smoke_tests.log")
GIT_REPO_PATH = os.getenv("GIT_REPO_PATH", ".")
