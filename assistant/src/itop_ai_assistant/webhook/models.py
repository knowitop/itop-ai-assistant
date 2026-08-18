from itop_ai_assistant.pipelines.models import ObjectRef, TicketEvent


class WebhookPayload(ObjectRef):
    """What iTop posts: an object reference plus the event that fired."""

    event: TicketEvent
