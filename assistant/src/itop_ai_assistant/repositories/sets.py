"""The repositories of one principal: the record, and what builds it.

`ItopRepositories` owns `ticket_mapping` / `faq_mapping`, because a repository
is what they configure, and is the only place where repositories are listed;
`RepositorySet` is the record a run holds and creates nothing. The connection
they are built over is a collaborator from `itop/` — this package is a layer of
its own, not a subfolder of the iTop one: what a consumer gets out of it is a
domain object, and where it came from is none of its business.

Kept out of `core/deps.py` deliberately. `deps` is the composition root: it
imports Redis, Qdrant and langchain, so anything that imports it inherits the
whole infrastructure. The run shell needs to *name* these types and nothing else
from the container — importing `deps` for that was what forced the
`TYPE_CHECKING` dance in `pipelines/` (ADR-019).
"""

from dataclasses import dataclass

from itop_ai_assistant.config import FaqMappingConfig, TicketMappingConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.itop.connection import ItopConnection
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.repositories.access import AccessRepository
from itop_ai_assistant.repositories.catalog import CatalogRepository
from itop_ai_assistant.repositories.faq import FaqRepository
from itop_ai_assistant.repositories.ticket import TicketRepository
from itop_ai_assistant.settings.config_store import ConfigStore


@dataclass(frozen=True)
class RepositorySet:
    """The repositories of one principal — a record, not a factory.

    One set, one identity: everything in here talks to iTop with the same
    credentials, so a run cannot half-act as somebody else. The client is not a
    field, and there is no accessor for it: nothing outside the repositories
    touches iTop (`.claude/rules/itop.md`).
    """

    ticket_repo: TicketRepository
    catalog_repo: CatalogRepository
    access_repo: AccessRepository
    faq_repo: FaqRepository


class ItopRepositories:
    """Builds repository sets over a connection. The only place they are listed.

    Owns the mapping sections and reads them per set, so an admin edit applies
    from the next run with no client rebuild and no cache to invalidate — the
    repositories themselves are stateless, a client and a mapping and nothing
    else.
    """

    def __init__(self, connection: ItopConnection, config_store: ConfigStore):
        self._connection = connection
        self._config_store = config_store

    async def for_principal(self, principal: Principal, *, comment: str) -> RepositorySet:
        """The only way to get a set: one connection view, one identity, one comment.

        A caller with nothing to attribute (the vector sweep, `selfcheck`)
        passes `Principal.service()` with a comment naming the subsystem
        instead of a run.
        """
        return await self._build(await self._connection.as_principal(principal, comment=comment))

    async def _build(self, client: Itop) -> RepositorySet:
        ticket_mapping = await self._config_store.get("ticket_mapping", TicketMappingConfig)
        faq_mapping = await self._config_store.get("faq_mapping", FaqMappingConfig)
        return RepositorySet(
            ticket_repo=TicketRepository(client, ticket_mapping),
            catalog_repo=CatalogRepository(client),
            access_repo=AccessRepository(client),
            faq_repo=FaqRepository(client, faq_mapping),
        )
