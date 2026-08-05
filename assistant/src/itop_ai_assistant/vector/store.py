"""ChunkStore — the seam between the indexer and whatever stores the vectors.

The indexer, the sweep and the API speak to storage only through this
protocol. Everything a backend cannot answer on its own — sweep cursors, the
pending-backfill flag, the run journal, cross-replica exclusion — is
operational state and lives in Redis (`vector/sync_state.py`,
`vector/index_journal.py`), never here. See ADR-002 in dev-docs.

`family` selects which collection a call goes to (one per `VectorSource`,
see ADR-015) — it is never stored on `ChunkMetadata` or written to a chunk's
payload, since it names *where* an object lives, not a property of the
object itself.

Two hashes travel with a chunk, not one. `content_hash` guards the chunk's
text — it comes from the chunker and changes only when the source text does.
`ChunkMetadata.meta_hash` guards everything else the payload carries that
filtering depends on (`visibility`, `status`, `org_id`, `filters`,
`last_update`) — it lets the sweep tell "the object was reopened, only
status moved" apart from "the text changed", and refresh the former without
paying for a re-embed (ADR-004, `update_chunk_metadata`). `created_at`
deliberately does not feed `meta_hash`: the indexer falls back to the
sweep's `started_at` when a source has no creation date, and that fallback
is not deterministic across passes — folding it into the hash would make
metadata churn on every sweep for such objects. `last_update` has no such
fallback and does feed it, which is what keeps the "resolved within the last
year" filter honest. See `dev-docs/architecture/vector.md`.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


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
class ChunkMetadata:
    """Everything a chunk's payload carries — no vector.

    `meta_hash` is a canonical digest of the filterable fields only
    (identifiers are the comparison key, not part of what they identify;
    `content_hash` is the other hash and has its own role). Sorted keys and
    `ensure_ascii=False` keep the digest stable across processes.
    """

    obj_class: str
    obj_id: int
    chunk_kind: str  # profile / description / solution / log:public …
    chunk_n: int
    visibility: str  # public / internal
    status: str
    content_hash: str
    created_at: datetime  # object creation time (time-window KNN later)
    org_id: str | None = None
    filters: dict[str, str] | None = None
    # Last modification of the source object — the range filter behind "solved
    # within the last year". None when the source has no such date: such an
    # object is then invisible to every `updated_after` search, which is the
    # honest answer (see the module docstring on why there is no fallback).
    last_update: datetime | None = None

    @property
    def obj_key(self) -> str:
        """Identity of the object across classes — the grouping key of a search.

        `obj_id` alone is unique only within one root class hierarchy: iTop
        allocates it in the root table, so all `Ticket` subclasses share one
        numbering, while `KnowledgeBaseArticle` and friends have their own.
        """
        return f"{self.obj_class}:{self.obj_id}"

    @property
    def meta_hash(self) -> str:
        canonical = json.dumps(
            {
                "visibility": self.visibility,
                "status": self.status,
                "org_id": self.org_id,
                "filters": self.filters,
                "last_update": self.last_update.isoformat() if self.last_update else None,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ChunkDigest:
    """What's stored for one chunk, cheap to compare against a fresh one.

    `meta_hash` is `None` for a chunk written before this field existed —
    that reads as "metadata stale", triggering one cheap payload rewrite.
    """

    content_hash: str
    meta_hash: str | None


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

    async def ensure_version(self, family: str, model: str, dim: int) -> IndexMeta: ...

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
        classes: list[str],
        statuses: list[str],
        visibilities: list[str],
        allowed_orgs: list[str] | None = None,
        exclude: tuple[str, int] | None = None,
        updated_after: datetime | None = None,
        limit: int = 30,
    ) -> list[SearchHit]:
        """`exclude` is one (obj_class, obj_id) pair — the asking object
        itself. `updated_after` keeps objects modified at/after that moment;
        an object indexed without a `last_update` never passes it."""
        ...

    async def stats(self, family: str) -> IndexStats | None: ...

    async def aclose(self) -> None: ...
