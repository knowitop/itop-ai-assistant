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
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.repositories.catalog import CatalogRepository
from itop_ai_assistant.util.text import bind_oql

from .config import IntakeConfig
from .context import IntakeContext
from .domain import Classification


async def offered_services(ctx: IntakeContext) -> list[Service]:
    """The services this ticket may be classified into."""
    return await read_offered_services(ctx.catalog_repo, ctx.intake, ctx.ticket, ctx.classification)


async def read_offered_services(
    catalog_repo: CatalogRepository, cfg: IntakeConfig, ticket: Ticket, classification: Classification
) -> list[Service]:
    """`offered_services` before there is a context: `compose.py` reads the
    catalog to build the scope the context is made of."""
    services = await catalog_repo.find_services(bind_oql(cfg.classify_service_oql, ticket.model_dump()))
    return [service for service in services if service.id not in classification.unclassified_services]
