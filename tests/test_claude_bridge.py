"""Unit tests for ClaudeBridge helpers in claude_bridge.py.

Tests _parse_proposal, _parse_file_blocks, and _apply_edits without making
any real Anthropic API calls. REPO_ROOT is redirected to tmp_path for
_apply_edits so nothing in the live repo is written to or deleted.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_bridge as cb
from claude_bridge import ClaudeBridge


# ---------------------------------------------------------------------------
# Fixture — create a ClaudeBridge without a real API key or HTTP client
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-unit-tests")
    with patch("anthropic.Anthropic"):
        return ClaudeBridge()


# ---------------------------------------------------------------------------
# _parse_proposal
# ---------------------------------------------------------------------------

def test_parse_proposal_extracts_feature_name(bridge):
    raw = "FEATURE_NAME: My Cool Feature\n\n## Design Proposal\nSome content."
    name, text = bridge._parse_proposal(raw, "fallback request")
    assert name == "my_cool_feature"
    assert "## Design Proposal" in text
    assert "FEATURE_NAME" not in text


def test_parse_proposal_normalises_hyphens_to_underscores(bridge):
    raw = "FEATURE_NAME: add-search-endpoint\n\nBody."
    name, _ = bridge._parse_proposal(raw, "fallback")
    assert name == "add_search_endpoint"
    assert "-" not in name


def test_parse_proposal_falls_back_to_request_words_when_no_feature_name(bridge):
    raw = "## Design Proposal\nNo FEATURE_NAME line here."
    name, text = bridge._parse_proposal(raw, "add search endpoint now")
    assert name  # non-empty
    assert "add" in name
    assert text  # proposal body preserved


def test_parse_proposal_empty_feature_name_uses_fallback(bridge):
    raw = "## Design Proposal\nContent."
    name, _ = bridge._parse_proposal(raw, "")
    # Falls back to "feature" when request is also empty
    assert name == "feature"


# ---------------------------------------------------------------------------
# _parse_file_blocks
# ---------------------------------------------------------------------------

def test_parse_file_blocks_single_file(bridge):
    raw = "=== FILE: foo.py ===\nprint('hi')\n=== END FILE ==="
    edits = bridge._parse_file_blocks(raw)
    assert len(edits) == 1
    assert edits[0]["path"] == "foo.py"
    assert edits[0]["content"] == "print('hi')\n"


def test_parse_file_blocks_multiple_files(bridge):
    raw = (
        "=== FILE: a.py ===\nx = 1\n=== END FILE ===\n\n"
        "=== FILE: b.py ===\ny = 2\n=== END FILE ==="
    )
    edits = bridge._parse_file_blocks(raw)
    assert len(edits) == 2
    assert edits[0]["path"] == "a.py"
    assert edits[1]["path"] == "b.py"


def test_parse_file_blocks_preserves_content_exactly(bridge):
    code = "def hello():\n    return 'world'\n"
    raw = f"=== FILE: hello.py ===\n{code}=== END FILE ==="
    edits = bridge._parse_file_blocks(raw)
    assert edits[0]["content"] == code


def test_parse_file_blocks_raises_on_unterminated_block(bridge):
    raw = "=== FILE: foo.py ===\nprint('hi')\n"
    with pytest.raises(ValueError, match="Unterminated"):
        bridge._parse_file_blocks(raw)


def test_parse_file_blocks_empty_input(bridge):
    assert bridge._parse_file_blocks("no blocks here") == []


def test_parse_file_blocks_strips_path_whitespace(bridge):
    raw = "=== FILE:   spaced.py   ===\nx=1\n=== END FILE ==="
    edits = bridge._parse_file_blocks(raw)
    assert edits[0]["path"] == "spaced.py"


# ---------------------------------------------------------------------------
# _apply_edits — safety checks
# ---------------------------------------------------------------------------

def test_apply_edits_rejects_path_traversal(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "REPO_ROOT", tmp_path)
    with pytest.raises(ValueError, match="escapes repo root"):
        bridge._apply_edits([{"path": "../../etc/passwd", "content": "bad"}])


def test_apply_edits_strips_leading_slash_and_writes_inside_repo(bridge, tmp_path, monkeypatch):
    """_apply_edits strips leading '/' before resolving so absolute-looking paths
    are treated as relative to REPO_ROOT rather than being rejected."""
    monkeypatch.setattr(cb, "REPO_ROOT", tmp_path)
    bridge._apply_edits([{"path": "/safe_module.py", "content": "x = 1"}])
    assert (tmp_path / "safe_module.py").read_text() == "x = 1"


def test_apply_edits_rejects_protected_file(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "REPO_ROOT", tmp_path)
    with pytest.raises(ValueError, match="protected file"):
        bridge._apply_edits([{"path": "claude_bridge.py", "content": "# evil"}])


def test_apply_edits_rejects_protected_prefix(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "REPO_ROOT", tmp_path)
    with pytest.raises(ValueError, match="protected path"):
        bridge._apply_edits([{"path": ".github/workflows/ci.yml", "content": "bad"}])


def test_apply_edits_writes_valid_file(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "REPO_ROOT", tmp_path)
    bridge._apply_edits([{"path": "new_feature.py", "content": "print('hello')"}])
    assert (tmp_path / "new_feature.py").read_text() == "print('hello')"


def test_apply_edits_creates_subdirectory(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "REPO_ROOT", tmp_path)
    bridge._apply_edits([{"path": "subdir/module.py", "content": "x = 1"}])
    assert (tmp_path / "subdir" / "module.py").read_text() == "x = 1"


def test_apply_edits_returns_changed_file_list(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "REPO_ROOT", tmp_path)
    changed = bridge._apply_edits([
        {"path": "a.py", "content": "a=1"},
        {"path": "b.py", "content": "b=2"},
    ])
    assert sorted(changed) == ["a.py", "b.py"]
