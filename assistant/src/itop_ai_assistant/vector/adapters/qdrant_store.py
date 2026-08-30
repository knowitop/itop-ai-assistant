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
with the vectors it guards. Its points carry a 1-dimensional placeholder
vector because Qdrant has no notion of a point without one.

`chunks_meta` carries one row per (family, version), not one per family: an
active row every family has, plus — while its embeddings model is being
changed — one `is_active=False` row for the version being filled to replace
it. That is what makes changing the model recoverable at all. The old
collection stays whole and readable until the new one is complete, so
putting the previous model back in the config is a config edit and not a
second rebuild; and because the row lives here rather than in Redis, a
wiped Redis cannot make `ensure_version` create a collection over one that
already exists.

Which version an operation lands on is decided here (`_write_meta` versus
`active_meta`) rather than passed in — see `ports/store.py` for why
`get_chunk_digests` follows the writes.

Payloads carry ids, filter metadata and the content hash — never the text
of a ticket.
"""

import logging
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from qdrant_client import AsyncQdrantClient, models

from itop_ai_assistant.vector.ports.store import (
    ChunkDigest,
    ChunkMetadata,
    ChunkRecord,
    ChunkStore,
    DateRange,
    FingerprintMismatchError,
    IndexMeta,
    IndexStats,
    SearchHit,
)

logger = logging.getLogger(__name__)

_META_COLLECTION = "chunks_meta"
_DENSE = "dense"
_SPARSE = "sparse"
_SCROLL_PAGE = 256
# How long a memoized set of versions may be trusted. It could be trusted
# forever while a family's active version never changed after being created;
# a rotation changes it, and a replica that did not perform the switch itself
# would otherwise hold the retired version's name until the process restarts.
# What that costs is bounded — the read path refuses a family whose
# fingerprint does not match the configured model (`use_cases/search.py`), so
# a stale entry keeps search closed rather than sending it to a dropped
# collection — but it has to end on its own, and this is what ends it.
_META_CACHE_TTL_SECONDS = 30.0
# Qdrant refuses a request larger than 32 MB (default service.max_request_size_mb),
# and the sweep writes a whole object in one call — an object with a thousand
# chunks would exceed it. The batch is derived from the vector width rather
# than fixed, because `EmbeddingsConfig.dimension` has no upper bound to keep
# a constant in step with: it declares whatever the configured model returns.
_UPSERT_REQUEST_BYTES = 8 * 1024 * 1024
_BYTES_PER_DIMENSION = 21  # a float of this magnitude, as JSON
_POINT_PAYLOAD_BYTES = 1024
# The same ceiling for the calls that carry no vectors: a payload rewrite is
# ~1 KB per point and a delete is a bare id, so they can go far wider.
_PAYLOAD_BATCH = 1024
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


@dataclass(frozen=True)
class _FamilyVersions:
    """Everything `chunks_meta` says about one family, read in one go.

    Both halves come from the same scroll because every caller needs to know
    about both: an operation picks between them, and `ensure_version` decides
    what to do with the pair.
    """

    active: IndexMeta | None
    building: IndexMeta | None

    @property
    def highest(self) -> int:
        return max((m.version for m in (self.active, self.building) if m is not None), default=0)


class QdrantChunkStore(ChunkStore):
    """Lazy holder of the async Qdrant client (unconfigured when the URL is None)."""

    def __init__(self, url: str | None) -> None:
        self._url = url
        self._client: AsyncQdrantClient | None = None
        # Every operation needs a version to build a collection name, so
        # reading `chunks_meta` used to cost two round-trips per store call —
        # and the sweep calls the store once per *object* (TASK-020). This is a
        # cache, not operational state: it decides nothing, dies with the
        # process and is rebuilt from `chunks_meta` itself, so the rule that
        # keeps cursors and journals out of the port (`.claude/rules/vector.md`)
        # does not reach it. Entries expire (`_META_CACHE_TTL_SECONDS`), which
        # they did not have to while a family's active version never moved.
        self._meta_cache: dict[str, tuple[float, _FamilyVersions]] = {}

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
        self._meta_cache.clear()
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def active_meta(self, family: str) -> IndexMeta | None:
        """The version search reads."""
        return (await self._versions(family)).active

    async def pending_meta(self, family: str) -> IndexMeta | None:
        """The version being filled to replace the active one, if any."""
        return (await self._versions(family)).building

    async def _write_meta(self, family: str) -> IndexMeta | None:
        """The version index maintenance acts on: whichever is being filled.

        Every write goes here and never to `active_meta`, so there is no way
        to write into the collection search is reading while it is being
        replaced. `get_chunk_digests` follows this one too, though it reads —
        see `ports/store.py`.
        """
        versions = await self._versions(family)
        return versions.building or versions.active

    async def _versions(self, family: str) -> _FamilyVersions:
        """Both of the family's versions, read once per TTL and remembered.

        A family with no active version is deliberately *not* remembered:
        "no index yet" is the state `ensure_version` exists to leave, and
        caching it would answer "there is no index" to everything asking
        before the first sweep of the process gets that far.
        """
        cached = self._meta_cache.get(family)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        versions = await self._read_versions(family)
        if versions.active is not None:
            self._remember(family, versions)
        return versions

    def _remember(self, family: str, versions: _FamilyVersions) -> None:
        self._meta_cache[family] = (time.monotonic() + _META_CACHE_TTL_SECONDS, versions)

    async def _read_versions(self, family: str) -> _FamilyVersions:
        """`chunks_meta` for one family, straight from storage.

        The newest row wins within each group. For the active group that is
        not paranoia about duplicates but the rule that makes `activate_version`
        safe to be interrupted: it promotes the new version before retiring
        the old one, so a crash in between leaves two active rows, and the
        newer of them is the one whose collection is full.
        """
        if not await self.client.collection_exists(_META_COLLECTION):
            return _FamilyVersions(active=None, building=None)
        rows: list[IndexMeta] = []
        offset = None
        while True:
            records, offset = await self.client.scroll(
                collection_name=_META_COLLECTION,
                scroll_filter=models.Filter(
                    must=[models.FieldCondition(key="family", match=models.MatchValue(value=family))]
                ),
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            rows.extend(_meta_of(record.payload) for record in records)
            if offset is None:
                break
        return _FamilyVersions(
            active=max((m for m in rows if m.is_active), key=_by_version, default=None),
            building=max((m for m in rows if not m.is_active), key=_by_version, default=None),
        )

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
        """The version to fill for this fingerprint, rotating if it changed.

        Four cases, and the last two are what keeps a changed model from
        wedging the family:

        - nothing indexed yet — `v1`, active at once: there is no older index
          to answer searches from meanwhile, so nothing is gained by filling
          it in the background;
        - the active version matches — it is returned, and a half-filled
          replacement (if the model was put back before it finished) is
          dropped, since nothing will ever read it now;
        - a replacement under this fingerprint is already being filled — it is
          returned, and the pass carries on filling it;
        - neither matches — a new version, one past the highest this family
          has ever had, `is_active=False`. Any replacement being filled under
          the previous fingerprint is dropped first: it is as incomparable
          with the new model as the active version is, and no one reads it.

        Version numbers are never reused, which is what lets a caller tell one
        rebuild from the next by the number alone.
        """
        # The only writer of `chunks_meta`, hence the only place the memoized
        # versions can go stale — dropped before the write, refilled below or
        # by the next reader.
        self._meta_cache.pop(family, None)
        versions = await self._read_versions(family)
        active, building = versions.active, versions.building
        if active is None:
            return await self._create_version(family, 1, model, dim, filter_keys, is_active=True)
        if (active.model, active.dim) == (model, dim):
            if building is not None:
                logger.info(
                    f"vector index: {family!r} is back on ({model!r}, dim={dim}) — "
                    f"dropping the unfinished v{building.version}"
                )
                await self._drop_version(building)
            # Payload indexes also for a collection that already exists: one
            # added by a later release would otherwise never appear on a
            # deployment provisioned before it (a filter would still work, by
            # full scan). Creating an existing index is a no-op for Qdrant.
            await self._ensure_payload_indexes(family, active.version, filter_keys)
            self._remember(family, _FamilyVersions(active=active, building=None))
            return active
        if building is not None and (building.model, building.dim) == (model, dim):
            await self._ensure_payload_indexes(family, building.version, filter_keys)
            self._remember(family, versions)
            return building
        version = versions.highest + 1
        if building is not None:
            await self._drop_version(building)
        logger.info(
            f"vector index: {family!r} v{active.version} was built with ({active.model!r}, dim={active.dim}), "
            f"the configured model is ({model!r}, dim={dim}) — filling v{version} next to it; "
            f"searches over {family!r} are refused until it is complete"
        )
        return await self._create_version(family, version, model, dim, filter_keys, is_active=False)

    async def activate_version(self, family: str, version: int) -> None:
        """Promote a filled version and retire what it replaces.

        Promote first, retire second: interrupted after the promotion, the
        family has two active rows and `_read_versions` picks the newer one —
        the full collection. The other order would leave it with none, and
        `ensure_version` would then set about creating a `v1` whose collection
        already exists.

        A no-op when `version` is not the version being filled any more:
        another replica finished the same rebuild first, and its result is as
        good as this one's.
        """
        versions = await self._read_versions(family)
        if versions.building is None or versions.building.version != version:
            logger.info(f"vector index: {family!r} v{version} is no longer the version being filled — not switching")
            return
        promoted = IndexMeta(
            family=family,
            version=version,
            model=versions.building.model,
            dim=versions.building.dim,
            is_active=True,
        )
        await self.client.upsert(
            collection_name=_META_COLLECTION,
            points=[
                models.PointStruct(id=_meta_point_id(family, version), vector=[0.0], payload=_meta_payload(promoted))
            ],
            wait=True,
        )
        if versions.active is not None:
            await self._drop_version(versions.active)
        self._remember(family, _FamilyVersions(active=promoted, building=None))
        logger.info(
            f"vector index: {family!r} now answers from v{version} ({promoted.model!r}, dim={promoted.dim})"
            + (f", v{versions.active.version} dropped" if versions.active is not None else "")
        )

    async def _create_version(
        self, family: str, version: int, model: str, dim: int, filter_keys: Sequence[str], *, is_active: bool
    ) -> IndexMeta:
        meta = IndexMeta(family=family, version=version, model=model, dim=dim, is_active=is_active)
        await self._create_meta_collection()
        await self._create_chunk_collection(family, version, dim, filter_keys)
        await self.client.upsert(
            collection_name=_META_COLLECTION,
            points=[models.PointStruct(id=_meta_point_id(family, version), vector=[0.0], payload=_meta_payload(meta))],
            wait=True,
        )
        versions = await self._read_versions(family)
        self._remember(family, versions)
        return meta

    async def _drop_version(self, meta: IndexMeta) -> None:
        """Forget a version and delete its vectors — only ever one nothing reads.

        Row first, collection second, for the reason `activate_version`
        explains: a collection left behind costs disk until someone notices,
        a row left behind costs the next `create_collection` a 409 and the
        family a rebuild that cannot start.
        """
        await self.client.delete(
            collection_name=_META_COLLECTION,
            points_selector=models.PointIdsList(points=[_meta_point_id(meta.family, meta.version)]),
            wait=True,
        )
        name = self.collection_name(meta.family, meta.version)
        if await self.client.collection_exists(name):
            await self.client.delete_collection(name)

    async def upsert_chunks(self, chunks: list[ChunkRecord], *, family: str, model: str, dim: int) -> int:
        """Idempotent insert-or-update by (obj_class, obj_id, chunk_kind, chunk_n).

        Written in batches sized for `dim` (see `_upsert_batch`): point ids
        are deterministic, so a repeated batch overwrites itself and the split
        costs nothing on the happy path. It does cost the all-or-nothing
        write an object used to get: if a later batch fails, the earlier ones
        stay committed and the object is searchable half-indexed until a
        sweep gets through it whole.
        """
        if not chunks:
            return 0
        meta = _require_version(await self._write_meta(family))
        _check_fingerprint(meta, model, dim)
        points = [
            models.PointStruct(
                id=_point_id(c.meta.obj_class, c.meta.obj_id, c.meta.chunk_kind, c.meta.chunk_n),
                vector={_DENSE: c.embedding},
                payload=_payload(c.meta),
            )
            for c in chunks
        ]
        name = self.collection_name(family, meta.version)
        for batch in _batches(points, _upsert_batch(dim)):
            await self.client.upsert(collection_name=name, points=batch, wait=True)
        return len(points)

    async def get_chunk_digests(self, family: str, obj_class: str, obj_id: int) -> dict[tuple[str, int], ChunkDigest]:
        """Stored digests of one object, keyed by (chunk_kind, chunk_n).
        `meta_hash` is `None` for a point written before that field existed.

        Reads the version being *written* — while one is being filled, this
        is what tells the sweep the object is not in it yet. Against the
        active version the hashes would match, nothing would be embedded, and
        the replacement would be switched on empty.
        """
        meta = await self._write_meta(family)
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
                with_payload=["chunk_kind", "chunk_n", "content_hash", "meta_hash", "created_at"],
                with_vectors=False,
            )
            for record in records:
                payload = record.payload
                digests[(payload["chunk_kind"], payload["chunk_n"])] = ChunkDigest(
                    content_hash=payload["content_hash"],
                    meta_hash=payload.get("meta_hash"),
                    created_at=_parse_datetime(payload.get("created_at")),
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

        Split into `_PAYLOAD_BATCH` requests for the same reason the upsert
        is: a fragment's `filters` or `visibility` changing re-hashes the
        metadata of every chunk of every object, so this list is as long as
        the whole object here too.
        """
        if not chunks:
            return 0
        meta = await self._write_meta(family)
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
        name = self.collection_name(family, meta.version)
        for batch in _batches(operations, _PAYLOAD_BATCH):
            await self.client.batch_update_points(collection_name=name, update_operations=batch, wait=True)
        return len(operations)

    async def stats(self, family: str, version: int | None = None) -> IndexStats | None:
        """Row count of the active version, or of the one `version` names."""
        if version is None:
            meta = await self.active_meta(family)
            if meta is None:
                return None
            version = meta.version
        name = self.collection_name(family, version)
        if not await self.client.collection_exists(name):
            return None
        info = await self.client.get_collection(name)
        return IndexStats(family=family, version=version, rows=info.points_count or 0)

    async def delete_object(self, family: str, obj_class: str, obj_id: int) -> int:
        """Delete every chunk of one object. Returns points deleted."""
        meta = await self._write_meta(family)
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
        meta = await self._write_meta(family)
        if meta is None:
            return 0
        point_ids = [_point_id(obj_class, obj_id, kind, n) for kind, n in keys]
        name = self.collection_name(family, meta.version)
        for batch in _batches(point_ids, _PAYLOAD_BATCH):
            await self.client.delete(collection_name=name, points_selector=models.PointIdsList(points=batch), wait=True)
        return len(point_ids)

    async def list_object_ids(self, family: str, obj_class: str, after: int = 0, limit: int = 1000) -> list[int]:
        """Distinct indexed obj_ids > `after`, ascending — keyset pagination
        for the reconciliation walk.

        An object has several chunks, so the ordered scroll returns each id
        as many times as it has chunks; pages are read until `limit` distinct
        ids are collected or the class runs out.
        """
        meta = await self._write_meta(family)
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
        chunk_kinds: list[str] | None = None,
        filters: dict[str, list[str]] | None = None,
        exclude: tuple[str, int] | None = None,
        created: DateRange | None = None,
        updated: DateRange | None = None,
        score_threshold: float | None = None,
        limit: int = 30,
    ) -> list[SearchHit]:
        """Filtered nearest neighbours aggregated to objects: the score of an
        object is its best chunk's, computed server-side by `group_by`.

        Filters are applied during the walk, not over its result — the
        property the backend was chosen for (ADR-001, R1). `classes=None`
        searches the whole family, `chunk_kinds=None` matches any chunk kind;
        an absent key in `filters` means unrestricted for that key — an empty
        list under any of these is a caller mistake, not "no results", and is
        rejected loudly. The `created`/`updated` windows join the same `must`,
        so a date range narrows the walk itself rather than its result — both
        dates carry a DATETIME payload index for exactly that. `score_threshold`
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
        if chunk_kinds is not None:
            if not chunk_kinds:
                raise ValueError('search chunk_kinds got an empty list — omit the argument for "any kind", not []')
            must.append(models.FieldCondition(key="chunk_kind", match=models.MatchAny(any=chunk_kinds)))
        for key, values in (filters or {}).items():
            if not values:
                raise ValueError(
                    f'search filter {key!r} got an empty value list — omit the key for "unrestricted", not []'
                )
            must.append(models.FieldCondition(key=f"fields.{key}", match=models.MatchAny(any=values)))
        if created is not None:
            must.append(_date_condition("created_at", created))
        if updated is not None:
            must.append(_date_condition("updated_at", updated))
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
        if await self.client.collection_exists(name):
            # A collection with no row in `chunks_meta` to name it: nothing
            # can read it and nothing can write to it. It is left behind by a
            # crash between the two writes below, and it used to be the end of
            # the family — `create_collection` answers 409 and there is no
            # operation in the product that removes it. Version numbers are
            # never reused, so this can only ever be that leftover.
            logger.warning(f"vector index: dropping orphaned collection {name!r} (no active or pending row)")
            await self.client.delete_collection(name)
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
        # DATETIME, not KEYWORD: these are the fields range conditions are
        # built on ("modified within the last year"), and Qdrant parses
        # RFC-3339 and compares in UTC itself. `created_at` joined `updated_at`
        # in TASK-018 and needs no backfill — it has always been written to
        # every payload, and `ensure_version` runs this for existing
        # collections too, so a deployment provisioned earlier gets the index
        # on the next start.
        for field in ("created_at", "updated_at"):
            await self.client.create_payload_index(
                collection_name=name, field_name=field, field_schema=models.PayloadSchemaType.DATETIME
            )


def _upsert_batch(dim: int) -> int:
    """How many points of this vector width fit in one request Qdrant accepts.

    At the 1024 dimensions of a typical multilingual model this is a few
    hundred points; a model an order of magnitude wider gets a proportionally
    smaller batch instead of the same request growing past the 32 MB limit.
    """
    return max(1, _UPSERT_REQUEST_BYTES // (dim * _BYTES_PER_DIMENSION + _POINT_PAYLOAD_BYTES))


def _batches[T](items: list[T], size: int) -> Iterator[list[T]]:
    """Slice a write into requests Qdrant will accept — see `_upsert_batch`."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _point_id(obj_class: str, obj_id: int, chunk_kind: str, chunk_n: int) -> str:
    """Qdrant accepts a UUID or an unsigned int as a point id; the composite
    key becomes a deterministic UUID and stays in the payload for filtering."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"{obj_class}:{obj_id}:{chunk_kind}:{chunk_n}"))


def _meta_point_id(family: str, version: int) -> str:
    """A bare `version` int collides across families (`tickets` v1 and
    `kb_articles` v1 are different rows) — namespaced the same way chunk
    point ids already are."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"{family}:{version}"))


def _meta_of(payload: dict) -> IndexMeta:
    return IndexMeta(
        family=payload["family"],
        version=payload["version"],
        model=payload["model"],
        dim=payload["dim"],
        is_active=payload["is_active"],
    )


def _meta_payload(meta: IndexMeta) -> dict:
    return {
        "family": meta.family,
        "version": meta.version,
        "model": meta.model,
        "dim": meta.dim,
        "is_active": meta.is_active,
    }


def _by_version(meta: IndexMeta) -> int:
    return meta.version


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


def _parse_datetime(raw: object) -> datetime | None:
    """A payload date back as a `datetime`, or `None` if it cannot be read.

    Unreadable rather than fatal on purpose: this feeds the indexer's
    "inherit the stored creation date" path, and one malformed point must
    degrade to "nothing to inherit" instead of failing the object's whole
    class for the pass. A naive value — nothing this code writes, but a point
    put here by hand might be — reads as UTC for the same reason: the
    indexer compares these dates to each other, and one naive sibling among
    aware ones would raise instead of degrading.
    """
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if not isinstance(raw, datetime):
        return None
    return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)


def _date_condition(key: str, window: DateRange) -> models.FieldCondition:
    """The port's `after`/`before` in Qdrant's own terms: `gte`/`lte`, both
    inclusive. An omitted bound stays `None` — Qdrant then leaves that side
    of the range open."""
    return models.FieldCondition(
        key=key,
        range=models.DatetimeRange(
            gte=window.after.isoformat() if window.after else None,
            lte=window.before.isoformat() if window.before else None,
        ),
    )


def _object_filter(obj_class: str, obj_id: int) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="obj_class", match=models.MatchValue(value=obj_class)),
            models.FieldCondition(key="obj_id", match=models.MatchValue(value=obj_id)),
        ]
    )


def _require_version(meta: IndexMeta | None) -> IndexMeta:
    if meta is None:
        raise FingerprintMismatchError("No index version for this family — call ensure_version first")
    return meta


def _check_fingerprint(meta: IndexMeta, model: str, dim: int) -> None:
    """The vectors were embedded for the version they are being written into.

    Not the guard against a changed model any more — `ensure_version` rotates
    for that, and the sweep writes under the very meta it handed back. What is
    left is the narrow window where that version stopped being the one being
    filled while the pass was running (another replica finished the same
    rebuild and switched over).
    """
    if (meta.model, meta.dim) != (model, dim):
        raise FingerprintMismatchError(
            f"Index v{meta.version} of {meta.family!r} takes ({meta.model!r}, dim={meta.dim}); "
            f"these chunks were embedded with ({model!r}, dim={dim})"
        )
