from enum import StrEnum
from typing import Optional, TypedDict

from domain.ticket import Ticket


class Action(StrEnum):
    ASK = "ask"
    ENRICH = "enrich"
    STOP = "stop"


class EnrichmentState(TypedDict):
    ticket: Ticket
    action: Optional[Action]
    question: Optional[str]
