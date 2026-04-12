import json
from pathlib import Path

import ollama
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

MODEL = "qwen2.5:7b"
ALLOWED_USERS = {
    "whatsapp:+6598807621",
    "whatsapp:+6593885619",
}
CONVERSATION_FILE = Path("conversation_store.json")
MEMORY_FILE = Path("structured_memory.json")

SYSTEM_PROMPT = """
You are a helpful personal supervisor assistant.
Use the user's memory when relevant.
Be concise, practical, and clear.
"""


def load_json(path):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


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
    store = get_conversation_store()
    memory = get_memory(user_id)

    if user_id not in store:
        store[user_id] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    history = store[user_id]

    memory_text = json.dumps(memory, indent=2, ensure_ascii=False)

    history.append({
        "role": "user",
        "content": f"User memory:\n{memory_text}\n\nUser message:\n{user_text}"
    })

    history = trim_history(history)
    store[user_id] = history
    save_conversation_store(store)

    return store, history


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


def update_memory(user_id, user_text):
    memory_store = load_json(MEMORY_FILE)
    if user_id not in memory_store:
        memory_store[user_id] = {
            "profile": {},
            "facts": {},
            "notes": []
        }

    extract_prompt = f"""
Extract any useful long-term user memory from this message.

Message:
{user_text}

Return JSON only in this shape:
{{
  "profile": {{}},
  "facts": {{}},
  "notes": []
}}

Rules:
- Only include useful long-term facts or preferences.
- Keep keys short and consistent.
- If nothing useful, return empty objects/lists.
"""

    try:
        response = ollama.chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You extract structured user memory."},
                {"role": "user", "content": extract_prompt}
            ]
        )

        content = response["message"]["content"].strip()
        extracted = json.loads(content)

        memory_store[user_id]["profile"].update(extracted.get("profile", {}))
        memory_store[user_id]["facts"].update(extracted.get("facts", {}))

        for note in extracted.get("notes", []):
            if note not in memory_store[user_id]["notes"]:
                memory_store[user_id]["notes"].append(note)

        save_json(MEMORY_FILE, memory_store)
    except Exception:
        pass


@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "").strip()

    resp = MessagingResponse()
    msg = resp.message()

    if sender not in ALLOWED_USERS:
        msg.body("Sorry, you are not authorized to use this bot.")
        return str(resp)

    if not incoming_msg:
        msg.body("I didn't receive any message content.")
        return str(resp)

    try:
        _, history = build_messages(sender, incoming_msg)

        model_response = ollama.chat(
            model=MODEL,
            messages=history
        )

        reply = model_response["message"]["content"].strip()
        append_assistant_reply(sender, reply)
        update_memory(sender, incoming_msg)

        msg.body(reply)
    except Exception as e:
        msg.body(f"Error: {str(e)}")

    return str(resp)


@app.route("/", methods=["GET"])
def health():
    return "WhatsApp supervisor agent is running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
