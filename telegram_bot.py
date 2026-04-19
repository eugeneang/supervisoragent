import json
import os
import sys
from pathlib import Path
import logging

from agents.ai_news_agent import get_ai_news_digest

import ollama
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict as TelegramConflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from supervisor_loop import SupervisorLoop
from commands.ping import ping_command
from commands.recap import recap_handler

from ddgs import DDGS

supervisor = SupervisorLoop()

WHITELIST_FILE = Path("whitelist.json")
AUTO_WHITELIST_LIMIT = 2
MODEL = "qwen2.5:7b"
CONVERSATION_FILE = Path("conversation_store.json")
MEMORY_FILE = Path("structured_memory.json")

SYSTEM_PROMPT = """
You are a helpful personal supervisor assistant.
Use the user's memory when relevant.
Be concise, practical, and clear.
Keep replies short and chat-friendly.

If web results are provided, use them.
If the web results are weak or incomplete, say so clearly.
Do not pretend you searched the internet unless web results are included.
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Available commands:\n\n"
        "/help - show command list\n"
        "/ping - check if the bot is alive\n"
        "/ai - latest AI news summary\n"
        "/id - show your Telegram user ID\n"
        "/recap - daily activity summary\n\n"
        "Coding loop:\n"
        "/design <request> - generate a design proposal\n"
        "/approve - approve the pending proposal and start build\n"
        "/reject [reason] - reject/rollback and reset to IDLE\n"
        "/build_status - show current loop state and timestamps\n"
        "/reset_build - force-reset stuck state to IDLE\n\n"
        "After a build completes you will see inline buttons:\n"
        "  ✅ Commit & Push — commit, push, and restart services\n"
        "  ❌ Rollback Build — revert all build file changes"
    )
    await update.message.reply_text(help_text)

async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    telegram_user_id = update.effective_user.id

    if not is_user_allowed(telegram_user_id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    await update.message.reply_text("Fetching latest AI news...")

    try:
        digest = get_ai_news_digest()
        await update.message.reply_text(digest)
    except Exception:
        logger.exception("Failed to fetch AI news")
        await update.message.reply_text("Sorry, I couldn't fetch AI news right now.")

def load_json(path):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def search_web(query, max_results=5):
    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            title = r.get("title", "")
            body = r.get("body", "")
            href = r.get("href", "")
            results.append({
                "title": title,
                "body": body,
                "href": href
            })
    return results

def should_use_web(user_text):
    web_keywords = [
        "today", "latest", "current", "news", "weather", "near me",
        "restaurant", "food", "open now", "price", "stock", "flight",
        "traffic", "map", "review", "best"
    ]
    text = user_text.lower()
    return any(k in text for k in web_keywords)

def load_whitelist():
    data = load_json(WHITELIST_FILE)
    return set(data.get("allowed_users", []))


def save_whitelist(users):
    save_json(WHITELIST_FILE, {
        "allowed_users": list(users)
    })

def is_user_allowed(user_id):
    allowed = load_whitelist()

    if user_id in allowed:
        return True

    if len(allowed) < AUTO_WHITELIST_LIMIT:
        logger.info(f"Auto-whitelisting new user: {user_id}")
        allowed.add(user_id)
        save_whitelist(allowed)
        return True

    return False

def trim_history(history, max_messages=8):
    if len(history) <= max_messages:
        return history
    return [history[0]] + history[-(max_messages - 1):]


def get_memory(user_id):
    memory = load_json(MEMORY_FILE)
    if user_id not in memory:
        memory[user_id] = {
            "profile": {},
            "facts": {},
            "notes": []
        }
        save_json(MEMORY_FILE, memory)
    return memory[user_id]


def get_conversation_store():
    return load_json(CONVERSATION_FILE)


def save_conversation_store(store):
    save_json(CONVERSATION_FILE, store)


def build_messages(user_id, user_text):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    store = get_conversation_store()

    if user_id not in store:
        store[user_id] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    history = store[user_id]
    memory_text = _memory_text(user_id)

    now = datetime.now(ZoneInfo("Asia/Singapore"))
    current_datetime = now.strftime("%A, %d %B %Y %H:%M SGT")

    extra_context = ""

    if should_use_web(user_text):
        web_results = search_web(user_text, max_results=5)
        if web_results:
            formatted = []
            for i, r in enumerate(web_results, start=1):
                formatted.append(
                    f"{i}. Title: {r['title']}\nSummary: {r['body']}\nLink: {r['href']}"
                )
            extra_context = "\n\nWeb results:\n" + "\n\n".join(formatted)
        else:
            extra_context = "\n\nWeb results:\nNo useful web results found."

    history.append({
        "role": "user",
        "content": (
            f"Current date and time: {current_datetime}\n\n"
            f"User memory:\n{memory_text}\n\n"
            f"User message:\n{user_text}{extra_context}"
        ),
    })

    history = trim_history(history)
    store[user_id] = history
    save_conversation_store(store)

    return history


def append_assistant_reply(user_id, reply):
    store = get_conversation_store()
    if user_id not in store:
        store[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    store[user_id].append({
        "role": "assistant",
        "content": reply
    })
    store[user_id] = trim_history(store[user_id])
    save_conversation_store(store)


def _note_as_of(note: object, today: str) -> str:
    """Render a memory note with its age for injection into the model context."""
    if isinstance(note, dict) and "as_of" in note:
        stored = note["as_of"]
        text = note.get("text", json.dumps({k: v for k, v in note.items() if k != "as_of"}))
        if stored == today:
            return f"{text} (today)"
        return f"{text} (as of {stored})"
    if isinstance(note, str):
        return note
    return json.dumps(note)


def _memory_text(user_id: str) -> str:
    """
    Render structured memory for injection into the model context.
    Notes carry their 'as_of' date so the model can treat stale items with
    appropriate scepticism rather than assuming everything is current.
    """
    from datetime import date
    today = date.today().isoformat()
    memory = get_memory(user_id)
    rendered_notes = [_note_as_of(n, today) for n in memory.get("notes", [])]
    return json.dumps(
        {
            "profile": memory.get("profile", {}),
            "facts": memory.get("facts", {}),
            "notes": rendered_notes,
        },
        indent=2,
        ensure_ascii=False,
    )


def update_memory(user_id, user_text):
    from datetime import date
    memory_store = load_json(MEMORY_FILE)
    if user_id not in memory_store:
        memory_store[user_id] = {
            "profile": {},
            "facts": {},
            "notes": []
        }

    today = date.today().isoformat()

    extract_prompt = f"""
Extract any useful LONG-TERM user memory from this message.

Message:
{user_text}

Return JSON only in this shape:
{{
  "profile": {{}},
  "facts": {{}},
  "notes": []
}}

Rules:
- LONG-TERM means it will still be true weeks or months from now.
- profile: stable identity info (name, location, language, timezone).
- facts: persistent preferences or habits (favourite food, sleep patterns, hobbies).
- notes: brief strings for long-term context worth remembering.
- DO NOT store one-time events, appointments, or anything with a specific date/time
  (e.g. "meeting at 9am tomorrow", "dinner tonight") — these go stale immediately.
- DO NOT store the current emotional state (e.g. "feeling down today") unless it
  reveals a persistent pattern worth remembering.
- Keep keys short and consistent. If nothing qualifies, return empty objects/lists.
"""

    try:
        response = ollama.chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You extract structured long-term user memory."},
                {"role": "user", "content": extract_prompt}
            ]
        )

        content = response["message"]["content"].strip()
        extracted = json.loads(content)

        memory_store[user_id]["profile"].update(extracted.get("profile", {}))
        memory_store[user_id]["facts"].update(extracted.get("facts", {}))

        for note in extracted.get("notes", []):
            # Wrap plain strings with an as_of date so staleness is visible later
            if isinstance(note, str) and note.strip():
                entry = {"as_of": today, "text": note.strip()}
            elif isinstance(note, dict):
                note["as_of"] = today
                entry = note
            else:
                continue
            # Deduplicate by text content
            existing_texts = {
                (n.get("text") if isinstance(n, dict) else n)
                for n in memory_store[user_id]["notes"]
            }
            text_key = entry.get("text") if isinstance(entry, dict) else entry
            if text_key not in existing_texts:
                memory_store[user_id]["notes"].append(entry)

        save_json(MEMORY_FILE, memory_store)
    except Exception:
        logger.exception("Failed to update memory")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Hello {user.first_name or 'there'}! Send me a message and I'll reply."
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(f"Your Telegram user ID is: {user_id}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.message.text:
        return

    telegram_user_id = update.effective_user.id
    user_id = f"telegram:{telegram_user_id}"
    incoming_msg = update.message.text.strip()

    logger.info("Telegram sender ID: %s", telegram_user_id)

    if not is_user_allowed(telegram_user_id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    try:
        history = build_messages(user_id, incoming_msg)
        model_response = ollama.chat(
            model=MODEL,
            messages=history
        )

        reply = model_response["message"]["content"].strip()
        append_assistant_reply(user_id, reply)
        update_memory(user_id, incoming_msg)

        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")
        logger.exception("Error while handling Telegram message")

async def design_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized.")
        return
    request_text = " ".join(context.args).strip() if context.args else ""
    if not request_text:
        await update.message.reply_text("Usage: /design <your feature request>")
        return
    await update.message.reply_text("Generating design proposal... ⏳")
    proposal_text, status_msg = await supervisor.start_design(
        update.effective_chat.id, request_text
    )
    if proposal_text is None:
        await update.message.reply_text(status_msg)
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data="approve"),
        InlineKeyboardButton("❌ Reject", callback_data="reject"),
    ]])
    await update.message.reply_text(proposal_text, reply_markup=keyboard)


def _commit_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard shown after a successful build + passing tests."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Commit & Push", callback_data="approve_commit"),
        InlineKeyboardButton("❌ Rollback Build", callback_data="reject_commit"),
    ]])


def _fix_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard shown when Claude has proposed a fix after failing tests."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔧 Approve Fix", callback_data="approve_fix"),
        InlineKeyboardButton("❌ Rollback", callback_data="reject_fix"),
    ]])


def _make_notify(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """
    Return a notify coroutine that attaches the appropriate inline keyboard
    based on the supervisor's current state.
      AWAITING_COMMIT_APPROVAL → commit/rollback keyboard
      AWAITING_FIX_APPROVAL    → approve-fix/rollback keyboard
    Keeps Telegram UI concerns out of supervisor_loop.py.
    """
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
    """
    Run the in-process smoke test suite and return (all_passed, results).
    Injected as test_callback into supervisor.approve() so the build pipeline
    runs tests automatically after every successful build.
    """
    from tests.telegram_smoke_tester import run_inprocess
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = int(os.environ.get("SMOKE_CHAT_ID", "0"))
    return await run_inprocess(chat_id=chat_id, bot_token=token, send_summary=False)


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized.")
        return
    chat_id = update.effective_chat.id
    reply = await supervisor.approve(
        chat_id, _make_notify(context, chat_id), test_callback=_run_smoke_tests
    )
    await update.message.reply_text(reply)


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized.")
        return
    reason = " ".join(context.args).strip() if context.args else ""
    await update.message.reply_text(supervisor.reject(reason))


async def build_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized.")
        return
    await update.message.reply_text(supervisor.get_status())


async def reset_build_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized.")
        return
    await update.message.reply_text(supervisor.force_reset())


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not update.effective_user:
        return
    if not is_user_allowed(update.effective_user.id):
        await query.answer("Not authorized.")
        return
    await query.answer()  # removes the loading spinner on the button
    chat_id = query.message.chat.id
    data = query.data or ""

    if data == "approve":
        reply = await supervisor.approve(
            chat_id, _make_notify(context, chat_id), test_callback=_run_smoke_tests
        )
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=chat_id, text=reply)

    elif data == "reject":
        reply = supervisor.reject()
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=chat_id, text=reply)

    elif data == "approve_fix":
        reply = await supervisor.approve(
            chat_id, _make_notify(context, chat_id), test_callback=_run_smoke_tests
        )
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=chat_id, text=reply)

    elif data == "reject_fix":
        reply = supervisor.reject("fix rejected by user")
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=chat_id, text=reply)

    elif data == "approve_commit":
        reply = await supervisor.approve_commit(_make_notify(context, chat_id))
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=chat_id, text=reply)

    elif data == "reject_commit":
        reply = supervisor.reject_commit()
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=chat_id, text=reply)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global error handler for the Application.

    409 Conflict means a second bot instance is polling the same token.
    We exit immediately so launchd can restart us cleanly once the other
    instance has been killed.  All other errors are logged and swallowed
    so a single bad update cannot crash the bot.
    """
    err = context.error
    if isinstance(err, TelegramConflict):
        logger.critical(
            "409 Conflict: another bot instance is polling this token. "
            "Exiting so launchd can restart cleanly. "
            "Check for duplicate launchd services or stale processes."
        )
        sys.exit(1)
    logger.error("Unhandled exception in update handler", exc_info=err)


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Supervisor coding loop
    app.add_handler(CommandHandler("design", design_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("reject", reject_command))
    app.add_handler(CommandHandler("build_status", build_status_command))
    app.add_handler(CommandHandler("reset_build", reset_build_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Telegram bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
