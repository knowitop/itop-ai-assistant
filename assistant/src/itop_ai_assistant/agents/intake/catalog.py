"""The one place intake reads the service catalog.

Every list of services the module works with comes from here: the catalog the
model is given at the start of a run, the one it re-reads after a refusal, and
the one `set_classification` validates against. That is what makes the single
filter enough — a service declared "not classified" (`Classification`) is
absent from all three, so classifying a ticket into it is refused by the
existing validation instead of by a rule of its own.

Subcategories are read in `tools.py` as they always were: they are read per
service, and a declared service never gets that far.
"""

from itop_ai_assistant.domain.catalog import Service
from itop_ai_assistant.util.text import bind_oql

from .context import IntakeContext


async def offered_services(ctx: IntakeContext) -> list[Service]:
    """The services this ticket may be classified into."""
    services = await ctx.catalog_repo.find_services(bind_oql(ctx.intake.classify_service_oql, ctx.ticket.model_dump()))
    declared = ctx.classification.unclassified_services
    return [service for service in services if service.id not in declared]
