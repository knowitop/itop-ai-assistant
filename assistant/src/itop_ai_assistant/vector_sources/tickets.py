"""Ticket vector source: iTop UserRequest/Incident tickets as vectorizable
objects.

Wraps `TicketRepository` behind the generic `VectorSource` protocol
(`vector/source.py`) — the vector indexer itself never imports `Ticket` or
`ItopBundle`; all of that domain knowledge lives here instead.
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from itop_ai_assistant.config import ChunkFragmentConfig, VectorClassConfig
from itop_ai_assistant.domain.ticket import LogEntry, Ticket
from itop_ai_assistant.ticket_repository import TicketRepoFactory, TicketRepository
from itop_ai_assistant.vector.chunker import (
    Chunk,
    FragmentContent,
    SequenceContent,
    TextContent,
    chunk_object,
    clean_text,
)
from itop_ai_assistant.vector.source import FragmentSpec, VectorRecord, VectorSource

logger = logging.getLogger(__name__)

# The collection family this source writes to (`TicketVectorSource.name`) and
# the one `SimilarSearch` in `agents/intake/pipeline.py` reads from — one
# constant so the two never drift apart (TASK-008).
FAMILY = "tickets"

# The semantic fields an administrator composes the required fragments from
# (ADR-018). Not iTop attribute names: the mapping to those is
# `ticket_mapping`'s job, and `_semantic_fields` below is the one place these
# names are bound to actual ticket content.
FIELDS = ("title", "description", "solution", "service", "subcategory")

# Every fragment this source can produce. The two log fragments are opt-in:
# whether internal notes get embedded at all is the administrator's call
# (TASK-013), and `log:private` is the only fragment here that is not
# caller-facing.
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


class TicketVectorSource(VectorSource):
    """VectorSource implementation for iTop tickets.

    `classes` is taken verbatim from `vector.classes` at construction time —
    `TicketRepository` is itself generic over any class the deployment's
    `ticket_mapping` covers, so this source imposes no class list of its own.

    Source contract (`vector/source.py`): the relevance attribute for tickets
    is the semantic `status`, the modification date is the semantic
    `last_update` — both mapped to actual iTop attributes per class by
    `ticket_mapping` (`find_modified_since` raises if `last_update` is not
    mapped for a class).
    """

    name = FAMILY
    indexed_filter_keys = ("status", "org_id")
    fields = FIELDS
    fragments = FRAGMENTS

    def __init__(self, get_ticket_repo: TicketRepoFactory, *, classes: list[str]) -> None:
        self._get_ticket_repo = get_ticket_repo
        self.classes: Sequence[str] = classes
        self._ticket_repo: TicketRepository | None = None

    async def prepare(self) -> None:
        # The plain connection, not a principal's view of it: the sweep is not a
        # run — no journal entry, nobody to act for — and the index it builds is
        # global by design (`dev-docs/architecture/platform.md` §3.5). What a searcher may see
        # is decided later, by resolving hits under their own token. Not just by
        # convention: `get_ticket_repo` is bound to `ItopProvider.ticket_repo`,
        # which never touches `for_principal` — this class has no way to reach it.
        self._ticket_repo = await self._get_ticket_repo()

    async def find_modified_since(
        self, obj_class: str, since: datetime | None, *, page: int, page_size: int
    ) -> list[VectorRecord]:
        assert self._ticket_repo is not None, "prepare() must run before find_modified_since()"
        tickets = await self._ticket_repo.find_modified_since(
            obj_class, since, page=page, page_size=page_size, include_private_log=True
        )
        return [
            VectorRecord(
                obj_id=int(ticket.id),
                index_value=ticket.status,
                updated_at=ticket.last_update,
                created_at=ticket.start_date,
                org_id=ticket.org_id,
                filters={"service_id": ticket.service_id} if ticket.has_service else None,
                payload=ticket,
            )
            for ticket in tickets
        ]

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        assert self._ticket_repo is not None, "prepare() must run before find_existing_ids()"
        return await self._ticket_repo.find_existing_ids(obj_class, ids)

    async def chunk(
        self,
        obj_class: str,
        record: VectorRecord,
        cfg: VectorClassConfig,
        *,
        max_chunk_tokens: int,
        log_entries_per_chunk: int,
    ) -> list[Chunk]:
        ticket: Ticket = record.payload  # type: ignore[assignment] # TODO: generic type for VectorRecord.payload
        fields = self._semantic_fields(ticket)
        fragments = [
            content
            for spec in FRAGMENTS
            if (content := self._resolve(spec, cfg.chunks.get(spec.kind), ticket, fields)) is not None
        ]
        return chunk_object(fragments, max_chunk_tokens=max_chunk_tokens, items_per_window=log_entries_per_chunk)

    def _semantic_fields(self, ticket: Ticket) -> dict[str, str]:
        """`FIELDS` bound to this ticket's content, canonicalized.

        The one place the two are tied together — `test_vector_sources_tickets`
        keeps the key set equal to `FIELDS`, so the vocabulary served to the
        admin UI cannot drift away from what is actually chunkable.
        """
        return {
            "title": clean_text(ticket.title),
            "description": clean_text(ticket.description),
            "solution": clean_text(ticket.solution),
            "service": clean_text(ticket.service_name),
            "subcategory": clean_text(ticket.subcategory_name),
        }

    def _resolve(
        self, spec: FragmentSpec, cfg: ChunkFragmentConfig | None, ticket: Ticket, fields: dict[str, str]
    ) -> FragmentContent | None:
        """One fragment's configured content, or None if it is switched off."""
        if spec.optional:
            if cfg is None or not cfg.enabled:
                return None
            entries: list[LogEntry] = getattr(ticket, _LOG_SOURCES[spec.kind])
            return FragmentContent(spec.kind, spec.visibility, SequenceContent(_conversation(entries, ticket)))
        if cfg is None or not cfg.fields:
            return None
        parts = []
        for name in cfg.fields:
            if name not in fields:
                logger.warning(f"tickets source: fragment {spec.kind!r} references unknown field {name!r} — ignored")
                continue
            if fields[name]:
                parts.append(fields[name])
        return FragmentContent(spec.kind, spec.visibility, TextContent("\n\n".join(parts)))


def _conversation(entries: list[LogEntry], ticket: Ticket) -> list[str]:
    """Log entries as canonical lines. Who counts as the caller is domain
    knowledge, so the labelling happens here and the chunker only ever sees
    strings."""
    return [
        f"{'caller' if entry.user_login == ticket.caller_name else 'agent'}: {clean_text(entry.message)}"
        for entry in entries
    ]
