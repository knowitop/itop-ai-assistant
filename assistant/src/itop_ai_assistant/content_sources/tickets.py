"""Ticket vector source: iTop UserRequest/Incident tickets as vectorizable
objects.

Wraps `ObjectRepository` behind the generic `VectorSource` protocol
(`vector/ports/source.py`) — the vector indexer itself never imports a
repository or a family schema; all of that domain knowledge lives here
instead.

The sweep reads an `ObjectView`, not the typed `Ticket`: what it needs is
text by semantic name, and the typed model exists for the module that reads
`ticket.caller_name` in business logic, not for this.
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from itop_ai_assistant.content_sources.acl import org_ids
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.object_view import LogEntry, ObjectView
from itop_ai_assistant.domain.schema import Role
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA
from itop_ai_assistant.repositories.object_repo import (
    ObjectRepository,
    ObjectRepositoryForPrincipal,
    ObjectRepositoryProvider,
)
from itop_ai_assistant.vector import (
    Chunk,
    ChunkPlan,
    FamilyConfig,
    FragmentContent,
    FragmentSpec,
    SequenceContent,
    TextContent,
    VectorRecord,
    VectorSource,
    chunk_object,
    clean_text,
)

logger = logging.getLogger(__name__)

# The collection family this source writes to (`TicketVectorSource.name`).
FAMILY = TICKET_SCHEMA.name

# The semantic fields an administrator composes the required fragments from
# (ADR-018). Not iTop attribute names: the mapping to those is
# `ticket_mapping`'s job. Derived, not listed: a field is offered here because
# it carries what the ticket is about (`Role.CONTENT`), and a list written out
# by hand is what let this vocabulary call a field `service` while the model
# called it `service_name`.
FIELDS = TICKET_SCHEMA.names(Role.CONTENT)

# The semantic fields of a ticket that can name an organization giving access
# to it (`VectorClassConfig.acl_org_fields` picks from these). One today: a
# build where the organization working the ticket also grants access declares
# that as a semantic field of its own, and it joins this tuple by declaring
# `Role.ORGANIZATION`.
ORG_FIELDS = TICKET_SCHEMA.names(Role.ORGANIZATION)

# Every fragment this source can produce. The two log fragments are opt-in:
# whether internal notes get embedded at all is the administrator's call,
# and `log:private` is the only fragment here that is not caller-facing.
FRAGMENTS = (
    FragmentSpec(kind="profile", visibility="public"),
    FragmentSpec(kind="body", visibility="public"),
    FragmentSpec(kind="solution", visibility="public"),
    FragmentSpec(kind="log:public", visibility="public", optional=True),
    FragmentSpec(kind="log:private", visibility="internal", optional=True),
)

# Which ticket log each opt-in log fragment is built from. Fixed here rather
# than configurable: a fragment's visibility is declared above, and letting
# the private log feed a public fragment would make that declaration a lie.
_LOG_SOURCES = {"log:public": "public_log", "log:private": "private_log"}

# Whose entries count as the requester's when the log is labelled.
_CALLER_NAME = "caller_name"


class TicketVectorSource(VectorSource[ObjectView]):
    """VectorSource implementation for iTop tickets.

    `classes` is taken verbatim from `vector.families.tickets.classes` at
    construction time — `ObjectRepository` is itself generic over any class
    the deployment's `ticket_mapping` covers, so this source imposes no class
    list of its own.

    Source contract (`vector/ports/source.py`): the relevance attribute is the
    field carrying `Role.LIFECYCLE_STATE`, the modification date the one
    carrying `Role.MODIFIED_AT` — both mapped to actual iTop attributes per
    class by `ticket_mapping`.
    """

    name = FAMILY
    # Which payload keys this source asks Qdrant to index. A statement about
    # its own index, not about the field, so it is declared here rather than
    # read off the schema (ADR-034) — the same reasoning that keeps the
    # `filters` keys below out of the schema.
    indexed_filter_keys = ("status",)
    fields = FIELDS
    org_fields = ORG_FIELDS
    fragments = FRAGMENTS

    def __init__(
        self,
        get_repo: ObjectRepositoryProvider,
        get_repo_as: ObjectRepositoryForPrincipal,
        *,
        family_cfg: FamilyConfig,
    ) -> None:
        self._get_repo = get_repo
        self._get_repo_as = get_repo_as
        self.classes: Sequence[str] = list(family_cfg.classes)
        # Per class, which of `ORG_FIELDS` this deployment says grant access.
        # Read once here rather than per record: the source is rebuilt from a
        # freshly read config on every pass (`vector/assembly.py`).
        self._acl_org_fields = {name: cfg.acl_org_fields for name, cfg in family_cfg.classes.items()}
        self._repo: ObjectRepository | None = None

    async def prepare(self) -> None:
        # The service account's view, not a principal's: the sweep is not a
        # run — no journal entry, nobody to act for — and the index it builds is
        # global by design (`dev-docs/architecture/platform.md` §3.5). What a searcher may see
        # is decided later, by `confirm_visible` under their own token. Not just
        # by convention: `get_repo` is bound to `Principal.service()`
        # (see `content_sources/registry.py`), and the principal-bound accessor
        # is a *separate* closure — sweeping as somebody else is not something
        # this class can express.
        self._repo = await self._get_repo()

    async def find_modified_since(
        self, obj_class: str, since: datetime | None, *, page: int, page_size: int
    ) -> list[VectorRecord[ObjectView]]:
        assert self._repo is not None, "prepare() must run before find_modified_since()"
        # Nothing is excluded from the projection: the sweep is the one reader
        # of the private log, and whether it is embedded at all is decided per
        # fragment below, not by leaving it unread.
        tickets = await self._repo.find_modified_since(obj_class, since, page=page, page_size=page_size)
        return [
            VectorRecord(
                obj_id=int(ticket.id),
                index_value=ticket.state_of(Role.LIFECYCLE_STATE),
                updated_at=ticket.moment_of(Role.MODIFIED_AT),
                created_at=ticket.moment_of(Role.CREATED_AT),
                acl_org_ids=org_ids(ticket, self._acl_org_fields.get(obj_class, ()), source=FAMILY),
                filters={"service_id": service_id} if (service_id := ticket.identifier("service_id")) else None,
                payload=ticket,
            )
            for ticket in tickets
        ]

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        assert self._repo is not None, "prepare() must run before find_existing_ids()"
        return await self._repo.find_existing_ids(obj_class, ids)

    async def confirm_visible(self, principal: Principal, obj_class: str, ids: list[int]) -> set[int]:
        # A repository per call, and no `prepare()` in sight: the identity is
        # the caller's, not the sweep's, and caching a set built for one person
        # is exactly how a search would start answering with somebody else's
        # tickets.
        repo = await self._get_repo_as(principal)
        return await repo.find_existing_ids(obj_class, ids)

    async def chunk(
        self,
        obj_class: str,
        record: VectorRecord[ObjectView],
        plan: ChunkPlan,
        *,
        max_chunk_tokens: int,
        log_entries_per_chunk: int,
    ) -> list[Chunk]:
        ticket = record.payload
        fields = self._semantic_fields(ticket)
        fragments = [
            content for spec in FRAGMENTS if (content := self._resolve(spec, plan, ticket, fields)) is not None
        ]
        return chunk_object(fragments, max_chunk_tokens=max_chunk_tokens, items_per_window=log_entries_per_chunk)

    def _semantic_fields(self, ticket: ObjectView) -> dict[str, str]:
        """`FIELDS` bound to this ticket's content, canonicalized."""
        return {name: clean_text(ticket.text(name)) for name in FIELDS}

    def _resolve(
        self, spec: FragmentSpec, plan: ChunkPlan, ticket: ObjectView, fields: dict[str, str]
    ) -> FragmentContent | None:
        """One fragment's configured content, or None if it is switched off."""
        if spec.optional:
            if spec.kind not in plan.enabled:
                return None
            entries = ticket.log(_LOG_SOURCES[spec.kind])
            return FragmentContent(spec.kind, spec.visibility, SequenceContent(_conversation(entries, ticket)))
        field_names = plan.fields.get(spec.kind)
        if not field_names:
            return None
        parts = []
        for name in field_names:
            if name not in fields:
                logger.warning(f"tickets source: fragment {spec.kind!r} references unknown field {name!r} — ignored")
                continue
            if fields[name]:
                parts.append(fields[name])
        return FragmentContent(spec.kind, spec.visibility, TextContent("\n\n".join(parts)))


def _conversation(entries: list[LogEntry], ticket: ObjectView) -> list[str]:
    """Log entries as canonical lines. Who counts as the caller is domain
    knowledge, so the labelling happens here and the chunker only ever sees
    strings."""
    caller = ticket.text(_CALLER_NAME)
    return [f"{'caller' if entry.user_login == caller else 'agent'}: {clean_text(entry.message)}" for entry in entries]
