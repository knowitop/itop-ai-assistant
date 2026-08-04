"""ChunkStore — the seam between the indexer and whatever stores the vectors.

The indexer, the sweep and the API speak to storage only through this
protocol. Everything a backend cannot answer on its own — sweep cursors, the
pending-backfill flag, the run journal, cross-replica exclusion — is
operational state and lives in Redis (`vector/sync_state.py`,
`vector/index_journal.py`), never here. See ADR-002 in dev-docs.
"""

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
class ChunkRecord:
    """One embedded chunk of an iTop object — ids and filter metadata, no text."""

    obj_class: str
    obj_id: int
    chunk_kind: str  # profile / description / solution / log:public …
    chunk_n: int
    visibility: str  # public / internal
    status: str
    content_hash: str
    embedding: list[float]
    created_at: datetime  # object creation time (time-window KNN later)
    org_id: str | None = None
    filters: dict[str, str] | None = None


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

    async def get_chunk_hashes(self, obj_class: str, obj_id: int) -> dict[tuple[str, int], str]: ...

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
