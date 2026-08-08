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
filtering depends on (`visibility`, `filters`, `updated_at`) — it lets the
sweep tell "the object was reopened" apart from "the text changed", and
refresh the former without paying for a re-embed (ADR-004,
`update_chunk_metadata`). `created_at` deliberately does not feed `meta_hash`:
the indexer falls back to the sweep's `started_at` when a source has no
creation date, and that fallback is not deterministic across passes — folding
it into the hash would make metadata churn on every sweep for such objects.
`updated_at` has no such fallback and does feed it, which is what keeps the
"resolved within the last year" filter honest. Making `created_at` filterable
(`DateRange`, TASK-018) does not change that: the general rule is "a payload
field filtering depends on must feed `meta_hash`", and this one field is the
documented exception, bought at the price spelled out in `DateRange`. See
`dev-docs/architecture/vector.md`.
"""

import hashlib
import json
from collections.abc import Sequence
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
    chunk_kind: str  # profile / body / solution / log:public …
    chunk_n: int
    visibility: str  # public / internal
    content_hash: str
    created_at: datetime  # object creation time (time-window KNN later)
    filters: dict[str, str] | None = None
    # Last modification of the source object — the range filter behind "solved
    # within the last year". None when the source has no such date: such an
    # object is then invisible to every `updated` window, which is the honest
    # answer (see the module docstring on why there is no fallback).
    updated_at: datetime | None = None

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
                "filters": self.filters,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                "visibility": self.visibility,
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


@dataclass(frozen=True)
class DateRange:
    """A window over one indexed datetime field — both bounds inclusive.

    One object rather than a pair of scalar parameters per date: each new
    filterable date would otherwise add two more arguments to `search()`
    (TASK-018). Named `after`/`before` and not `gte`/`lte` to keep the port
    backend-agnostic, the same reason `SimilarSearch.find()` says `min_score`
    where the backend says `score_threshold`.

    Either bound may be omitted, but not both: "no restriction" is expressed
    by passing no range at all, so an empty one is a caller mistake — the
    same convention `filters`/`classes`/`chunk_kinds` follow (ADR-017). An
    inverted window (`after > before`) can match nothing and is rejected too.

    An object whose payload has no such date passes **no** range, not even
    one made of a single upper bound: an absent key matches no range
    condition, and "unknown" must read neither as "recent" nor as "old".
    That is the real case for `updated_at`, which a source may leave unset.
    `created_at` is always present, but for a source that reports no creation
    date the indexer falls back to the sweep's own clock — filtering such an
    object by `created` filters by when it was last written to the index, not
    by when it came into being (`vector/indexer.py`, module docstring above).
    """

    after: datetime | None = None
    before: datetime | None = None

    def __post_init__(self) -> None:
        if self.after is None and self.before is None:
            raise ValueError('DateRange got neither bound — omit the range for "unrestricted", not DateRange()')
        if self.after is not None and self.before is not None and self.after > self.before:
            raise ValueError(
                f"DateRange is inverted: after={self.after.isoformat()} > before={self.before.isoformat()}"
            )


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
