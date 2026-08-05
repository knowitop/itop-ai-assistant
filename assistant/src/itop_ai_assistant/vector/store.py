"""ChunkStore — the seam between the indexer and whatever stores the vectors.

The indexer, the sweep and the API speak to storage only through this
protocol. Everything a backend cannot answer on its own — sweep cursors, the
pending-backfill flag, the run journal, cross-replica exclusion — is
operational state and lives in Redis (`vector/sync_state.py`,
`vector/index_journal.py`), never here. See ADR-002 in dev-docs.

Two hashes travel with a chunk, not one. `content_hash` guards the chunk's
text — it comes from the chunker and changes only when the source text does.
`ChunkMetadata.meta_hash` guards everything else the payload carries that
filtering depends on (`visibility`, `status`, `org_id`, `filters`) — it lets
the sweep tell "the object was reopened, only status moved" apart from "the
text changed", and refresh the former without paying for a re-embed
(ADR-004, `update_chunk_metadata`). `created_at` deliberately does not feed
`meta_hash`: the indexer falls back to the sweep's `started_at` when a
source has no creation date, and that fallback is not deterministic across
passes — folding it into the hash would make metadata churn on every sweep
for such objects. See `dev-docs/architecture/vector.md`.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class IndexMeta:
    """The active index version and its model fingerprint (model, dim)."""

    version: int
    model: str
    dim: int


@dataclass(frozen=True)
class IndexStats:
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

    @property
    def meta_hash(self) -> str:
        canonical = json.dumps(
            {"visibility": self.visibility, "status": self.status, "org_id": self.org_id, "filters": self.filters},
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

    async def ensure_version(self, model: str, dim: int) -> IndexMeta: ...

    async def active_meta(self) -> IndexMeta | None: ...

    async def upsert_chunks(self, chunks: list[ChunkRecord], *, model: str, dim: int) -> int: ...

    async def get_chunk_digests(self, obj_class: str, obj_id: int) -> dict[tuple[str, int], ChunkDigest]: ...

    async def update_chunk_metadata(self, chunks: list[ChunkMetadata]) -> int:
        """Overwrite the payload of already-embedded chunks — no vector write.
        Returns 0 when there is no active index version, like the other
        write operations do."""
        ...

    async def delete_chunks(self, obj_class: str, obj_id: int, keys: list[tuple[str, int]]) -> int: ...

    async def delete_object(self, obj_class: str, obj_id: int) -> int: ...

    async def list_object_ids(self, obj_class: str, after: int = 0, limit: int = 1000) -> list[int]: ...

    async def search(
        self,
        embedding: list[float],
        *,
        classes: list[str],
        statuses: list[str],
        visibilities: list[str],
        allowed_orgs: list[str] | None = None,
        exclude_obj_id: int | None = None,
        limit: int = 30,
    ) -> list[SearchHit]: ...

    async def stats(self) -> IndexStats | None: ...

    async def aclose(self) -> None: ...
