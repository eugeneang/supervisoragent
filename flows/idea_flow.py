"""
IdeaFlow — multi-turn Telegram flow for /idea command.

States:
  IDLE       → user issues /idea [topic]
  PENDING    → idea shown, waiting for Approve / Revise / Cancel
  REVISING   → user sent free-text revision request, regenerating
"""
from __future__ import annotations

from enum import Enum, auto
from dataclasses import dataclass, field


class IdeaState(Enum):
    IDLE = auto()
    PENDING = auto()
    REVISING = auto()


@dataclass
class IdeaFlow:
    state: IdeaState = IdeaState.IDLE
    last_idea: str = ""
    topic: str = ""

    # Callback-data constants used by the inline keyboard
    CB_APPROVE = "idea:approve"
    CB_REVISE  = "idea:revise"
    CB_CANCEL  = "idea:cancel"

    def reset(self) -> None:
        self.state = IdeaState.IDLE
        self.last_idea = ""
        self.topic = ""
