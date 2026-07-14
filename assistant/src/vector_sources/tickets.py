"""Ticket vector source: iTop UserRequest/Incident tickets as vectorizable
objects.

Wraps `TicketRepository` + `CatalogRepository` behind the generic
`VectorSource` protocol (`vector/source.py`) — the vector indexer itself
never imports `Ticket`, `ItopBundle`, or `CatalogRepository`; all of that
domain knowledge lives here instead.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from catalog_repository import CatalogRepository
from domain.ticket import LogEntry, Ticket
from vector.chunker import Chunk, ConversationEntry, chunk_object
from vector.source import VectorRecord

if TYPE_CHECKING:
    from deps import AppDeps, ItopBundle


class _CatalogNames:
    """Per-sweep memo of service/subcategory id → name (profile chunk text)."""

    def __init__(self, catalog: CatalogRepository):
        self._catalog = catalog
        self._services: dict[str, str] = {}
        self._subcategories: dict[str, str] = {}

    async def service(self, ticket: Ticket) -> str:
        if not ticket.has_service:
            return ""
        if ticket.service_id not in self._services:
            service = await self._catalog.get_service(ticket.service_id)
            self._services[ticket.service_id] = service.name if service else ""
        return self._services[ticket.service_id]

    async def subcategory(self, ticket: Ticket) -> str:
        if not ticket.has_subcategory:
            return ""
        if ticket.subcategory_id not in self._subcategories:
            subcategory = await self._catalog.get_subcategory(ticket.subcategory_id)
            self._subcategories[ticket.subcategory_id] = subcategory.name if subcategory else ""
        return self._subcategories[ticket.subcategory_id]


class TicketVectorSource:
    """VectorSource implementation for iTop tickets.

    `classes` is taken verbatim from `vector.classes` at construction time —
    `TicketRepository` is itself generic over any class the deployment's
    `ticket_mapping` covers, so this source imposes no class list of its own.
    """

    name = "tickets"

    def __init__(self, deps: "AppDeps", *, classes: list[str]) -> None:
        self._deps = deps
        self.classes = classes
        self._bundle: "ItopBundle | None" = None
        self._names: _CatalogNames | None = None

    async def prepare(self) -> None:
        self._bundle = await self._deps.itop.get()
        self._names = _CatalogNames(self._bundle.catalog_repo)

    async def find_modified_since(
        self, obj_class: str, since: datetime | None, *, page: int, page_size: int
    ) -> list[VectorRecord]:
        assert self._bundle is not None, "prepare() must run before find_modified_since()"
        tickets = await self._bundle.ticket_repo.find_modified_since(obj_class, since, page=page, page_size=page_size)
        return [
            VectorRecord(
                obj_id=int(ticket.id),
                status=ticket.status,
                last_update=ticket.last_update,
                created_at=ticket.created_at,
                org_id=ticket.org_id,
                filters={"service_id": ticket.service_id} if ticket.has_service else None,
                payload=ticket,
            )
            for ticket in tickets
        ]

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        assert self._bundle is not None, "prepare() must run before find_existing_ids()"
        return await self._bundle.ticket_repo.find_existing_ids(obj_class, ids)

    async def chunk(
        self,
        obj_class: str,
        record: VectorRecord,
        profile: dict[str, list[str]],
        *,
        max_chunk_tokens: int,
        log_entries_per_chunk: int,
    ) -> list[Chunk]:
        assert self._names is not None, "prepare() must run before chunk()"
        ticket: Ticket = record.payload  # type: ignore[assignment]
        fields = {
            "title": ticket.title,
            "description": ticket.description,
            "solution": ticket.solution,
            "service": await self._names.service(ticket),
            "subcategory": await self._names.subcategory(ticket),
        }
        logs = {"log:public": _to_conversation(ticket.public_log, ticket.caller_name)}
        return chunk_object(
            fields,
            profile,
            max_chunk_tokens=max_chunk_tokens,
            log_entries_per_chunk=log_entries_per_chunk,
            logs=logs,
        )


def _to_conversation(entries: list[LogEntry], caller_name: str) -> list[ConversationEntry]:
    return [
        ConversationEntry(speaker="caller" if entry.user_login == caller_name else "agent", message=entry.message)
        for entry in entries
    ]
