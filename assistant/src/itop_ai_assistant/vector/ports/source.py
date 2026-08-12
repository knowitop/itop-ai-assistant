"""VectorSource — the contract a content source implements to be swept by
VectorIndexer.

The indexer knows nothing about iTop, tickets, or any other domain: it reads
`VectorRecord`s from registered sources, hands them back for chunking, and
writes the resulting `Chunk`s through the `ChunkStore` port. Adding a new source
(KB articles, KnownErrors, ...) means writing a new `vector/sources/<name>.py`
module implementing this protocol and registering it in
`vector/sources/registry.py` — no change needed here or in
`vector/use_cases/indexer.py`.

The contract: every indexed class must expose (a) a last-modification
datetime (`VectorRecord.updated_at` — drives the sweep cursor) and (b) a
"relevance" attribute whose current value (`VectorRecord.index_value`) tells
whether the object belongs in the index. Which attributes those are is the
source's own mapping concern (for tickets: semantic `status`/`updated_at`
via `ticket_mapping`); the per-class value lists live in
`vector.families[<family>].classes[<class>].index_values`.

A source also declares its own chunking vocabulary — `fields` and
`fragments` (ADR-018). That declaration is what the admin UI renders its
editor from, so a source that omits it cannot be configured by anyone but a
person editing raw config.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Protocol, TypeVar

from itop_ai_assistant.config import VectorClassConfig
from itop_ai_assistant.vector.chunker import Chunk

# The source's own payload type (e.g. `Ticket`, `FaqArticle`) — carried through
# `VectorRecord`/`VectorSource` so each source's `chunk()` gets it back typed,
# with the indexer itself (which only ever sees `VectorSource[Any]`) none the
# wiser.
T = TypeVar("T")


@dataclass(frozen=True)
class FragmentSpec:
    """One fragment a source can produce, as declared by the source itself
    (ADR-018) — the vocabulary the admin UI renders its editor from.

    Visibility belongs here and nowhere else: the source is written for a
    business scenario and is the only party that knows its private log is
    private. The config cannot express it at all, which is what keeps a
    security control from being a setting.

    `optional` marks the fragments where the administrator has a real choice.
    A required fragment is always indexed and the config only says which
    semantic fields feed it (adapting to a customized iTop datamodel); an
    opt-in fragment carries no fields of its own and is switched on or off
    (for tickets: whether internal notes get embedded at all).
    """

    kind: str
    visibility: str  # public / internal
    optional: bool = False


@dataclass(frozen=True)
class VectorRecord(Generic[T]):
    """One object as returned by a source's sweep page: identity and the
    fields the generic indexer needs to decide what to embed/delete.

    `payload` is opaque to the indexer (which only ever handles
    `VectorRecord[Any]`) — whatever the source's own `chunk()` needs to build
    the object's chunks (e.g. the domain `Ticket`), typed back for it via `T`.
    """

    obj_id: int
    # Current value of the class's relevance attribute (status for tickets) —
    # compared against `vector.families[<family>].classes[<class>].index_values`
    # by the indexer
    index_value: str
    updated_at: datetime | None
    created_at: datetime | None
    payload: T
    org_id: str | None = None
    # Source-defined pre-filter keys stored as-is in the chunk rows' `filters`
    # column (e.g. {"service_id": "5"} for tickets) — short scalar values
    # only, see vector/models.py.
    filters: dict[str, str] | None = None


class VectorSource(Protocol[T]):
    """One pluggable content source the vector indexer can sweep.

    Generic over its own payload type `T` (e.g. `Ticket`, `FaqArticle`) so a
    concrete source's `chunk()` gets `record.payload` back typed, with no
    cast — the indexer itself stores sources as `VectorSource[Any]`, since it
    never touches `payload`.
    """

    name: str
    classes: Sequence[str]  # obj_class values this source currently owns
    # Which keys from VectorRecord.filters this source wants indexed in Qdrant
    # (fields.<key>), see qdrant_store.py::_ensure_payload_indexes.
    indexed_filter_keys: Sequence[str] = ()
    # Semantic field names an administrator may compose required fragments
    # from — the source's own vocabulary, not iTop attribute names.
    fields: Sequence[str] = ()
    # Every fragment this source can produce. Served to the admin UI by
    # GET /api/vector/sources.
    fragments: Sequence[FragmentSpec] = ()

    async def prepare(self) -> None:
        """Called once per sweep pass, before any of this source's classes
        are read — reset per-sweep caches (e.g. lookups that must not go
        stale mid-sweep but also must not be cached forever)."""
        ...

    async def find_modified_since(
        self, obj_class: str, since: datetime | None, *, page: int, page_size: int
    ) -> list[VectorRecord[T]]:
        """One page of objects modified at/after `since` (None = full scan).

        Must include objects that left the indexable scope (e.g. status
        changed) so the indexer can delete their chunks — no relevance filter
        here, `vector.families[<family>].classes[<class>].index_values` is
        applied by the caller."""
        ...

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        """Which of the given ids still exist at the source (reconciliation probe)."""
        ...

    async def chunk(
        self,
        obj_class: str,
        record: VectorRecord[T],
        cfg: VectorClassConfig,
        *,
        max_chunk_tokens: int,
        log_entries_per_chunk: int,
    ) -> list[Chunk]:
        """Build this object's chunks from `cfg.chunks`.

        Resolving the config against the source's own datamodel happens here,
        not in the chunker: which field name means what, which fragment reads
        which log, and what to do with a field name the source does not know
        (warn and treat as empty — a stale config must not fail a sweep)."""
        ...
