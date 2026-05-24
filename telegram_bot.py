# --- imports + module-level setup ---
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import logging

from agents.ai_news_agent import get_ai_news_digest

import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict as TelegramConflict
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from supervisor_loop import SupervisorLoop
from commands.ping import ping_command
from commands.recap import recap_handler
from commands.ops import ops_handler
from commands.logs import logs_handler
from commands.idea import generate_idea
from flows.idea_flow import IdeaFlow, IdeaState

from ddgs import DDGS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------
import config

AUTHORIZED_USER_ID: int | None = getattr(config, "AUTHORIZED_USER_ID", None)
MEMORY_FILE: Path = Path(getattr(config, "MEMORY_FILE", "/tmp/bot_memory.json"))
WHITELIST_FILE = Path("whitelist.json")
AUTO_WHITELIST_LIMIT = 2
CONVERSATION_FILE = Path("conversation_store.json")
MEMORY_STORE_FILE = Path("memory_store.json")
CHAT_MODEL = "claude-haiku-4-5-20251001"
_SGT = ZoneInfo("Asia/Singapore")

supervisor = SupervisorLoop()

# ---------------------------------------------------------------------------
# Module-level idea flow state (single-user bot)
# ---------------------------------------------------------------------------
_idea_flow = IdeaFlow()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Could not read JSON file %s", path)
    return {}


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _parse_ts(ts: str | None) -> datetime:
    if not ts:
        return datetime(1970, 1, 1, tzinfo=_SGT)
    try:
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_SGT)
        return parsed.astimezone(_SGT)
    except Exception:
        return datetime(1970, 1, 1, tzinfo=_SGT)


def _window_history(history: list[dict], max_messages: int = 20) -> list[dict]:
    cutoff = datetime.now(_SGT) - timedelta(hours=72)
    recent = [
        msg for msg in history
        if msg.get("role") != "system" and _parse_ts(msg.get("ts")) >= cutoff
    ]
    return recent[-max_messages:]


def _trim_store(history: list[dict], max_messages: int = 50) -> list[dict]:
    return [msg for msg in history if msg.get("role") != "system"][-max_messages:]


def load_whitelist() -> set[int]:
    data = load_json(WHITELIST_FILE)
    return {int(uid) for uid in data.get("allowed_users", [])}


def save_whitelist(users: set[int]) -> None:
    save_json(WHITELIST_FILE, {"allowed_users": sorted(users)})


def is_user_allowed(user_id: int) -> bool:
    if AUTHORIZED_USER_ID is not None:
        return user_id == AUTHORIZED_USER_ID

    allowed = load_whitelist()
    if user_id in allowed:
        return True
    if len(allowed) < AUTO_WHITELIST_LIMIT:
        allowed.add(user_id)
        save_whitelist(allowed)
        logger.info("Auto-whitelisted Telegram user %s", user_id)
        return True
    return False


def load_user_memory(user_id: str) -> list[dict]:
    store = load_json(MEMORY_STORE_FILE)
    notes = store.get(user_id, [])
    return notes if isinstance(notes, list) else []


def save_user_memory(user_id: str, notes: list[dict]) -> None:
    store = load_json(MEMORY_STORE_FILE)
    store[user_id] = notes
    save_json(MEMORY_STORE_FILE, store)


def _memory_context(user_id: str) -> str:
    notes = load_user_memory(user_id)
    if not notes:
        return "none"
    return "\n".join(f"- {note.get('text', '')}" for note in notes if note.get("text"))


def build_messages(user_id: str, user_text: str) -> tuple[str, list[dict]]:
    store = load_json(CONVERSATION_FILE)
    history = _trim_store(store.get(user_id, []))
    now = datetime.now(_SGT)
    history.append({
        "role": "user",
        "content": user_text,
        "ts": now.isoformat(),
    })
    store[user_id] = _trim_store(history)
    save_json(CONVERSATION_FILE, store)

    system = (
        "You are a helpful personal supervisor assistant. "
        "Be concise, practical, and clear.\n\n"
        f"Current date and time: {now.strftime('%A, %d %B %Y %H:%M SGT')}\n\n"
        f"User memory:\n{_memory_context(user_id)}"
    )
    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in _window_history(store[user_id])
        if msg.get("role") in {"user", "assistant"}
    ]
    return system, messages


def append_assistant_reply(user_id: str, reply: str) -> None:
    store = load_json(CONVERSATION_FILE)
    history = _trim_store(store.get(user_id, []))
    history.append({
        "role": "assistant",
        "content": reply,
        "ts": datetime.now(_SGT).isoformat(),
    })
    store[user_id] = _trim_store(history)
    save_json(CONVERSATION_FILE, store)


async def _chat_with_tools(system_prompt: str, messages: list[dict]) -> str:
    client = anthropic.Anthropic()
    response = await asyncio.to_thread(
        client.messages.create,
        model=CHAT_MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=messages,
    )
    return response.content[0].text.strip()


def _load_memory() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_memory(data: dict) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(data, indent=2))


def _is_authorized(update: Update) -> bool:
    return bool(update.effective_user and is_user_allowed(update.effective_user.id))


# ---------------------------------------------------------------------------
# Auth guard decorator
# ---------------------------------------------------------------------------

def _require_auth(func):
    import functools

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            await update.message.reply_text("⛔ Unauthorized.")
            return
        return await func(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(f"Hello, {name}! I'm your agent bot. Type /help to see what I can do.")


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(f"Your Telegram user ID is: {uid}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Available commands:\n"
        "/ping — liveness check\n"
        "/id — show your Telegram user ID\n"
        "/start — greeting\n"
        "/help — this message\n"
        "/ai — AI news digest\n"
        "/recap — daily activity summary\n"
        "/ops — operational status dashboard\n"
        "/logs [service] [lines] — show recent sanitized logs\n"
        "/remember <key> <value> — store a memory\n"
        "/memory — list all memories\n"
        "/forget <key> — remove a memory\n"
        "/clear — clear conversation context\n"
        "/whitelist — show authorized user ID\n"
        "/design <task> — start a supervised coding task\n"
        "/approve — approve current build\n"
        "/reject — reject current build\n"
        "/build_status — show supervisor state\n"
        "/reset_build — reset supervisor to IDLE\n"
        "/idea [topic] — generate a project idea\n"
    )
    await update.message.reply_text(text)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    user_id = f"telegram:{update.effective_user.id}"
    store = load_json(CONVERSATION_FILE)
    store.pop(user_id, None)
    save_json(CONVERSATION_FILE, store)

    if context.args and context.args[0].lower() == "all":
        save_user_memory(user_id, [])
        await update.message.reply_text("Conversation history and memory cleared. Fresh start!")
    else:
        await update.message.reply_text("Conversation history cleared. Fresh start!")


# ---------------------------------------------------------------------------
# Memory commands
# ---------------------------------------------------------------------------

async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /remember <note>")
        return
    note_text = " ".join(args).strip()
    user_id = f"telegram:{update.effective_user.id}"
    notes = load_user_memory(user_id)
    notes.append({"text": note_text, "saved_at": datetime.now(_SGT).isoformat()})
    save_user_memory(user_id, notes)
    await update.message.reply_text(f"Remembered: {note_text}")


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    notes = load_user_memory(f"telegram:{update.effective_user.id}")
    if not notes:
        await update.message.reply_text("No memories stored.")
        return
    lines = [f"[{idx}] {note.get('text', '')}" for idx, note in enumerate(notes, start=1)]
    await update.message.reply_text("Memories:\n" + "\n".join(lines))


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /forget <memory_number>")
        return
    try:
        index = int(args[0])
    except ValueError:
        await update.message.reply_text("Usage: /forget <memory_number>")
        return
    user_id = f"telegram:{update.effective_user.id}"
    notes = load_user_memory(user_id)
    if index < 1 or index > len(notes):
        await update.message.reply_text(f"No note with ID {index}.")
        return
    removed = notes.pop(index - 1)
    save_user_memory(user_id, notes)
    await update.message.reply_text(f"Forgot: {removed.get('text', '')}")


async def whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Authorized user ID: {AUTHORIZED_USER_ID}")


# ---------------------------------------------------------------------------
# AI news command
# ---------------------------------------------------------------------------

@_require_auth
async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching AI news digest…")
    try:
        digest = await asyncio.get_event_loop().run_in_executor(None, get_ai_news_digest)
        await update.message.reply_text(digest)
    except Exception as exc:
        logger.exception("ai_command failed")
        await update.message.reply_text(f"❌ Error fetching digest: {exc}")


# ---------------------------------------------------------------------------
# Supervisor / build commands
# ---------------------------------------------------------------------------

@_require_auth
async def design_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = " ".join(context.args) if context.args else ""
    if not task:
        await update.message.reply_text("Usage: /design <task description>")
        return
    await update.message.reply_text("Generating design proposal... ⏳")
    proposal_text, status_msg = await supervisor.start_design(
        update.effective_chat.id,
        task,
    )
    if proposal_text is None:
        await update.message.reply_text(status_msg)
        return
    await update.message.reply_text(
        proposal_text,
        reply_markup=_design_keyboard(),
    )


@_require_auth
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = supervisor.load_state().get("state")
    if state == "AWAITING_COMMIT_APPROVAL":
        result = await supervisor.approve_commit(_make_notify(context, chat_id))
    else:
        result = await supervisor.approve(
            chat_id,
            _make_notify(context, chat_id),
            test_callback=_run_smoke_tests,
        )
    await update.message.reply_text(result)


@_require_auth
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reason = " ".join(context.args) if context.args else "No reason given."
    result = supervisor.reject(reason)
    await update.message.reply_text(f"🔄 {result}")


@_require_auth
async def build_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state_info = supervisor.get_status()
    await update.message.reply_text(state_info)


@_require_auth
async def reset_build_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(supervisor.force_reset())


def _design_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data="build:approve"),
        InlineKeyboardButton("✏️ Revise", callback_data="build:revise"),
        InlineKeyboardButton("❌ Reject", callback_data="build:reject"),
    ]])


def _commit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Commit & Push", callback_data="build:commit"),
        InlineKeyboardButton("❌ Rollback Build", callback_data="build:rollback"),
    ]])


def _fix_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔧 Approve Fix", callback_data="build:approve"),
        InlineKeyboardButton("❌ Rollback", callback_data="build:reject"),
    ]])


def _make_notify(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    async def notify(msg: str) -> None:
        state = supervisor.load_state().get("state")
        if state == "AWAITING_COMMIT_APPROVAL":
            markup = _commit_keyboard()
        elif state == "AWAITING_FIX_APPROVAL":
            markup = _fix_keyboard()
        else:
            markup = None
        await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=markup)
    return notify


async def _run_smoke_tests() -> tuple[bool, list]:
    from tests.telegram_smoke_tester import run_inprocess
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = int(os.environ.get("SMOKE_CHAT_ID", "0"))
    return await run_inprocess(chat_id=chat_id, bot_token=token, send_summary=False)


# ---------------------------------------------------------------------------
# Idea command — multi-turn flow
# ---------------------------------------------------------------------------

def _idea_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=IdeaFlow.CB_APPROVE),
            InlineKeyboardButton("✏️ Revise", callback_data=IdeaFlow.CB_REVISE),
            InlineKeyboardButton("❌ Cancel", callback_data=IdeaFlow.CB_CANCEL),
        ]
    ])


@_require_auth
async def idea_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _idea_flow
    topic = " ".join(context.args) if context.args else None
    _idea_flow.topic = topic or ""
    _idea_flow.state = IdeaState.PENDING

    await update.message.reply_text("💭 Generating idea…")
    try:
        idea_text = await asyncio.get_event_loop().run_in_executor(
            None, lambda: asyncio.run(generate_idea(topic))  # sync wrapper
        )
    except RuntimeError:
        # Already inside an event loop — use asyncio directly
        idea_text = await generate_idea(topic)
    except Exception as exc:
        logger.exception("idea_command: generate_idea failed")
        await update.message.reply_text(f"❌ Failed to generate idea: {exc}")
        _idea_flow.reset()
        return

    _idea_flow.last_idea = idea_text
    await update.message.reply_text(
        idea_text,
        parse_mode="Markdown",
        reply_markup=_idea_keyboard(),
    )


# ---------------------------------------------------------------------------
# Button callback dispatcher
# ---------------------------------------------------------------------------

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    if not is_user_allowed(update.effective_user.id):
        await query.answer("Not authorized.")
        return
    await query.answer()
    data = query.data

    if data == IdeaFlow.CB_APPROVE:
        await _handle_idea_approve(query, context)
    elif data == IdeaFlow.CB_REVISE:
        await _handle_idea_revise(query, context)
    elif data == IdeaFlow.CB_CANCEL:
        await _handle_idea_cancel(query, context)
    elif data == "build:approve":
        await _handle_build_approve(query, context)
    elif data == "build:revise":
        await _handle_build_revise(query, context)
    elif data == "build:reject":
        await _handle_build_reject(query, context)
    elif data == "build:commit":
        await _handle_build_commit(query, context)
    elif data == "build:rollback":
        await _handle_build_rollback(query, context)
    else:
        logger.warning("Unknown callback data: %s", data)


async def _handle_build_approve(query, context) -> None:
    chat_id = query.message.chat.id
    reply = await supervisor.approve(
        chat_id,
        _make_notify(context, chat_id),
        test_callback=_run_smoke_tests,
    )
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(chat_id=chat_id, text=reply)


async def _handle_build_revise(query, context) -> None:
    reply = supervisor.request_revision()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(reply)


async def _handle_build_reject(query, context) -> None:
    reply = supervisor.reject("rejected by user")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(reply)


async def _handle_build_commit(query, context) -> None:
    chat_id = query.message.chat.id
    reply = await supervisor.approve_commit(_make_notify(context, chat_id))
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(chat_id=chat_id, text=reply)


async def _handle_build_rollback(query, context) -> None:
    reply = supervisor.reject_commit()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(reply)


async def _handle_idea_approve(query, context) -> None:
    global _idea_flow
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "🚀 Great choice! The idea has been approved.\n\n"
        "Use /design to start building it, or /idea again for another one."
    )
    _idea_flow.reset()


async def _handle_idea_revise(query, context) -> None:
    global _idea_flow
    _idea_flow.state = IdeaState.REVISING
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "✏️ Sure! Tell me how you'd like to revise the idea "
        "(e.g. 'make it simpler', 'focus on AI news', 'add a web dashboard')."
    )


async def _handle_idea_cancel(query, context) -> None:
    global _idea_flow
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("❌ Idea cancelled. Use /idea to generate a new one.")
    _idea_flow.reset()


# ---------------------------------------------------------------------------
# Message handler (free-text, including idea revision)
# ---------------------------------------------------------------------------

async def _revision_command_intercept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Intercept commands typed while awaiting revision so flow isn't lost."""
    global _idea_flow
    if _idea_flow.state == IdeaState.REVISING:
        # Let the revision handler deal with it as plain text
        await handle_message(update, context)
        raise ApplicationHandlerStop


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _idea_flow

    text = update.message.text or ""
    user_id = f"telegram:{update.effective_user.id}" if update.effective_user else ""

    if not update.effective_user or not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    if supervisor.load_state().get("state") == "AWAITING_REVISION_FEEDBACK":
        reply = await supervisor.submit_revision_feedback(
            text,
            _make_notify(context, update.effective_chat.id),
        )
        await update.message.reply_text(reply)
        return

    # --- Idea revision flow ---
    if _idea_flow.state == IdeaState.REVISING:
        revision_note = text
        topic = _idea_flow.topic
        combined_topic = f"{topic}. Revision request: {revision_note}" if topic else revision_note
        await update.message.reply_text("💭 Revising idea…")
        try:
            idea_text = await generate_idea(combined_topic)
        except Exception as exc:
            logger.exception("handle_message: revision generate_idea failed")
            await update.message.reply_text(f"❌ Failed to revise: {exc}")
            _idea_flow.reset()
            return
        _idea_flow.last_idea = idea_text
        _idea_flow.state = IdeaState.PENDING
        await update.message.reply_text(
            idea_text,
            parse_mode="Markdown",
            reply_markup=_idea_keyboard(),
        )
        return

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )
        system_prompt, messages = build_messages(user_id, text.strip())
        reply = await _chat_with_tools(system_prompt, messages)
        append_assistant_reply(user_id, reply)
    except Exception as exc:
        logger.exception("handle_message: Claude call failed")
        reply = f"❌ Error: {exc}"

    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, TelegramConflict):
        logger.warning("Telegram Conflict error — another instance may be running.")
        return
    logger.exception("Unhandled exception", exc_info=context.error)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(token).build()

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("ai", ai_command))
    app.add_handler(CommandHandler("recap", recap_handler))
    app.add_handler(CommandHandler("ops", ops_handler))
    app.add_handler(CommandHandler("logs", logs_handler))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("remember", remember_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("whitelist", whitelist_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Group -1: intercepts command-like text when awaiting revision feedback.
    # Must be registered after the main handlers so _make_notify closure works,
    # but the group=-1 argument ensures it runs BEFORE group-0 dispatch.
    app.add_handler(
        MessageHandler(filters.COMMAND, _revision_command_intercept),
        group=-1,
    )

    # Supervisor coding loop
    app.add_handler(CommandHandler("design", design_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("reject", reject_command))
    app.add_handler(CommandHandler("build_status", build_status_command))
    app.add_handler(CommandHandler("reset_build", reset_build_command))

    # Idea generation flow
    app.add_handler(CommandHandler("idea", idea_command))

    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Telegram bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
