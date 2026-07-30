from enum import StrEnum

from pydantic import BaseModel, Field


class TicketEvent(StrEnum):
    CREATED = "created"
    USER_COMMENTED = "user_commented"
    ASSIGNED = "assigned"


class WebhookPayload(BaseModel):
    obj_class: str = Field(alias="class")
    id: str
    event: TicketEvent

    model_config = {"populate_by_name": True}
