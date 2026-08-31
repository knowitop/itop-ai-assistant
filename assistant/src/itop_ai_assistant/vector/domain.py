"""Value objects with rules attached — computation and validation the ports
reference but do not own (rule 2.3). `ports/store.py` explains how these are
used in the `ChunkStore` contract; this module owns what they compute and
validate.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto


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
    # Fixed by the source per fragment kind (`FragmentSpec`, ADR-018) and
    # therefore a pure function of `chunk_kind`, which is part of the point's
    # identity — so it cannot change at runtime, and feeding `meta_hash` looks
    # pointless. It is not: a *release* that flips a fragment's declared
    # visibility keeps the same point identity and the same text, and without
    # this field in the hash the index would serve internal chunks as public
    # until their text happened to change. Cheap insurance on an access
    # control, not a runtime concern (TASK-020).
    visibility: str  # public / internal
    content_hash: str
    # Object creation time, frozen at first indexing when the source has none
    # of its own — see `ports/store.py`'s module docstring and
    # `vector/use_cases/indexer.py`.
    created_at: datetime
    filters: dict[str, str | list[str]] | None = None
    # Organizations that may see the object (ADR-003 layer 1). Empty means
    # the source claims no restriction, and the pre-filter lets the object
    # through — written to the payload as an explicit empty list, so a point
    # that claims nothing is distinguishable from one indexed before the key
    # existed.
    acl_org_ids: tuple[str, ...] = ()
    # Last modification of the source object — the range filter behind "solved
    # within the last year". None when the source has no such date: such an
    # object is then invisible to every `updated` window, which is the honest
    # answer (see `ports/store.py`'s module docstring on why there is no
    # fallback).
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
                "acl_org_ids": list(self.acl_org_ids),
                "created_at": self.created_at.isoformat(),
                "filters": self.filters,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                "visibility": self.visibility,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


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
    `created_at` is always present; for a source that reports no creation date
    it holds the moment the object first entered the index — an approximation,
    but a stable one, identical across the object's chunks and unchanged by
    later sweeps (`vector/use_cases/indexer.py`).
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


def left_indexable_scope(index_value: str, index_values: list[str]) -> bool:
    """True when the object's current relevance value falls outside the
    configured indexable set. An empty `index_values` means "index
    everything" — there is no scope to leave."""
    return bool(index_values) and index_value not in index_values


class ChunkSyncState(Enum):
    """What one chunk needs, compared to what is already stored for it."""

    CHANGED = auto()  # re-embed
    STALE_META = auto()  # rewrite the payload, no re-embed
    UNCHANGED = auto()  # nothing to do


def classify_chunk(
    content_hash: str, meta_hash: str, *, stored_content_hash: str | None, stored_meta_hash: str | None
) -> ChunkSyncState:
    """Content wins over metadata: a chunk with no stored digest, or whose
    text changed, is CHANGED even if its metadata also changed — the
    re-embed rewrites the whole payload anyway, including a fresh meta_hash."""
    if stored_content_hash is None or stored_content_hash != content_hash:
        return ChunkSyncState.CHANGED
    if stored_meta_hash != meta_hash:
        return ChunkSyncState.STALE_META
    return ChunkSyncState.UNCHANGED


def creation_date(
    record_created_at: datetime | None,
    record_updated_at: datetime | None,
    stored_created_ats: Iterable[datetime | None],
    started_at: datetime,
) -> datetime:
    """When the object came into being, as the index will remember it.

    The source's own date if it has one. Otherwise whatever the object's
    already-indexed chunks say — that is what freezes the value: the last two
    fallbacks fire once, at first indexing, and every pass afterwards
    inherits. Without that step a fresh `started_at` would enter the payload
    on every rewrite, and since rewrites are per-chunk, the chunks of one
    object would end up claiming different creation dates — visible as an
    object matching a `created` window through some of its chunks and not
    others (TASK-020).

    `min` over the stored values, not "this chunk's own": a chunk added later
    (a new log window) has nothing stored and would otherwise take the current
    clock while its siblings keep the old one. Earliest-known also cannot
    creep forward from pass to pass.

    Stored beats `record_updated_at` deliberately — a modification date moves
    with every edit, so as a fallback it is no better than the sweep's clock.
    """
    if record_created_at is not None:
        return record_created_at
    indexed = [d for d in stored_created_ats if d is not None]
    if indexed:
        return min(indexed)
    return record_updated_at or started_at
