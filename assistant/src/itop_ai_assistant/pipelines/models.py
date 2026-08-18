"""What a run is started with and what it ends as — shared by every trigger type.

Kept apart from `webhook/models.py` so the run shell depends on neither the
webhook payload shape nor any other entry point: a webhook, a synchronous
request and (later) a schedule all name the same object the same way.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ObjectRef(BaseModel):
    """An iTop object — all the shell needs to start a run over one."""

    obj_class: str = Field(alias="class")
    id: str

    model_config = {"populate_by_name": True}

    @property
    def label(self) -> str:
        return f"{self.obj_class}::{self.id}"


class TicketEvent(StrEnum):
    CREATED = "created"
    USER_COMMENTED = "user_commented"
    ASSIGNED = "assigned"


class RunOutcome(BaseModel):
    """How a run ended. A webhook ignores it; a request returns it to its caller."""

    status: Literal["done", "skipped"]
    detail: str = ""
