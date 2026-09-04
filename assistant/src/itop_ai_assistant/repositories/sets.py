"""The repositories of one principal: the record, and what builds it.

`ItopRepositories` owns the mapping section, because a repository is what it
configures, and is the only place where repositories are listed;
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

from collections.abc import Mapping
from dataclasses import dataclass

from itop_ai_assistant.config import MappingsConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA
from itop_ai_assistant.itop.connection import ItopConnection
from itop_ai_assistant.itop.write_policy import WritePolicy
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.repositories.access import AccessRepository
from itop_ai_assistant.repositories.catalog import CatalogRepository
from itop_ai_assistant.repositories.object_repo import ObjectRepository
from itop_ai_assistant.repositories.ticket import TicketRepository
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.state.counters import DailyCounters


@dataclass(frozen=True)
class RepositorySet:
    """The repositories of one principal — a record, not a factory.

    One set, one identity: everything in here talks to iTop with the same
    credentials, so a run cannot half-act as somebody else. The client is not a
    field, and there is no accessor for it: nothing outside the repositories
    touches iTop (`.claude/rules/itop.md`).
    """

    # One per object family, by family name. Generic consumers — the vector
    # sweep — ask for a family and get the same class whatever it is; a new
    # family adds an entry, not a field.
    objects: Mapping[str, ObjectRepository]
    ticket_repo: TicketRepository
    catalog_repo: CatalogRepository
    access_repo: AccessRepository


class ItopRepositories:
    """Builds repository sets over a connection. The only place they are listed.

    Owns the mapping section and reads it per set, so an admin edit applies
    from the next run with no client rebuild and no cache to invalidate — the
    repositories themselves are stateless, a client and a mapping and nothing
    else.
    """

    def __init__(
        self,
        connection: ItopConnection,
        config_store: ConfigStore,
        write_policy: WritePolicy,
        counters: DailyCounters,
    ):
        self._connection = connection
        self._config_store = config_store
        self._write_policy = write_policy
        self._counters = counters

    async def for_principal(self, principal: Principal, *, comment: str) -> RepositorySet:
        """The only way to get a set: one connection view, one identity, one comment.

        A caller with nothing to attribute (the vector sweep, `selfcheck`)
        passes `Principal.service()` with a comment naming the subsystem
        instead of a run.

        Also where the dry run is enforced (REQ-006): the policy is asked here,
        never passed in, so a set built for a module that has never heard of the
        mode is as unable to write as one built for a module that has. A check
        inside a tool would be the opposite — a promise made to the customer
        about the whole installation, kept by every module remembering to.
        """
        client = await self._connection.as_principal(principal, comment=comment)
        if await self._write_policy.dry_run():
            client = client.read_only()
        return await self._build(client)

    async def _build(self, client: Itop) -> RepositorySet:
        mappings = await self._config_store.get("mappings", MappingsConfig)
        # The schemas this deployment has, not the ones the code declares: a
        # field an administrator added is a field of the family from here on.
        objects = {
            name: ObjectRepository(client, schema, mappings.for_family(name), self._counters)
            for name, schema in mappings.schemas().items()
        }
        return RepositorySet(
            objects=objects,
            ticket_repo=TicketRepository(objects[TICKET_SCHEMA.name]),
            catalog_repo=CatalogRepository(client),
            access_repo=AccessRepository(client),
        )
