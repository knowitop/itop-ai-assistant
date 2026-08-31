"""What a search asks for and what it answers with — both as values.

`SearchQuery` is the whole scenario in one object: which set to search, what
to match against, which filters and windows apply, how deep to look. It is a
value the caller can build from its own configuration, validate before the
run and store — not a list of arguments assembled at the call site (TASK-031).
`SimilarSearch.find()` takes it whole and returns a `SearchResult`.

One family per query, deliberately. The tempting generalization — several sets
at once, merged by score — is unsound here: each family carries its own active
embeddings model in `chunks_meta` (`adapters/qdrant_store.py`), so a re-index
of one family onto a new model leaves the two scoring on different scales, and
a single `min_score` then cuts differently in each. If a scenario ever needs
two sets, the answer is a result grouped per set, not one merged ranking —
see `dev-docs/tasks/TASK-031-search-query-value/`.

Validation lives here rather than in the search body: a scenario read from
configuration should fail where it is built, not halfway through a run
(`architecture/principles.md`, 6.6).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from itop_ai_assistant.vector.domain import DateRange


@dataclass(frozen=True)
class SearchQuery:
    """One search scenario, ready to hand to the subsystem.

    `candidates` is how many neighbours are asked of the index; `top` caps
    what is returned after the source has confirmed them under the asking
    principal (ADR-003). The first must be at least the second: candidates get
    dropped by that confirmation, never added, so `top > candidates` cannot
    return `top` objects — it would quietly cap the answer at `candidates`
    while the configured `top` did nothing.

    Empty `text` is not an error: it means "nothing to match on" and yields no
    hits, which is a normal outcome for a caller whose object has no text yet.
    An empty *list*, on the other hand, is always a mistake — "unrestricted"
    is expressed by omitting the restriction (ADR-017), the same convention
    `ChunkStore.search()` enforces on its own arguments.
    """

    text: str
    family: str
    classes: list[str] | None = None
    chunk_kinds: list[str] | None = None
    filters: dict[str, list[str]] | None = None
    # The asking principal's organizations (ADR-003 layer 1). None = no
    # pre-filter, which is also what iTop's own "no restriction" normalizes
    # to (`AccessRepository.allowed_org_ids`). Not a key in `filters`: an
    # object that names no organization passes this one, and nothing else
    # behaves that way.
    org_ids: list[str] | None = None
    visibilities: Sequence[str] = ("public", "internal")
    exclude: tuple[str, int] | None = None
    created: DateRange | None = None
    updated: DateRange | None = None
    # Named `min_score` rather than the backend's own `score_threshold` to keep
    # the contract backend-agnostic, same as `candidates` translating to
    # `limit`.
    min_score: float | None = None
    candidates: int = 15
    top: int = 5

    def __post_init__(self) -> None:
        if self.classes is not None and not self.classes:
            raise ValueError('SearchQuery.classes got an empty list — omit it for "whole family", not []')
        if self.chunk_kinds is not None and not self.chunk_kinds:
            raise ValueError('SearchQuery.chunk_kinds got an empty list — omit it for "any kind", not []')
        for key, values in (self.filters or {}).items():
            if not values:
                raise ValueError(
                    f'SearchQuery.filters[{key!r}] got an empty value list — omit the key for "unrestricted", not []'
                )
        if self.org_ids is not None and not self.org_ids:
            raise ValueError('SearchQuery.org_ids got an empty list — omit it for "no pre-filter", not []')
        if self.min_score is not None and not -1.0 <= self.min_score <= 1.0:
            raise ValueError(f"SearchQuery.min_score is a cosine score in [-1, 1], got {self.min_score}")
        if self.candidates < 1 or self.top < 1:
            raise ValueError(f"SearchQuery needs candidates >= 1 and top >= 1, got {self.candidates}/{self.top}")
        if self.top > self.candidates:
            raise ValueError(
                f"SearchQuery asks for top={self.top} out of candidates={self.candidates}: "
                "candidates are only ever dropped by the source's confirmation, never added, "
                "so top must not exceed them"
            )


@dataclass(frozen=True)
class ObjectHit:
    obj_class: str
    obj_id: int
    score: float


@dataclass(frozen=True)
class FindStats:
    """What a `find()` call actually saw, for the run journal (TASK-014).

    `found < requested` is a signal, not proof: it happens both when
    `min_score` cut candidates and when the index/filter combination has
    fewer matching objects than `requested` to begin with — Qdrant's
    `query_points_groups` applies `score_threshold` natively and returns no
    count of what it dropped, so telling the two apart exactly would need a
    second query. `dropped_by_resolve` is the ADR-003 metric.
    """

    requested: int
    found: int
    dropped_by_resolve: int


@dataclass(frozen=True)
class SearchResult:
    """Hits with the counts that explain them.

    One value rather than a `(hits, stats)` pair: both consumers read both
    parts, and the pair would have to grow a third element the moment anything
    else about a search becomes observable.
    """

    hits: list[ObjectHit]
    stats: FindStats
