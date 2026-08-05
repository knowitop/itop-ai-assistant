from enum import StrEnum

from itop_ai_assistant.pipelines.models import ObjectRef


class TicketEvent(StrEnum):
    CREATED = "created"
    USER_COMMENTED = "user_commented"
    ASSIGNED = "assigned"


class WebhookPayload(ObjectRef):
    """What iTop posts: an object reference plus the event that fired."""

    event: TicketEvent
