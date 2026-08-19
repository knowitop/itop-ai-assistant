"""ChunkStore — the seam between the indexer and whatever stores the vectors.

The indexer, the sweep and the API speak to storage only through this
protocol. Everything a backend cannot answer on its own — sweep cursors, the
pending-backfill flag, the run journal, cross-replica exclusion — is
operational state and lives in Redis (`vector/state/sync_state.py`,
`vector/state/index_journal.py`), never here. See ADR-002 in dev-docs.

`family` selects which collection a call goes to (one per `VectorSource`,
see ADR-015) — it is never stored on `ChunkMetadata` or written to a chunk's
payload, since it names *where* an object lives, not a property of the
object itself.

Two hashes travel with a chunk, not one. `content_hash` guards the chunk's
text — it comes from the chunker and changes only when the source text does.
`ChunkMetadata.meta_hash` guards everything else the payload carries that
filtering depends on (`visibility`, `filters`, both dates) — it lets the sweep
tell "the object was reopened" apart from "the text changed", and refresh the
former without paying for a re-embed (ADR-004, `update_chunk_metadata`). The
rule has no exceptions: a payload field filtering depends on feeds the hash,
or it freezes at indexing time and no one notices.

`created_at` used to be that exception, because the indexer fell back to the
sweep's own clock for a source with no creation date and a fallback that moves
between passes would rewrite every payload on every sweep. It is in the hash
now because the value no longer moves: the indexer freezes it at first
indexing (`ChunkDigest.created_at` carries the stored value back, TASK-020),
so the fallback fires exactly once per object and the hash stays stable. What
that buys, beyond consistency, is repair — correcting a source's creation date
(mapping `created_at` where it was absent) now reaches the index instead of
losing to a value written years earlier. See `dev-docs/architecture/vector.md`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from itop_ai_assistant.vector.domain import ChunkMetadata, DateRange


@dataclass(frozen=True)
class IndexMeta:
    """The active index version and its model fingerprint (model, dim)."""

    family: str
    version: int
    model: str
    dim: int


@dataclass(frozen=True)
class IndexStats:
    family: str
    version: int
    rows: int


@dataclass(frozen=True)
class ChunkDigest:
    """What's stored for one chunk, cheap to compare against a fresh one.

    `meta_hash` is `None` for a chunk written before this field existed —
    that reads as "metadata stale", triggering one cheap payload rewrite.

    `created_at` travels back to the indexer rather than being compared here:
    it is what a source with no creation date of its own inherits instead of
    the sweep's current clock, which is what keeps one object's chunks from
    drifting apart (TASK-020). `None` when the stored payload has no readable
    date — then there is nothing to inherit and the fallback runs as before.
    """

    content_hash: str
    meta_hash: str | None
    created_at: datetime | None = None


@dataclass(frozen=True)
class ChunkRecord:
    """One embedded chunk of an iTop object, ready to write.

    Composition, not inheritance: `ChunkMetadata`'s trailing fields have
    defaults, so `embedding` couldn't follow them as a dataclass field
    without one of its own — and a default embedding invites writing an
    empty vector by accident.
    """

    meta: ChunkMetadata
    embedding: list[float]


@dataclass(frozen=True)
class SearchHit:
    """One object found, not one chunk — `score` is its best chunk's.

    `obj_class` travels with the id because a search spans several classes at
    once: without it the caller cannot say which class the id belongs to, and
    the source is asked per class.
    """

    obj_class: str
    obj_id: int
    score: float


class FingerprintMismatchError(Exception):
    """The active index was built with a different model/dim — rebuild required."""


@runtime_checkable
class ChunkStore(Protocol):
    """Vector storage. Nothing about *when* to index lives here."""

    @property
    def configured(self) -> bool:
        """False when no connection is configured — the deployment runs without vectors."""
        ...

    async def ensure_version(
        self, family: str, model: str, dim: int, *, filter_keys: Sequence[str] = ()
    ) -> IndexMeta: ...

    async def active_meta(self, family: str) -> IndexMeta | None: ...

    async def list_families(self) -> list[str]:
        """Every family that has ever had an active version, read from
        storage itself — not from whatever sources are registered in code
        today. A family a deployment stopped indexing stays observable here
        until its collection is dropped."""
        ...

    async def upsert_chunks(self, chunks: list[ChunkRecord], *, family: str, model: str, dim: int) -> int: ...

    async def get_chunk_digests(
        self, family: str, obj_class: str, obj_id: int
    ) -> dict[tuple[str, int], ChunkDigest]: ...

    async def update_chunk_metadata(self, chunks: list[ChunkMetadata], *, family: str) -> int:
        """Overwrite the payload of already-embedded chunks — no vector write.
        Returns 0 when there is no active index version, like the other
        write operations do."""
        ...

    async def delete_chunks(self, family: str, obj_class: str, obj_id: int, keys: list[tuple[str, int]]) -> int: ...

    async def delete_object(self, family: str, obj_class: str, obj_id: int) -> int: ...

    async def list_object_ids(self, family: str, obj_class: str, after: int = 0, limit: int = 1000) -> list[int]: ...

    async def search(
        self,
        embedding: list[float],
        *,
        family: str,
        classes: list[str] | None = None,
        visibilities: list[str],
        chunk_kinds: list[str] | None = None,
        filters: dict[str, list[str]] | None = None,
        exclude: tuple[str, int] | None = None,
        created: DateRange | None = None,
        updated: DateRange | None = None,
        score_threshold: float | None = None,
        limit: int = 30,
    ) -> list[SearchHit]:
        """`exclude` is one (obj_class, obj_id) pair — the asking object
        itself. `created`/`updated` are windows over the two system dates,
        each bound inclusive and each optional; an object indexed without an
        `updated_at` passes no `updated` window at all (see `DateRange`).
        `score_threshold` drops a hit below that similarity score regardless
        of rank — top-N alone does not mean relevant (TASK-011); `None`
        applies no such floor. `chunk_kinds`: `None` matches any kind, an
        empty list is a caller error."""
        ...

    async def stats(self, family: str) -> IndexStats | None: ...

    async def aclose(self) -> None: ...
