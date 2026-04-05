from enum import StrEnum
from typing import Optional, TypedDict


class Action(StrEnum):
    ASK = "ask"
    ENRICH = "enrich"
    STOP = "stop"


class EnrichmentState(TypedDict):
    ticket: dict
    rounds: int
    ai_done: bool
    action: Optional[Action]
    question: Optional[str]
