"""ChunkStore on Qdrant.

Why Qdrant and not a relational vector extension: filtering happens during
the graph walk instead of after it, grouping by object is built in, and new
filter keys need no migration (ADR-001 in dev-docs).

Collection layout. One collection per (family, index version),
`{family}_v{N}` — a family is a `VectorSource.name`, e.g. `tickets` — with a
named dense vector and an empty named sparse slot. The sparse slot is
created from the start and stays unused: reserving it costs nothing, adding
it to a live collection of hundreds of thousands of points costs a rebuild
(ADR-007). Splitting by family, not by `obj_class`, keeps one HNSW graph per
independent business scenario instead of one graph serving all of them
(ADR-015) — a source with several classes (tickets: UserRequest, Incident)
still shares one collection, since "similar past tickets" is asked across
both at once.

The (family, model, dim) fingerprint lives in a tiny `chunks_meta` collection
rather than in Redis, so the guard against mixing incomparable vectors sits
with the vectors it guards. It carries one active row per family
simultaneously. Its points carry a 1-dimensional placeholder vector because
Qdrant has no notion of a point without one.

Payloads carry ids, filter metadata and the content hash — never the text
of a ticket.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from qdrant_client import AsyncQdrantClient, models

from itop_ai_assistant.vector.store import (
    ChunkDigest,
    ChunkMetadata,
    ChunkRecord,
    FingerprintMismatchError,
    IndexMeta,
    IndexStats,
    SearchHit,
)

_META_COLLECTION = "chunks_meta"
_DENSE = "dense"
_SPARSE = "sparse"
_SCROLL_PAGE = 256
# Stable namespace so a chunk's point id is a pure function of its identity —
# re-indexing the same chunk must overwrite it, never add a twin.
_ID_NAMESPACE = uuid.UUID("6f0f5f8e-6a1d-5c2b-9a3e-0c1d2e3f4a5b")

# System keys only: `fields.*` (source-defined pre-filter keys, D6/TASK-008)
# rides unindexed under its own nested key by default — indexing a specific
# `fields.*` key waits for a real filtering scenario (ADR-005). A source opts
# its own generic keys (e.g. `status`, `org_id`) into indexing by declaring
# them in `VectorSource.indexed_filter_keys`, passed here as `filter_keys`.
_KEYWORD_FIELDS = ("obj_class", "chunk_kind", "visibility", "obj_key")


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
    def collection_name(family: str, version: int) -> str:
        return f"{family}_v{version}"

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def active_meta(self, family: str) -> IndexMeta | None:
        if not await self.client.collection_exists(_META_COLLECTION):
            return None
        records, _ = await self.client.scroll(
            collection_name=_META_COLLECTION,
            scroll_filter=_family_filter(family),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if not records:
            return None
        payload = records[0].payload
        return IndexMeta(family=family, version=payload["version"], model=payload["model"], dim=payload["dim"])

    async def list_families(self) -> list[str]:
        """Every family with an active row in `chunks_meta`, read from
        storage rather than from the currently-registered sources."""
        if not await self.client.collection_exists(_META_COLLECTION):
            return []
        families: list[str] = []
        seen: set[str] = set()
        offset = None
        while True:
            records, offset = await self.client.scroll(
                collection_name=_META_COLLECTION,
                scroll_filter=models.Filter(
                    must=[models.FieldCondition(key="is_active", match=models.MatchValue(value=True))]
                ),
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=["family"],
                with_vectors=False,
            )
            for record in records:
                family = record.payload["family"]
                if family not in seen:
                    seen.add(family)
                    families.append(family)
            if offset is None:
                return families

    async def ensure_version(self, family: str, model: str, dim: int, *, filter_keys: Sequence[str] = ()) -> IndexMeta:
        meta = await self.active_meta(family)
        if meta is not None:
            _check_fingerprint(meta, model, dim)
            # Also for a collection that already exists: a payload index added
            # by a later release would otherwise never appear on a deployment
            # that was provisioned before it (a filter would still work, by
            # full scan). Creating an existing index is a no-op for Qdrant.
            await self._ensure_payload_indexes(family, meta.version, filter_keys)
            return meta
        version = 1
        await self._create_meta_collection()
        await self._create_chunk_collection(family, version, dim, filter_keys)
        await self.client.upsert(
            collection_name=_META_COLLECTION,
            points=[
                models.PointStruct(
                    id=_meta_point_id(family, version),
                    vector=[0.0],
                    payload={"family": family, "version": version, "model": model, "dim": dim, "is_active": True},
                )
            ],
            wait=True,
        )
        return IndexMeta(family=family, version=version, model=model, dim=dim)

    async def upsert_chunks(self, chunks: list[ChunkRecord], *, family: str, model: str, dim: int) -> int:
        """Idempotent insert-or-update by (obj_class, obj_id, chunk_kind, chunk_n)."""
        if not chunks:
            return 0
        meta = _require_active(await self.active_meta(family))
        _check_fingerprint(meta, model, dim)
        points = [
            models.PointStruct(
                id=_point_id(c.meta.obj_class, c.meta.obj_id, c.meta.chunk_kind, c.meta.chunk_n),
                vector={_DENSE: c.embedding},
                payload=_payload(c.meta),
            )
            for c in chunks
        ]
        await self.client.upsert(collection_name=self.collection_name(family, meta.version), points=points, wait=True)
        return len(points)

    async def get_chunk_digests(self, family: str, obj_class: str, obj_id: int) -> dict[tuple[str, int], ChunkDigest]:
        """Stored digests of one object, keyed by (chunk_kind, chunk_n).
        `meta_hash` is `None` for a point written before that field existed."""
        meta = await self.active_meta(family)
        if meta is None:
            return {}
        digests: dict[tuple[str, int], ChunkDigest] = {}
        offset = None
        while True:
            records, offset = await self.client.scroll(
                collection_name=self.collection_name(family, meta.version),
                scroll_filter=_object_filter(obj_class, obj_id),
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=["chunk_kind", "chunk_n", "content_hash", "meta_hash"],
                with_vectors=False,
            )
            for record in records:
                payload = record.payload
                digests[(payload["chunk_kind"], payload["chunk_n"])] = ChunkDigest(
                    content_hash=payload["content_hash"], meta_hash=payload.get("meta_hash")
                )
            if offset is None:
                return digests

    async def update_chunk_metadata(self, chunks: list[ChunkMetadata], *, family: str) -> int:
        """Overwrite the payload of already-embedded chunks — vector untouched.

        One `OverwritePayloadOperation` per point: each chunk's payload
        differs, so a single batched `overwrite_payload` (which applies one
        payload to many points) cannot cover them. `overwrite_`, not
        `set_payload` (merge): a key `filters` no longer sends must
        disappear from the point, and the indexer already has the full
        payload here, so a merge would leave stale keys behind forever.
        """
        if not chunks:
            return 0
        meta = await self.active_meta(family)
        if meta is None:
            return 0
        operations = [
            models.OverwritePayloadOperation(
                overwrite_payload=models.SetPayload(
                    payload=_payload(c),
                    points=[_point_id(c.obj_class, c.obj_id, c.chunk_kind, c.chunk_n)],
                )
            )
            for c in chunks
        ]
        await self.client.batch_update_points(
            collection_name=self.collection_name(family, meta.version), update_operations=operations, wait=True
        )
        return len(operations)

    async def stats(self, family: str) -> IndexStats | None:
        meta = await self.active_meta(family)
        if meta is None:
            return None
        info = await self.client.get_collection(self.collection_name(family, meta.version))
        return IndexStats(family=family, version=meta.version, rows=info.points_count or 0)

    async def delete_object(self, family: str, obj_class: str, obj_id: int) -> int:
        """Delete every chunk of one object. Returns points deleted."""
        meta = await self.active_meta(family)
        if meta is None:
            return 0
        name = self.collection_name(family, meta.version)
        selector = _object_filter(obj_class, obj_id)
        # Qdrant's delete does not report how much it removed, and the sweep
        # report counts deletions — so count first, then delete.
        removed = (await self.client.count(collection_name=name, count_filter=selector, exact=True)).count
        if removed:
            await self.client.delete(
                collection_name=name, points_selector=models.FilterSelector(filter=selector), wait=True
            )
        return removed

    async def delete_chunks(self, family: str, obj_class: str, obj_id: int, keys: list[tuple[str, int]]) -> int:
        """Delete specific chunks of one object (vanished kinds/ordinals)."""
        if not keys:
            return 0
        meta = await self.active_meta(family)
        if meta is None:
            return 0
        point_ids = [_point_id(obj_class, obj_id, kind, n) for kind, n in keys]
        await self.client.delete(
            collection_name=self.collection_name(family, meta.version),
            points_selector=models.PointIdsList(points=point_ids),
            wait=True,
        )
        return len(point_ids)

    async def list_object_ids(self, family: str, obj_class: str, after: int = 0, limit: int = 1000) -> list[int]:
        """Distinct indexed obj_ids > `after`, ascending — keyset pagination
        for the reconciliation walk.

        An object has several chunks, so the ordered scroll returns each id
        as many times as it has chunks; pages are read until `limit` distinct
        ids are collected or the class runs out.
        """
        meta = await self.active_meta(family)
        if meta is None:
            return []
        found: list[int] = []
        seen: set[int] = set()
        offset = None
        while len(found) < limit:
            records, offset = await self.client.scroll(
                collection_name=self.collection_name(family, meta.version),
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(key="obj_class", match=models.MatchValue(value=obj_class)),
                        models.FieldCondition(key="obj_id", range=models.Range(gt=after)),
                    ]
                ),
                order_by=models.OrderBy(key="obj_id", direction=models.Direction.ASC),
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=["obj_id"],
                with_vectors=False,
            )
            for record in records:
                obj_id = record.payload["obj_id"]
                if obj_id not in seen:
                    seen.add(obj_id)
                    found.append(obj_id)
                    if len(found) == limit:
                        break
            if offset is None:
                break
        return found

    async def search(
        self,
        embedding: list[float],
        *,
        family: str,
        classes: list[str] | None = None,
        visibilities: list[str],
        filters: dict[str, list[str]] | None = None,
        exclude: tuple[str, int] | None = None,
        updated_after: datetime | None = None,
        score_threshold: float | None = None,
        limit: int = 30,
    ) -> list[SearchHit]:
        """Filtered nearest neighbours aggregated to objects: the score of an
        object is its best chunk's, computed server-side by `group_by`.

        Filters are applied during the walk, not over its result — the
        property the backend was chosen for (ADR-001, R1). `classes=None`
        searches the whole family; an absent key in `filters` means
        unrestricted for that key — an empty list under either is a caller
        mistake, not "no results", and is rejected loudly. `score_threshold`
        is the same idea applied to the score itself, native to
        `query_points_groups` — a candidate below it never reaches the
        result, so it costs nothing extra beyond a plain top-N walk. Returns
        [] when no index version exists yet.

        Grouping is by `obj_key`, not by `obj_id`: the id is unique only
        within one root class hierarchy, and a search spans several classes.
        The class of a hit is read from its best chunk's payload rather than
        parsed back out of the group id — one format less to keep in sync.
        """
        meta = await self.active_meta(family)
        if meta is None:
            return []
        must: list[models.Condition] = [
            models.FieldCondition(key="visibility", match=models.MatchAny(any=visibilities)),
        ]
        if classes is not None:
            if not classes:
                raise ValueError('search classes got an empty list — omit the argument for "whole family", not []')
            must.append(models.FieldCondition(key="obj_class", match=models.MatchAny(any=classes)))
        for key, values in (filters or {}).items():
            if not values:
                raise ValueError(
                    f'search filter {key!r} got an empty value list — omit the key for "unrestricted", not []'
                )
            must.append(models.FieldCondition(key=f"fields.{key}", match=models.MatchAny(any=values)))
        if updated_after is not None:
            must.append(
                models.FieldCondition(key="updated_at", range=models.DatetimeRange(gte=updated_after.isoformat()))
            )
        must_not: list[models.Condition] = []
        if exclude is not None:
            must_not.append(
                models.FieldCondition(key="obj_key", match=models.MatchValue(value=f"{exclude[0]}:{exclude[1]}"))
            )
        response = await self.client.query_points_groups(
            collection_name=self.collection_name(family, meta.version),
            query=embedding,
            using=_DENSE,
            query_filter=models.Filter(must=must, must_not=must_not or None),
            group_by="obj_key",
            group_size=1,
            limit=limit,
            score_threshold=score_threshold,
            with_payload=["obj_class", "obj_id"],
        )
        return [
            SearchHit(
                obj_class=group.hits[0].payload["obj_class"],
                obj_id=int(group.hits[0].payload["obj_id"]),
                score=float(group.hits[0].score),
            )
            for group in response.groups
            if group.hits
        ]

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
        await self.client.create_payload_index(
            collection_name=_META_COLLECTION, field_name="family", field_schema=models.PayloadSchemaType.KEYWORD
        )

    async def _create_chunk_collection(
        self, family: str, version: int, dim: int, filter_keys: Sequence[str] = ()
    ) -> None:
        name = self.collection_name(family, version)
        await self.client.create_collection(
            collection_name=name,
            vectors_config={_DENSE: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
            sparse_vectors_config={_SPARSE: models.SparseVectorParams(index=models.SparseIndexParams())},
        )
        await self._ensure_payload_indexes(family, version, filter_keys)

    async def _ensure_payload_indexes(self, family: str, version: int, filter_keys: Sequence[str] = ()) -> None:
        name = self.collection_name(family, version)
        for field in _KEYWORD_FIELDS:
            await self.client.create_payload_index(
                collection_name=name, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD
            )
        for key in filter_keys:
            await self.client.create_payload_index(
                collection_name=name, field_name=f"fields.{key}", field_schema=models.PayloadSchemaType.KEYWORD
            )
        await self.client.create_payload_index(
            collection_name=name, field_name="obj_id", field_schema=models.PayloadSchemaType.INTEGER
        )
        # DATETIME, not KEYWORD: this is the field range conditions are built
        # on ("modified within the last year"), and Qdrant parses RFC-3339 and
        # compares in UTC itself.
        await self.client.create_payload_index(
            collection_name=name, field_name="updated_at", field_schema=models.PayloadSchemaType.DATETIME
        )


def _point_id(obj_class: str, obj_id: int, chunk_kind: str, chunk_n: int) -> str:
    """Qdrant accepts a UUID or an unsigned int as a point id; the composite
    key becomes a deterministic UUID and stays in the payload for filtering."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"{obj_class}:{obj_id}:{chunk_kind}:{chunk_n}"))


def _meta_point_id(family: str, version: int) -> str:
    """A bare `version` int collides across families (`tickets` v1 and
    `kb_articles` v1 are different rows) — namespaced the same way chunk
    point ids already are."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"{family}:{version}"))


def _family_filter(family: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="is_active", match=models.MatchValue(value=True)),
            models.FieldCondition(key="family", match=models.MatchValue(value=family)),
        ]
    )


def _payload(chunk: ChunkMetadata) -> dict:
    """The one builder both write paths share (`upsert_chunks`,
    `update_chunk_metadata`) — anything that goes into `meta_hash` must be
    written here too, or it silently freezes at indexing-time forever."""
    payload = {
        "obj_class": chunk.obj_class,
        "obj_id": chunk.obj_id,
        # Grouping key of a search: obj_id repeats across root class hierarchies
        "obj_key": chunk.obj_key,
        "chunk_kind": chunk.chunk_kind,
        "chunk_n": chunk.chunk_n,
        "visibility": chunk.visibility,
        "content_hash": chunk.content_hash,
        "meta_hash": chunk.meta_hash,
        "created_at": chunk.created_at.isoformat() if isinstance(chunk.created_at, datetime) else chunk.created_at,
    }
    if chunk.updated_at is not None:
        # Absent rather than null when the source has no such date: a null
        # would sort as a value in a range condition, an absent key never
        # matches one — and "unknown" must not read as "recent".
        payload["updated_at"] = (
            chunk.updated_at.isoformat() if isinstance(chunk.updated_at, datetime) else chunk.updated_at
        )
    # Source-defined pre-filter keys (e.g. `status`, `org_id` for tickets) nest
    # under `fields` so a source's own key can never shadow a system key of
    # the same name (D6, TASK-008). Not indexed automatically — a source opts
    # specific keys in via `indexed_filter_keys` (see `_KEYWORD_FIELDS`, ADR-005).
    payload["fields"] = chunk.filters or {}
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
