"""What a run ends as — shared by every trigger type.

What a run is *started* with is `domain.identity.ObjectIdentity` — a webhook,
a synchronous request and (later) a schedule all name the same object the same
way, and the shell depends on neither the webhook payload shape nor any other
entry point.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class TicketEvent(StrEnum):
    CREATED = "created"
    USER_COMMENTED = "user_commented"
    ASSIGNED = "assigned"


class RunOutcome(BaseModel):
    """How a run ended. A webhook ignores it; a request returns it to its caller."""

    status: Literal["done", "skipped"]
    detail: str = ""
