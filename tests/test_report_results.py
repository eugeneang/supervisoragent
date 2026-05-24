"""Unit tests for format_summary in tests/report_results.py."""

from tests.report_results import TestResult, format_summary


def test_format_summary_all_pass():
    results = [
        TestResult(command="/ping", description="liveness", passed=True, detail="pong"),
        TestResult(command="/help", description="help text", passed=True, detail="Available commands"),
    ]
    summary = format_summary(results)
    assert "2/2 passed" in summary
    assert "\u2705" in summary   # ✅
    assert "\u274c" not in summary  # no ❌


def test_format_summary_some_fail():
    results = [
        TestResult(command="/ping", description="liveness", passed=True, detail="pong"),
        TestResult(
            command="/ai",
            description="ai news",
            passed=False,
            detail="No reply within 45s",
        ),
    ]
    summary = format_summary(results)
    assert "1/2 passed" in summary
    assert "\u274c" in summary   # ❌
    assert "No reply within 45s" in summary


def test_format_summary_empty_results():
    summary = format_summary([])
    assert "0/0 passed" in summary


def test_format_summary_includes_command_names():
    results = [
        TestResult(command="/build_status", description="status check", passed=True),
    ]
    summary = format_summary(results)
    assert "/build_status" in summary


def test_format_summary_shows_attempt_count():
    results = [
        TestResult(command="/design", description="proposal", passed=True, attempts=2),
    ]
    summary = format_summary(results)
    assert "attempt 2" in summary
