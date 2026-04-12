import json
from pathlib import Path
import ollama

MODEL = "qwen2.5:7b"

CONVERSATION_FILE = Path("conversation_store.json")
MEMORY_FILE = Path("structured_memory.json")

SYSTEM_PROMPT = """
You are a helpful personal supervisor assistant.

You have access to user structured memory including:
- profile
- facts
- notes

Always use this memory when responding.

If user shares new preferences or personal facts, remember them.
"""


# ---------- Load / Save Helpers ----------

def load_json(path):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------- Conversation Memory ----------

def get_conversation(user_id):
    store = load_json(CONVERSATION_FILE)

    if user_id not in store:
        store[user_id] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    return store, store[user_id]


def save_conversation(store):
    save_json(CONVERSATION_FILE, store)


def trim_history(history, max_messages=10):
    if len(history) <= max_messages:
        return history
    return [history[0]] + history[-(max_messages - 1):]


# ---------- Structured Memory ----------

def get_memory(user_id):
    memory = load_json(MEMORY_FILE)

    if user_id not in memory:
        memory[user_id] = {
            "profile": {},
            "facts": {},
            "notes": []
        }

    return memory, memory[user_id]


def save_memory(memory):
    save_json(MEMORY_FILE, memory)


# ---------- Extract New Facts ----------

def update_memory(user_id, user_text, reply):

    memory, user_memory = get_memory(user_id)

    extract_prompt = f"""
Extract any useful user facts from this message.

User message:
{user_text}

Return JSON only like:
{{
  "profile": {{}},
  "facts": {{}},
  "notes": []
}}

Only include new information.
"""

    response = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You extract structured user memory."},
            {"role": "user", "content": extract_prompt}
        ]
    )

    try:
        extracted = json.loads(response["message"]["content"])

        user_memory["profile"].update(extracted.get("profile", {}))
        user_memory["facts"].update(extracted.get("facts", {}))

        for note in extracted.get("notes", []):
            if note not in user_memory["notes"]:
                user_memory["notes"].append(note)

        memory[user_id] = user_memory
        save_memory(memory)

    except:
        pass


# ---------- Chat ----------

def chat(user_id, user_text):

    store, history = get_conversation(user_id)

    memory, user_memory = get_memory(user_id)

    memory_text = json.dumps(user_memory, indent=2)

    history.append({
        "role": "user",
        "content": f"""
User Memory:
{memory_text}

User message:
{user_text}
"""
    })

    history = trim_history(history)

    response = ollama.chat(
        model=MODEL,
        messages=history
    )

    reply = response["message"]["content"]

    history.append({
        "role": "assistant",
        "content": reply
    })

    store[user_id] = history
    save_conversation(store)

    update_memory(user_id, user_text, reply)

    return reply


# ---------- Main ----------

def main():

    user_id = input("Enter user id: ").strip() or "default-user"

    print("Type 'exit' to quit.\n")

    while True:

        user_text = input("You: ").strip()

        if user_text.lower() == "exit":
            break

        reply = chat(user_id, user_text)

        print("\nAssistant:", reply, "\n")


if __name__ == "__main__":
    main()