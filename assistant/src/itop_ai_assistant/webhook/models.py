from itop_ai_assistant.domain.identity import ObjectIdentity
from itop_ai_assistant.pipelines.models import TicketEvent


class WebhookPayload(ObjectIdentity):
    """What iTop posts: an object reference plus the event that fired."""

    event: TicketEvent
