"""ChunkStore on Qdrant.

Why Qdrant and not pgvector: filtering happens during the graph walk instead
of after it, grouping by object is built in, and new filter keys need no
migration (ADR-001 in dev-docs).

Collection layout. One collection per index version, `chunks_v{N}`, with a
named dense vector and an empty named sparse slot. The sparse slot is
created from the start and stays unused: reserving it costs nothing, adding
it to a live collection of hundreds of thousands of points costs a rebuild
(ADR-007).

The (model, dim) fingerprint lives in a tiny `chunks_meta` collection rather
than in Redis, so the guard against mixing incomparable vectors sits with
the vectors it guards. Its points carry a 1-dimensional placeholder vector
because Qdrant has no notion of a point without one.

Payloads carry ids, filter metadata and the content hash — never the text
of a ticket.
"""

import uuid
from datetime import datetime

from qdrant_client import AsyncQdrantClient, models

from itop_ai_assistant.vector.store import (
    ChunkRecord,
    FingerprintMismatchError,
    IndexMeta,
    IndexStats,
)

_META_COLLECTION = "chunks_meta"
_DENSE = "dense"
_SPARSE = "sparse"
_SCROLL_PAGE = 256
# Stable namespace so a chunk's point id is a pure function of its identity —
# re-indexing the same chunk must overwrite it, never add a twin.
_ID_NAMESPACE = uuid.UUID("6f0f5f8e-6a1d-5c2b-9a3e-0c1d2e3f4a5b")

_KEYWORD_FIELDS = ("obj_class", "chunk_kind", "visibility", "status", "org_id")


class QdrantNotConfigured(RuntimeError):
    """Raised when the store is used while `qdrant_url` is unset."""


class QdrantChunkStore:
    """Lazy holder of the async Qdrant client (unconfigured when the URL is None)."""

    def __init__(self, url: str | None) -> None:
        self._url = url
        self._client: AsyncQdrantClient | None = None

    @property
    def configured(self) -> bool:
        return self._url is not None

    @property
    def client(self) -> AsyncQdrantClient:
        if self._url is None:
            raise QdrantNotConfigured("qdrant_url is not set — the vector store is unavailable")
        if self._client is None:
            self._client = AsyncQdrantClient(location=self._url)
        return self._client

    @staticmethod
    def collection_name(version: int) -> str:
        return f"chunks_v{version}"

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def active_meta(self) -> IndexMeta | None:
        if not await self.client.collection_exists(_META_COLLECTION):
            return None
        records, _ = await self.client.scroll(
            collection_name=_META_COLLECTION,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="is_active", match=models.MatchValue(value=True))]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if not records:
            return None
        payload = records[0].payload
        return IndexMeta(version=payload["version"], model=payload["model"], dim=payload["dim"])

    async def ensure_version(self, model: str, dim: int) -> IndexMeta:
        meta = await self.active_meta()
        if meta is not None:
            _check_fingerprint(meta, model, dim)
            return meta
        version = 1
        await self._create_meta_collection()
        await self._create_chunk_collection(version, dim)
        await self.client.upsert(
            collection_name=_META_COLLECTION,
            points=[
                models.PointStruct(
                    id=version,
                    vector=[0.0],
                    payload={"version": version, "model": model, "dim": dim, "is_active": True},
                )
            ],
            wait=True,
        )
        return IndexMeta(version=version, model=model, dim=dim)

    async def upsert_chunks(self, chunks: list[ChunkRecord], *, model: str, dim: int) -> int:
        """Idempotent insert-or-update by (obj_class, obj_id, chunk_kind, chunk_n)."""
        if not chunks:
            return 0
        meta = _require_active(await self.active_meta())
        _check_fingerprint(meta, model, dim)
        points = [
            models.PointStruct(
                id=_point_id(c.obj_class, c.obj_id, c.chunk_kind, c.chunk_n),
                vector={_DENSE: c.embedding},
                payload=_payload(c),
            )
            for c in chunks
        ]
        await self.client.upsert(collection_name=self.collection_name(meta.version), points=points, wait=True)
        return len(points)

    async def get_chunk_hashes(self, obj_class: str, obj_id: int) -> dict[tuple[str, int], str]:
        """Stored content hashes of one object, keyed by (chunk_kind, chunk_n)."""
        meta = await self.active_meta()
        if meta is None:
            return {}
        hashes: dict[tuple[str, int], str] = {}
        offset = None
        while True:
            records, offset = await self.client.scroll(
                collection_name=self.collection_name(meta.version),
                scroll_filter=_object_filter(obj_class, obj_id),
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=["chunk_kind", "chunk_n", "content_hash"],
                with_vectors=False,
            )
            for record in records:
                payload = record.payload
                hashes[(payload["chunk_kind"], payload["chunk_n"])] = payload["content_hash"]
            if offset is None:
                return hashes

    async def stats(self) -> IndexStats | None:
        meta = await self.active_meta()
        if meta is None:
            return None
        info = await self.client.get_collection(self.collection_name(meta.version))
        return IndexStats(version=meta.version, rows=info.points_count or 0)

    async def _create_meta_collection(self) -> None:
        if await self.client.collection_exists(_META_COLLECTION):
            return
        await self.client.create_collection(
            collection_name=_META_COLLECTION,
            # Placeholder: Qdrant has no point without a vector, and this
            # collection is a metadata table, never searched by similarity.
            vectors_config=models.VectorParams(size=1, distance=models.Distance.COSINE),
        )
        await self.client.create_payload_index(
            collection_name=_META_COLLECTION, field_name="is_active", field_schema=models.PayloadSchemaType.BOOL
        )

    async def _create_chunk_collection(self, version: int, dim: int) -> None:
        name = self.collection_name(version)
        await self.client.create_collection(
            collection_name=name,
            vectors_config={_DENSE: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
            sparse_vectors_config={_SPARSE: models.SparseVectorParams(index=models.SparseIndexParams())},
        )
        for field in _KEYWORD_FIELDS:
            await self.client.create_payload_index(
                collection_name=name, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD
            )
        await self.client.create_payload_index(
            collection_name=name, field_name="obj_id", field_schema=models.PayloadSchemaType.INTEGER
        )


def _point_id(obj_class: str, obj_id: int, chunk_kind: str, chunk_n: int) -> str:
    """Qdrant accepts a UUID or an unsigned int as a point id; the composite
    key becomes a deterministic UUID and stays in the payload for filtering."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"{obj_class}:{obj_id}:{chunk_kind}:{chunk_n}"))


def _payload(chunk: ChunkRecord) -> dict:
    payload = {
        "obj_class": chunk.obj_class,
        "obj_id": chunk.obj_id,
        "chunk_kind": chunk.chunk_kind,
        "chunk_n": chunk.chunk_n,
        "visibility": chunk.visibility,
        "status": chunk.status,
        "org_id": chunk.org_id,
        "content_hash": chunk.content_hash,
        "created_at": chunk.created_at.isoformat() if isinstance(chunk.created_at, datetime) else chunk.created_at,
    }
    # Source-defined pre-filter keys ride flat alongside the rest so they can
    # be indexed and filtered like any other field (see ADR-005).
    payload.update(chunk.filters or {})
    return payload


def _object_filter(obj_class: str, obj_id: int) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="obj_class", match=models.MatchValue(value=obj_class)),
            models.FieldCondition(key="obj_id", match=models.MatchValue(value=obj_id)),
        ]
    )


def _require_active(meta: IndexMeta | None) -> IndexMeta:
    if meta is None:
        raise FingerprintMismatchError("No active index version — call ensure_version first")
    return meta


def _check_fingerprint(meta: IndexMeta, model: str, dim: int) -> None:
    if (meta.model, meta.dim) != (model, dim):
        raise FingerprintMismatchError(
            f"Active index v{meta.version} was built with ({meta.model!r}, dim={meta.dim}); "
            f"current config is ({model!r}, dim={dim}) — rebuild the index before writing"
        )
