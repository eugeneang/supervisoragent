# Approval-Gated Autonomous Coding Loop — Design Document

**Phase:** 1 (minimal working loop)
**Date:** 2026-04-15

---

## 1. Goal

Add a self-contained approval-gated coding loop alongside the existing bot.
The user sends a design request via Telegram, reviews a Claude-generated proposal,
approves it, and Claude executes the build by writing files to disk.
No existing features are changed. No auto-commit in Phase 1.

---

## 2. What changes and what does not

### Changed
| File | Change |
|------|--------|
| `telegram_bot.py` | +4 command handlers, +1 callback handler, +2 imports |
| `requirements.txt` | Add `anthropic` |

### New files
| File | Purpose |
|------|---------|
| `claude_bridge.py` | Anthropic SDK wrapper: design proposal + code generation |
| `supervisor_loop.py` | State machine + state persistence |
| `supervisor_state.json` | Runtime state on disk (auto-created, gitignored) |

### Untouched (hard constraint)
`ai_news_push.py`, `health_monitor.py`, `agents/ai_news_agent.py`,
`smart_commit.py`, `local_chat.py`, `whatsapp_app.py`,
`.github/workflows/*`, `.github/scripts/*`, `launchd/*`,
all existing Ollama calls, all existing Telegram handlers.

---

## 3. State machine

Single in-flight request. State persisted in `supervisor_state.json`.

```
IDLE
 │  /design <request text>
 ▼
DESIGNING          ← claude_bridge generates proposal (~5–20s)
 │  proposal ready
 ▼
AWAITING_APPROVAL  ← bot sends proposal + [Approve] [Reject] inline keyboard
 │  /approve or button       /reject or button
 ▼                              ▼
BUILDING                      IDLE
 │  build complete
 ▼
DONE               ← files written to disk; user inspects and commits manually
 │  /design (new request) or /reject (clear)
 ▼
IDLE
```

If the bot restarts mid-flow, state is recovered from `supervisor_state.json`.
`/reject` always resets to IDLE regardless of current state.

---

## 4. supervisor_state.json schema

Auto-created on first run. Never committed (gitignored).

```json
{
  "state": "IDLE",
  "requester_chat_id": null,
  "request_text": null,
  "proposal_text": null,
  "proposal_message_id": null,
  "feature_name": null,
  "build_result": null,
  "changed_files": [],
  "error": null,
  "created_at": null,
  "updated_at": null
}
```

---

## 5. supervisor_loop.py

Owns all state transitions. Knows nothing about Telegram — returns plain strings
and dicts for the bot handlers to forward.

```python
class SupervisorLoop:
    def load_state(self) -> dict
    def save_state(self) -> None          # atomic write via tmp file rename

    async def start_design(chat_id, request_text) -> str
        # Guard: reject if state != IDLE
        # IDLE → DESIGNING
        # calls claude_bridge.generate_proposal(request_text)
        # DESIGNING → AWAITING_APPROVAL
        # returns proposal_text

    async def approve(chat_id) -> str
        # Guard: reject if state != AWAITING_APPROVAL
        # AWAITING_APPROVAL → BUILDING
        # calls claude_bridge.execute_build(build_request) in asyncio task
        # returns "Build started..." message

    def reject(chat_id, reason="") -> str
        # Any state → IDLE
        # Clears all fields
        # returns confirmation string

    def get_status() -> str
        # Returns human-readable summary of current state
```

---

## 6. claude_bridge.py

All Anthropic SDK calls. No Telegram, no file-system logic except applying edits.
Uses `claude-sonnet-4-6` for both tasks. System prompts use `cache_control` for
prompt caching (system prompt is long and repeated).

```python
class ClaudeBridge:
    client: anthropic.Anthropic

    async def generate_proposal(request_text: str) -> ProposalResult
        # Returns: ProposalResult(feature_name, proposal_text, error)
        # Prompt instructs Claude to produce a structured markdown proposal

    async def execute_build(build_request: dict) -> BuildResult
        # Reads relevant repo files as context
        # Asks Claude to produce file edits as structured JSON
        # Schema: [{"path": "relative/path.py", "content": "full file content"}]
        # Applies edits (writes files inside repo root only)
        # Returns: BuildResult(success, changed_files, summary, error)

    def _apply_edits(edits: list[dict], repo_root: Path) -> list[str]
        # Writes each file; enforces path is inside repo_root
        # Returns list of relative paths written
```

**Build request passed to execute_build:**
```python
{
    "feature_name": str,
    "request_text": str,
    "proposal_text": str,
    "repo_path": "/Users/eugene/Agents/supervisoragent",
    "constraints": [
        "Do not modify ai_news_push.py",
        "Do not modify health_monitor.py",
        "Do not modify agents/ai_news_agent.py",
        "Do not modify smart_commit.py",
        "Do not modify local_chat.py",
        "Do not modify whatsapp_app.py",
        "Do not modify any file under .github/",
        "Do not modify any launchd plist",
        "Output ONLY a JSON array of file edits, nothing else",
    ],
}
```

**ProposalResult:**
```python
@dataclass
class ProposalResult:
    feature_name: str   # slug, e.g. "weather_command"
    proposal_text: str  # markdown
    error: str | None
```

**BuildResult:**
```python
@dataclass
class BuildResult:
    success: bool
    changed_files: list[str]
    summary: str
    error: str | None
```

---

## 7. telegram_bot.py — minimal additions only

### New imports (2 lines added)
```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from supervisor_loop import SupervisorLoop
```

`CallbackQueryHandler` added to the existing `telegram.ext` import line.

### New module-level instance (1 line)
```python
supervisor = SupervisorLoop()
```

### New async handler functions (4 functions, ~80 lines total)

**`design_command(update, context)`**
- Reads args as request text
- Calls `supervisor.start_design(chat_id, request_text)`
- On proposal ready: sends proposal text + inline keyboard `[✅ Approve] [❌ Reject]`
- On error: replies with error message

**`approve_command(update, context)`**
- Calls `supervisor.approve(chat_id)`
- Replies with "Build started..." or error

**`reject_command(update, context)`**
- Calls `supervisor.reject(chat_id, reason)`
- Replies with confirmation

**`build_status_command(update, context)`**
- Calls `supervisor.get_status()`
- Replies with status string

**`button_callback(update, context)`**
- Handles `callback_data="approve"` and `callback_data="reject"`
- Delegates to same logic as approve/reject commands
- Answers the callback query to remove the loading spinner

### New handler registrations in main() (5 lines added after existing handlers)
```python
app.add_handler(CommandHandler("design", design_command))
app.add_handler(CommandHandler("approve", approve_command))
app.add_handler(CommandHandler("reject", reject_command))
app.add_handler(CommandHandler("build_status", build_status_command))
app.add_handler(CallbackQueryHandler(button_callback))
```

---

## 8. Telegram UX walkthrough

### Start a design
```
User:  /design add a /weather command that fetches weather for a city
Bot:   Generating design proposal... ⏳
```

### Proposal arrives
```
Bot:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Design Proposal: weather_command
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Feature: /weather <city>
Summary: Adds a Telegram command that calls wttr.in JSON API
         and returns temperature + short conditions string.

Files to change:
  • telegram_bot.py — add /weather handler (~30 lines)

Constraints respected:
  ✓ No changes to ai_news_push.py, health_monitor.py, .github/

[✅ Approve]  [❌ Reject]
```

### Approve
```
User: [taps ✅ Approve]
Bot:  Build started... 🔨 Claude is writing code. This may take 30–60s.

Bot:  Build complete ✅
      Changed files:
        • telegram_bot.py

      Restart the bot to activate the new command.
      Review the diff with: git diff telegram_bot.py
```

### Reject
```
User: [taps ❌ Reject]
Bot:  Design rejected. Send /design <request> to start over.
```

### Status check
```
User:  /build_status
Bot:   State: AWAITING_APPROVAL
       Feature: weather_command
       Request: "add a /weather command..."
       Waiting for your approval or rejection.
```

---

## 9. Environment variables

| Variable | Where used | How to set |
|----------|-----------|------------|
| `ANTHROPIC_API_KEY` | `claude_bridge.py` | Add to `ai_news_push.env` |
| `TELEGRAM_BOT_TOKEN` | existing — unchanged | already set |
| `TELEGRAM_CHAT_ID` | existing — unchanged | already set |

---

## 10. Safety constraints

| Concern | Mitigation |
|---------|-----------|
| Claude writes files outside repo | `_apply_edits` resolves all paths under `repo_root`; raises if path escapes |
| State stuck after bot restart | `supervisor_state.json` persists; `/reject` always resets to IDLE |
| Concurrent /design requests | Guard at top of `start_design`: returns error if state != IDLE |
| Missing API key | `ClaudeBridge.__init__` raises with clear message if key absent |
| Build writes bad code | User inspects `git diff` before committing; auto-rollback CI handles it if committed |
| Breaking existing handlers | Zero changes to existing handlers; new handlers registered after existing ones |

---

## 11. What is deferred to Phase 2

- Auto-commit + push (Option C + CI safety net)
- Ollama → Claude migration for telegram_bot.py, ai_news_agent.py, smart_commit.py
- /test_<feature> dynamic command registration
- Multi-user concurrent builds
- Build history / audit log

---

## 12. Implementation order

1. `requirements.txt` — add `anthropic`
2. `.gitignore` — add `supervisor_state.json`
3. `claude_bridge.py` — new file
4. `supervisor_loop.py` — new file
5. `telegram_bot.py` — minimal additions only
