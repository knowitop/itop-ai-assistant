from enum import StrEnum
from typing import Optional, TypedDict


class Action(StrEnum):
    ASK = "ask"
    ENRICH = "enrich"
    STOP = "stop"


class EnrichmentState(TypedDict):
    ticket: dict
    action: Optional[Action]
    question: Optional[str]
