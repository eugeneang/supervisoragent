"""
/idea command — generates a creative project idea using Claude,
drawing on the skills visible in this repo (AI news, web search,
supervisor loop, Telegram bot, commit automation, etc.).
"""
from __future__ import annotations

import re
import anthropic

SKILLS_CONTEXT = """
Skills and tools available in this repo / system:
- Telegram bot with inline keyboards and multi-turn flows
- Claude (Anthropic) LLM for reasoning and text generation
- Web search via DuckDuckGo (ddgs)
- AI news digest (fetches + summarises recent AI news)
- Git automation: smart commits, repo introspection
- Supervisor loop: design → build → test → approve cycle
- Health monitor: tracks service uptime and notifies on issues
- launchd service management on macOS (start/stop/restart services)
- Memory system: persistent key-value store per Telegram user
- Recap command: daily activity summary (commits, builds, tests)
- Smoke test runner: in-process async test harness for bot commands
- Scheduled push notifications (AI news, health alerts)
"""

SYSTEM_PROMPT = f"""You are a creative technical product manager helping a solo developer
find their next exciting side-project idea.

{SKILLS_CONTEXT}

When asked for an idea, respond in EXACTLY this format (no extra text):

💡 *Idea:* <one-line catchy title>

<2-3 sentence description of what it does and why it's interesting>

🛠 *Tools:* <comma-separated list of tools/skills from the repo that would be used>

📈 *Effort:* <Small | Medium | Large>

_Approve, revise, or cancel?_"""


async def generate_idea(topic: str | None = None) -> str:
    """Call Claude to generate a project idea, optionally focused on `topic`."""
    client = anthropic.Anthropic()

    user_msg = "Give me a great new project idea I could build this weekend."
    if topic and topic.strip():
        user_msg = (
            f"Give me a great new project idea I could build this weekend, "
            f"focused on or inspired by: {topic.strip()}"
        )

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return message.content[0].text.strip()
