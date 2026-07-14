"""Unit tests for Telegram command handlers in telegram_bot.py.

Covers:
- _parse_ts, _window_history, _trim_store   (history helpers)
- build_messages                             (system prompt shape, ts stamps)
- append_assistant_reply                     (ts stamp, legacy-system strip)
- handle_message                             (tool-use path, Anthropic client called)
- is_user_allowed                            (whitelist logic)
- /help, /id, /start, /clear, /clear all     (command handlers)
- /remember, /memory, /forget               (explicit memory commands)
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import telegram_bot as tb
from telegram_bot import (
    HELP_SECTIONS,
    _parse_ts,
    _window_history,
    _trim_store,
    append_assistant_reply,
    build_messages,
    clear_command,
    forget_command,
    help_command,
    is_user_allowed,
    load_user_memory,
    memory_command,
    remember_command,
    save_user_memory,
    show_id,
    start,
)


def test_help_catalog_covers_every_registered_command():
    source = (Path(tb.__file__)).read_text(encoding="utf-8")
    registered = set(re.findall(r'CommandHandler\("([a-z_]+)"', source))
    documented = {
        command.split()[0]
        for _, commands in HELP_SECTIONS
        for command, _ in commands
    }
    assert documented == registered


_SGT = ZoneInfo("Asia/Singapore")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def whitelist_file(tmp_path, monkeypatch):
    wf = tmp_path / "whitelist.json"
    monkeypatch.setattr(tb, "WHITELIST_FILE", wf)
    return wf


@pytest.fixture
def conversation_file(tmp_path, monkeypatch):
    cf = tmp_path / "conversation_store.json"
    monkeypatch.setattr(tb, "CONVERSATION_FILE", cf)
    return cf


@pytest.fixture
def memory_store_file(tmp_path, monkeypatch):
    mf = tmp_path / "memory_store.json"
    monkeypatch.setattr(tb, "MEMORY_STORE_FILE", mf)
    return mf


def _make_update(user_id: int = 12345, first_name: str = "Alice") -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = first_name
    update.effective_chat.id = 99999
    update.message.reply_text = AsyncMock()
    update.message.text = "hello"
    return update


def _ts(hours_ago: float = 0) -> str:
    return (datetime.now(_SGT) - timedelta(hours=hours_ago)).isoformat()


# ---------------------------------------------------------------------------
# _parse_ts
# ---------------------------------------------------------------------------

def test_parse_ts_valid_iso():
    ts = datetime.now(_SGT).isoformat()
    result = _parse_ts(ts)
    assert abs((result - datetime.now(_SGT)).total_seconds()) < 5


def test_parse_ts_none_returns_epoch():
    assert _parse_ts(None).year == 1970


def test_parse_ts_invalid_string_returns_epoch():
    assert _parse_ts("not-a-date").year == 1970


def test_parse_ts_empty_string_returns_epoch():
    assert _parse_ts("").year == 1970


# ---------------------------------------------------------------------------
# _window_history
# ---------------------------------------------------------------------------

def test_window_history_excludes_messages_older_than_72h():
    history = [
        {"role": "user", "content": "old", "ts": _ts(hours_ago=73)},
        {"role": "assistant", "content": "old reply", "ts": _ts(hours_ago=72.1)},
    ]
    assert _window_history(history) == []


def test_window_history_includes_recent_messages():
    history = [
        {"role": "user", "content": "recent", "ts": _ts(hours_ago=1)},
        {"role": "assistant", "content": "reply", "ts": _ts(hours_ago=0.9)},
    ]
    assert len(_window_history(history)) == 2


def test_window_history_mixes_old_and_new():
    history = [
        {"role": "user", "content": "old", "ts": _ts(hours_ago=80)},
        {"role": "user", "content": "new", "ts": _ts(hours_ago=1)},
    ]
    result = _window_history(history)
    assert len(result) == 1
    assert result[0]["content"] == "new"


def test_window_history_caps_at_max_messages():
    history = [
        {"role": "user", "content": str(i), "ts": _ts(hours_ago=i * 0.1)}
        for i in range(30)
    ]
    assert len(_window_history(history, max_messages=20)) == 20


def test_window_history_keeps_most_recent_when_capping():
    history = [
        {"role": "user", "content": str(i), "ts": _ts(hours_ago=(30 - i) * 0.5)}
        for i in range(30)
    ]
    result = _window_history(history, max_messages=5)
    assert result[-1]["content"] == "29"


def test_window_history_excludes_system_messages():
    history = [
        {"role": "system", "content": "sys", "ts": _ts()},
        {"role": "user", "content": "hi", "ts": _ts()},
    ]
    assert all(m["role"] != "system" for m in _window_history(history))


def test_window_history_empty_input():
    assert _window_history([]) == []


def test_window_history_messages_without_ts_are_excluded():
    assert _window_history([{"role": "user", "content": "no ts"}]) == []


# ---------------------------------------------------------------------------
# _trim_store
# ---------------------------------------------------------------------------

def test_trim_store_keeps_last_50():
    history = [{"role": "user", "content": str(i), "ts": _ts()} for i in range(60)]
    result = _trim_store(history)
    assert len(result) == 50
    assert result[-1]["content"] == "59"


def test_trim_store_strips_system_messages():
    history = [
        {"role": "system", "content": "old system"},
        {"role": "user", "content": "hi", "ts": _ts()},
    ]
    assert all(m["role"] != "system" for m in _trim_store(history))


def test_trim_store_under_limit_unchanged():
    history = [{"role": "user", "content": str(i), "ts": _ts()} for i in range(10)]
    assert _trim_store(history) == history


# ---------------------------------------------------------------------------
# build_messages
# ---------------------------------------------------------------------------

def test_build_messages_returns_system_and_messages_tuple(
    conversation_file, memory_store_file
):
    system, messages = build_messages("u1", "hello")
    assert isinstance(system, str)
    assert isinstance(messages, list)


def test_build_messages_no_system_role_in_messages(
    conversation_file, memory_store_file
):
    _, messages = build_messages("u1", "hello")
    assert all(m["role"] != "system" for m in messages)


def test_build_messages_stores_ts_on_user_message(
    conversation_file, memory_store_file
):
    build_messages("u1", "hello")
    store = json.loads(conversation_file.read_text())
    assert "ts" in store["u1"][-1]


def test_build_messages_strips_legacy_system_from_store(
    conversation_file, memory_store_file
):
    conversation_file.write_text(json.dumps({
        "u1": [
            {"role": "system", "content": "old system prompt"},
            {"role": "user", "content": "prev", "ts": _ts(hours_ago=1)},
        ]
    }))
    _, messages = build_messages("u1", "new message")
    assert all(m["role"] != "system" for m in messages)


def test_build_messages_datetime_in_system_prompt(
    conversation_file, memory_store_file
):
    system, _ = build_messages("u1", "what time is it")
    assert "Current date and time" in system
    assert "SGT" in system


def test_build_messages_user_message_not_enriched_with_datetime(
    conversation_file, memory_store_file
):
    """Datetime must be in system prompt only, not in user message content."""
    _, messages = build_messages("u1", "what time is it")
    last_user = next(m for m in reversed(messages) if m["role"] == "user")
    assert "Current date and time" not in last_user["content"]


def test_build_messages_api_messages_have_no_ts_field(
    conversation_file, memory_store_file
):
    _, messages = build_messages("u1", "hello")
    for m in messages:
        assert "ts" not in m


def test_build_messages_memory_injected_in_system_prompt(
    conversation_file, memory_store_file
):
    memory_store_file.write_text(json.dumps({
        "u1": [{"text": "I prefer short replies", "saved_at": _ts()}]
    }))
    system, _ = build_messages("u1", "hi")
    assert "I prefer short replies" in system


# ---------------------------------------------------------------------------
# append_assistant_reply
# ---------------------------------------------------------------------------

def test_append_assistant_reply_adds_ts(conversation_file):
    conversation_file.write_text(json.dumps({
        "u1": [{"role": "user", "content": "hi", "ts": _ts(hours_ago=1)}]
    }))
    append_assistant_reply("u1", "hello back")
    store = json.loads(conversation_file.read_text())
    last = store["u1"][-1]
    assert last["role"] == "assistant"
    assert "ts" in last


def test_append_assistant_reply_strips_legacy_system(conversation_file):
    conversation_file.write_text(json.dumps({
        "u1": [
            {"role": "system", "content": "old"},
            {"role": "user", "content": "hi", "ts": _ts(hours_ago=1)},
        ]
    }))
    append_assistant_reply("u1", "reply")
    store = json.loads(conversation_file.read_text())
    assert all(m["role"] != "system" for m in store["u1"])


def test_append_assistant_reply_empty_store(conversation_file):
    append_assistant_reply("u1", "hello")
    store = json.loads(conversation_file.read_text())
    assert store["u1"][0]["role"] == "assistant"


# ---------------------------------------------------------------------------
# handle_message — Anthropic tool-use path
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_chat_with_tools(monkeypatch):
    """Patch _chat_with_tools so no real API call is made."""
    async def fake_chat(system_prompt, messages):
        return "mocked reply from Haiku"
    monkeypatch.setattr(tb, "_chat_with_tools", fake_chat)
    return fake_chat


async def test_handle_message_replies_with_model_output(
    whitelist_file, conversation_file, memory_store_file, mock_chat_with_tools
):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    update = _make_update(user_id=12345)
    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    await tb.handle_message(update, ctx)
    update.message.reply_text.assert_called_once_with("mocked reply from Haiku")


async def test_handle_message_unauthorized_user_gets_rejection(
    whitelist_file, conversation_file, memory_store_file, mock_chat_with_tools
):
    whitelist_file.write_text(json.dumps({"allowed_users": [111, 222]}))
    update = _make_update(user_id=999)
    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    await tb.handle_message(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "not authorized" in reply.lower()


async def test_handle_message_stores_reply_in_history(
    whitelist_file, conversation_file, memory_store_file, mock_chat_with_tools
):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    update = _make_update(user_id=12345)
    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    await tb.handle_message(update, ctx)
    store = json.loads(conversation_file.read_text())
    assert any(m["role"] == "assistant" for m in store["telegram:12345"])


# ---------------------------------------------------------------------------
# Explicit memory: /remember, /memory, /forget
# ---------------------------------------------------------------------------

async def test_remember_command_saves_note(whitelist_file, memory_store_file):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    update = _make_update(user_id=12345)
    ctx = MagicMock()
    ctx.args = ["I", "prefer", "short", "replies"]
    await remember_command(update, ctx)
    notes = load_user_memory("telegram:12345")
    assert any("I prefer short replies" in n["text"] for n in notes)
    reply = update.message.reply_text.call_args[0][0]
    assert "I prefer short replies" in reply


async def test_remember_command_no_args(whitelist_file, memory_store_file):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    update = _make_update(user_id=12345)
    ctx = MagicMock()
    ctx.args = []
    await remember_command(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Usage" in reply


async def test_remember_command_appends_multiple(whitelist_file, memory_store_file):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    for text in [["note", "one"], ["note", "two"]]:
        update = _make_update(user_id=12345)
        ctx = MagicMock()
        ctx.args = text
        await remember_command(update, ctx)
    assert len(load_user_memory("telegram:12345")) == 2


async def test_memory_command_lists_notes(whitelist_file, memory_store_file):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    save_user_memory("telegram:12345", [
        {"text": "I like coffee", "saved_at": _ts()},
        {"text": "morning person", "saved_at": _ts()},
    ])
    update = _make_update(user_id=12345)
    ctx = MagicMock()
    await memory_command(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "[1]" in reply
    assert "[2]" in reply
    assert "I like coffee" in reply
    assert "morning person" in reply


async def test_memory_command_empty(whitelist_file, memory_store_file):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    update = _make_update(user_id=12345)
    ctx = MagicMock()
    await memory_command(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "No memories" in reply or "no memories" in reply.lower()


async def test_forget_command_removes_correct_note(whitelist_file, memory_store_file):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    save_user_memory("telegram:12345", [
        {"text": "note one", "saved_at": _ts()},
        {"text": "note two", "saved_at": _ts()},
    ])
    update = _make_update(user_id=12345)
    ctx = MagicMock()
    ctx.args = ["1"]
    await forget_command(update, ctx)
    notes = load_user_memory("telegram:12345")
    assert len(notes) == 1
    assert notes[0]["text"] == "note two"
    reply = update.message.reply_text.call_args[0][0]
    assert "note one" in reply


async def test_forget_command_invalid_id(whitelist_file, memory_store_file):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    save_user_memory("telegram:12345", [{"text": "note", "saved_at": _ts()}])
    update = _make_update(user_id=12345)
    ctx = MagicMock()
    ctx.args = ["99"]
    await forget_command(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "No note with ID" in reply


async def test_forget_command_non_numeric_id(whitelist_file, memory_store_file):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    update = _make_update(user_id=12345)
    ctx = MagicMock()
    ctx.args = ["abc"]
    await forget_command(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Usage" in reply


# ---------------------------------------------------------------------------
# /clear and /clear all
# ---------------------------------------------------------------------------

async def test_clear_command_removes_conversation_only(
    whitelist_file, conversation_file, memory_store_file
):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    conversation_file.write_text(json.dumps({
        "telegram:12345": [{"role": "user", "content": "old", "ts": _ts(hours_ago=1)}]
    }))
    save_user_memory("telegram:12345", [{"text": "I like coffee", "saved_at": _ts()}])

    update = _make_update(user_id=12345)
    ctx = MagicMock()
    ctx.args = []
    await clear_command(update, ctx)

    store = json.loads(conversation_file.read_text())
    assert "telegram:12345" not in store
    # Memory should be preserved
    notes = load_user_memory("telegram:12345")
    assert len(notes) == 1


async def test_clear_all_wipes_both_conversation_and_memory(
    whitelist_file, conversation_file, memory_store_file
):
    whitelist_file.write_text(json.dumps({"allowed_users": [12345]}))
    conversation_file.write_text(json.dumps({
        "telegram:12345": [{"role": "user", "content": "old", "ts": _ts(hours_ago=1)}]
    }))
    save_user_memory("telegram:12345", [{"text": "I like coffee", "saved_at": _ts()}])

    update = _make_update(user_id=12345)
    ctx = MagicMock()
    ctx.args = ["all"]
    await clear_command(update, ctx)

    store = json.loads(conversation_file.read_text())
    assert "telegram:12345" not in store
    assert load_user_memory("telegram:12345") == []


async def test_clear_command_unauthorized_user(
    whitelist_file, conversation_file, memory_store_file
):
    whitelist_file.write_text(json.dumps({"allowed_users": [1, 2]}))
    update = _make_update(user_id=999)
    ctx = MagicMock()
    ctx.args = []
    await clear_command(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "not authorized" in reply.lower()


# ---------------------------------------------------------------------------
# is_user_allowed
# ---------------------------------------------------------------------------

def test_is_user_allowed_when_explicitly_whitelisted(whitelist_file):
    whitelist_file.write_text(json.dumps({"allowed_users": [111, 222]}))
    assert is_user_allowed(111) is True
    assert is_user_allowed(222) is True


def test_is_user_allowed_auto_whitelists_under_limit(whitelist_file):
    whitelist_file.write_text(json.dumps({"allowed_users": []}))
    assert is_user_allowed(999) is True
    data = json.loads(whitelist_file.read_text())
    assert 999 in data["allowed_users"]


def test_is_user_allowed_rejected_when_limit_reached(whitelist_file):
    whitelist_file.write_text(json.dumps({"allowed_users": [1, 2]}))
    assert is_user_allowed(999) is False


def test_is_user_allowed_missing_file_auto_whitelists_first_user(whitelist_file):
    assert not whitelist_file.exists()
    assert is_user_allowed(42) is True


# ---------------------------------------------------------------------------
# /help, /id, /start
# ---------------------------------------------------------------------------

async def test_help_command_includes_all_registered_commands():
    update = _make_update()
    ctx = MagicMock()
    await help_command(update, ctx)
    reply: str = update.message.reply_text.call_args[0][0]
    for cmd in ("/ping", "/design", "/approve", "/reject", "/build_status",
                "/reset_build", "/clear", "/remember", "/memory", "/forget"):
        assert cmd in reply, f"Expected {cmd!r} in /help output"


async def test_show_id_returns_callers_user_id():
    update = _make_update(user_id=55555)
    ctx = MagicMock()
    await show_id(update, ctx)
    assert "55555" in update.message.reply_text.call_args[0][0]


async def test_start_greets_user_by_first_name():
    update = _make_update(first_name="Eugene")
    ctx = MagicMock()
    await start(update, ctx)
    assert "Eugene" in update.message.reply_text.call_args[0][0]


async def test_start_handles_missing_first_name():
    update = _make_update()
    update.effective_user.first_name = None
    ctx = MagicMock()
    await start(update, ctx)
    update.message.reply_text.assert_called_once()
