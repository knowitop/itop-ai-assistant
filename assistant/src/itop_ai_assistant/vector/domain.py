"""Value objects with rules attached — computation and validation the ports
reference but do not own (rule 2.3). `ports/store.py` explains how these are
used in the `ChunkStore` contract; this module owns what they compute and
validate.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime


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
    filters: dict[str, str] | None = None
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
