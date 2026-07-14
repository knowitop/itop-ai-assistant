"""VectorSource — the contract a content source implements to be swept by
VectorIndexer.

The indexer knows nothing about iTop, tickets, or any other domain: it reads
`VectorRecord`s from registered sources, hands them back for chunking, and
writes the resulting `Chunk`s through `VectorIndex`. Adding a new source
(KB articles, KnownErrors, ...) means writing a new `src/vector_sources/
<name>.py` module implementing this protocol and registering it in
`vector_sources/registry.py` — no change needed here or in `vector/indexer.py`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from vector.chunker import Chunk


@dataclass(frozen=True)
class VectorRecord:
    """One object as returned by a source's sweep page: identity and the
    fields the generic indexer needs to decide what to embed/delete.

    `payload` is opaque to the indexer — whatever the source's own `chunk()`
    needs to build the object's chunks (e.g. the domain `Ticket`).
    """

    obj_id: int
    status: str
    last_update: datetime | None
    created_at: datetime | None
    org_id: str | None = None
    # Source-defined pre-filter keys stored as-is in the chunk rows' `filters`
    # column (e.g. {"service_id": "5"} for tickets) — short scalar values
    # only, see vector/models.py.
    filters: dict[str, str] | None = None
    payload: object = None


class VectorSource(Protocol):
    """One pluggable content source the vector indexer can sweep."""

    name: str
    classes: Sequence[str]  # obj_class values this source currently owns

    async def prepare(self) -> None:
        """Called once per sweep pass, before any of this source's classes
        are read — reset per-sweep caches (e.g. lookups that must not go
        stale mid-sweep but also must not be cached forever)."""
        ...

    async def find_modified_since(
        self, obj_class: str, since: datetime | None, *, page: int, page_size: int
    ) -> list[VectorRecord]:
        """One page of objects modified at/after `since` (None = full scan).

        Must include objects that left the indexable scope (e.g. status
        changed) so the indexer can delete their chunks — no status filter
        here, `VectorConfig.index_statuses` is applied by the caller."""
        ...

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        """Which of the given ids still exist at the source (reconciliation probe)."""
        ...

    async def chunk(
        self,
        obj_class: str,
        record: VectorRecord,
        profile: dict[str, list[str]],
        *,
        max_chunk_tokens: int,
        log_entries_per_chunk: int,
    ) -> list[Chunk]:
        """Build this object's chunks according to the class's chunking profile."""
        ...
