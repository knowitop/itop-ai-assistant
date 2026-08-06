import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from qdrant_client import models

from itop_ai_assistant.vector.qdrant_store import QdrantChunkStore
from itop_ai_assistant.vector.store import ChunkDigest, ChunkMetadata, ChunkRecord, ChunkStore, FingerprintMismatchError

_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
_FAMILY = "tickets"


def _meta(
    obj_id: int,
    kind: str = "body",
    n: int = 0,
    *,
    obj_class="UserRequest",
    digest="hash",
    status="resolved",
    org_id: str | None = "1",
    visibility="public",
    extra_filters: dict[str, str] | None = {"service_id": "5"},
    updated_at: datetime | None = _NOW,
) -> ChunkMetadata:
    return ChunkMetadata(
        obj_class=obj_class,
        obj_id=obj_id,
        chunk_kind=kind,
        chunk_n=n,
        visibility=visibility,
        content_hash=digest,
        created_at=_NOW,
        filters={**(extra_filters or {}), "status": status, **({"org_id": org_id} if org_id else {})},
        updated_at=updated_at,
    )


def _chunk(
    obj_id: int,
    kind: str = "body",
    n: int = 0,
    *,
    obj_class="UserRequest",
    vector=None,
    digest="hash",
    status="resolved",
    org_id: str | None = "1",
    visibility="public",
    updated_at: datetime | None = _NOW,
) -> ChunkRecord:
    return ChunkRecord(
        meta=_meta(
            obj_id,
            kind,
            n,
            obj_class=obj_class,
            digest=digest,
            status=status,
            org_id=org_id,
            visibility=visibility,
            updated_at=updated_at,
        ),
        embedding=vector or [1.0, 0.0, 0.0, 0.0],
    )


class QdrantStoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = QdrantChunkStore(":memory:")
        self.meta = await self.store.ensure_version(_FAMILY, "test-model", 4)

    async def asyncTearDown(self):
        await self.store.aclose()


class TestVersioning(QdrantStoreCase):
    async def test_implements_the_port(self):
        self.assertIsInstance(self.store, ChunkStore)

    async def test_first_use_creates_v1(self):
        self.assertEqual(
            (self.meta.family, self.meta.version, self.meta.model, self.meta.dim), (_FAMILY, 1, "test-model", 4)
        )

    async def test_active_meta_survives_a_new_client(self):
        self.assertEqual(await self.store.active_meta(_FAMILY), self.meta)

    async def test_same_fingerprint_reuses_the_version(self):
        self.assertEqual(await self.store.ensure_version(_FAMILY, "test-model", 4), self.meta)

    async def test_a_different_model_refuses_to_write(self):
        with self.assertRaises(FingerprintMismatchError):
            await self.store.ensure_version(_FAMILY, "other-model", 4)

    async def test_a_different_dimension_refuses_to_write(self):
        with self.assertRaises(FingerprintMismatchError):
            await self.store.ensure_version(_FAMILY, "test-model", 8)

    async def test_two_families_coexist_as_different_collections(self):
        other = await self.store.ensure_version("kb_articles", "test-model", 4)

        self.assertEqual((other.family, other.version), ("kb_articles", 1))
        self.assertEqual(await self.store.active_meta(_FAMILY), self.meta)
        self.assertEqual(await self.store.active_meta("kb_articles"), other)

        await self.store.upsert_chunks([_chunk(1)], family=_FAMILY, model="test-model", dim=4)
        await self.store.upsert_chunks(
            [_chunk(1, obj_class="KnowledgeBaseArticle")], family="kb_articles", model="test-model", dim=4
        )

        self.assertEqual((await self.store.stats(_FAMILY)).rows, 1)
        self.assertEqual((await self.store.stats("kb_articles")).rows, 1)
        self.assertEqual(await self.store.list_object_ids("kb_articles", "UserRequest"), [])

    async def test_list_families_reads_active_rows_from_storage(self):
        await self.store.ensure_version("kb_articles", "test-model", 4)

        self.assertEqual(set(await self.store.list_families()), {_FAMILY, "kb_articles"})

    async def test_empty_store_has_no_families(self):
        store = QdrantChunkStore(":memory:")
        try:
            self.assertEqual(await store.list_families(), [])
        finally:
            await store.aclose()

    async def test_indexed_filter_keys_are_created(self):
        # Local (`:memory:`) Qdrant does not persist payload-index metadata —
        # `get_collection().payload_schema` is always {} there, so the only
        # observable signal is the call itself; the spy still goes through
        # the real client, it only records what it was asked to index.
        with patch.object(
            self.store.client, "create_payload_index", wraps=self.store.client.create_payload_index
        ) as spy:
            await self.store.ensure_version(_FAMILY, "test-model", 4, filter_keys=("status", "org_id"))

        field_names = {call.kwargs["field_name"] for call in spy.await_args_list}
        self.assertIn("fields.status", field_names)
        self.assertIn("fields.org_id", field_names)


class TestUpsert(QdrantStoreCase):
    async def test_chunks_are_written_and_counted(self):
        written = await self.store.upsert_chunks(
            [_chunk(1), _chunk(1, "solution")], family=_FAMILY, model="test-model", dim=4
        )

        self.assertEqual(written, 2)
        stats = await self.store.stats(_FAMILY)
        self.assertEqual(stats.rows, 2)

    async def test_rewriting_the_same_chunk_updates_it(self):
        await self.store.upsert_chunks([_chunk(1, digest="old")], family=_FAMILY, model="test-model", dim=4)
        await self.store.upsert_chunks([_chunk(1, digest="new")], family=_FAMILY, model="test-model", dim=4)

        self.assertEqual((await self.store.stats(_FAMILY)).rows, 1)
        digests = await self.store.get_chunk_digests(_FAMILY, "UserRequest", 1)
        self.assertEqual(digests[("body", 0)].content_hash, "new")

    async def test_hashes_are_keyed_by_kind_and_ordinal(self):
        await self.store.upsert_chunks(
            [
                _chunk(1, "body", 0, digest="a"),
                _chunk(1, "body", 1, digest="b"),
                _chunk(1, "solution", 0, digest="c"),
            ],
            family=_FAMILY,
            model="test-model",
            dim=4,
        )

        digests = await self.store.get_chunk_digests(_FAMILY, "UserRequest", 1)
        self.assertEqual(
            {key: digest.content_hash for key, digest in digests.items()},
            {("body", 0): "a", ("body", 1): "b", ("solution", 0): "c"},
        )

    async def test_upserted_chunks_carry_a_meta_hash(self):
        meta = _meta(1)
        await self.store.upsert_chunks(
            [ChunkRecord(meta=meta, embedding=[1.0, 0.0, 0.0, 0.0])], family=_FAMILY, model="test-model", dim=4
        )

        digests = await self.store.get_chunk_digests(_FAMILY, "UserRequest", 1)

        self.assertEqual(digests[("body", 0)].meta_hash, meta.meta_hash)

    async def test_hashes_of_an_unknown_object_are_empty(self):
        self.assertEqual(await self.store.get_chunk_digests(_FAMILY, "UserRequest", 999), {})

    async def test_writing_under_a_stale_fingerprint_is_refused(self):
        with self.assertRaises(FingerprintMismatchError):
            await self.store.upsert_chunks([_chunk(1)], family=_FAMILY, model="other-model", dim=4)

    async def test_no_chunk_payload_carries_text(self):
        # The index is a derived cache; anything shown to a human is re-read from iTop
        await self.store.upsert_chunks([_chunk(1)], family=_FAMILY, model="test-model", dim=4)

        records, _ = await self.store.client.scroll(
            collection_name=self.store.collection_name(_FAMILY, 1), limit=10, with_payload=True
        )
        for record in records:
            self.assertNotIn("text", record.payload)
            self.assertNotIn("content", record.payload)

    async def test_filters_land_nested_under_fields(self):
        # D6/TASK-008: a source-defined key must not shadow a system key of
        # the same name — `fields.*` is the fix, verified here directly.
        # `status`/`org_id` are now just source-declared filter keys like any
        # other, so they land under `fields` too — there is no system-level
        # "status"/"org_id" payload key anymore.
        await self.store.upsert_chunks([_chunk(1)], family=_FAMILY, model="test-model", dim=4)

        records, _ = await self.store.client.scroll(
            collection_name=self.store.collection_name(_FAMILY, 1), limit=10, with_payload=True
        )
        self.assertEqual(records[0].payload["fields"], {"service_id": "5", "status": "resolved", "org_id": "1"})
        self.assertNotIn("service_id", records[0].payload)
        self.assertNotIn("status", records[0].payload)
        self.assertNotIn("org_id", records[0].payload)


class TestDeletion(QdrantStoreCase):
    async def test_deleting_an_object_removes_all_of_its_chunks(self):
        await self.store.upsert_chunks(
            [_chunk(1, "body"), _chunk(1, "solution"), _chunk(2, "body")], family=_FAMILY, model="test-model", dim=4
        )

        removed = await self.store.delete_object(_FAMILY, "UserRequest", 1)

        self.assertEqual(removed, 2)
        self.assertEqual((await self.store.stats(_FAMILY)).rows, 1)
        self.assertEqual(await self.store.get_chunk_digests(_FAMILY, "UserRequest", 1), {})

    async def test_deleting_an_absent_object_removes_nothing(self):
        self.assertEqual(await self.store.delete_object(_FAMILY, "UserRequest", 999), 0)

    async def test_named_chunks_are_deleted_and_the_rest_stay(self):
        await self.store.upsert_chunks(
            [_chunk(1, "body", 0), _chunk(1, "body", 1), _chunk(1, "solution", 0)],
            family=_FAMILY,
            model="test-model",
            dim=4,
        )

        removed = await self.store.delete_chunks(_FAMILY, "UserRequest", 1, [("body", 1)])

        self.assertEqual(removed, 1)
        self.assertEqual(
            set(await self.store.get_chunk_digests(_FAMILY, "UserRequest", 1)), {("body", 0), ("solution", 0)}
        )

    async def test_deleting_an_empty_list_touches_nothing(self):
        await self.store.upsert_chunks([_chunk(1)], family=_FAMILY, model="test-model", dim=4)

        self.assertEqual(await self.store.delete_chunks(_FAMILY, "UserRequest", 1, []), 0)
        self.assertEqual((await self.store.stats(_FAMILY)).rows, 1)


class TestReconciliationWalk(QdrantStoreCase):
    async def test_object_ids_come_back_ascending_and_deduplicated(self):
        await self.store.upsert_chunks(
            [_chunk(3, "body"), _chunk(1, "body"), _chunk(1, "solution"), _chunk(2, "body")],
            family=_FAMILY,
            model="test-model",
            dim=4,
        )

        self.assertEqual(await self.store.list_object_ids(_FAMILY, "UserRequest"), [1, 2, 3])

    async def test_the_walk_resumes_after_the_last_id_seen(self):
        await self.store.upsert_chunks([_chunk(1), _chunk(2), _chunk(3)], family=_FAMILY, model="test-model", dim=4)

        self.assertEqual(await self.store.list_object_ids(_FAMILY, "UserRequest", after=1), [2, 3])

    async def test_the_walk_respects_its_limit(self):
        await self.store.upsert_chunks([_chunk(1), _chunk(2), _chunk(3)], family=_FAMILY, model="test-model", dim=4)

        self.assertEqual(await self.store.list_object_ids(_FAMILY, "UserRequest", limit=2), [1, 2])

    async def test_another_class_is_not_walked(self):
        await self.store.upsert_chunks([_chunk(1)], family=_FAMILY, model="test-model", dim=4)

        self.assertEqual(await self.store.list_object_ids(_FAMILY, "Incident"), [])


_ALL = {
    "family": _FAMILY,
    "classes": ["UserRequest"],
    "filters": {"status": ["resolved", "closed"]},
    "visibilities": ["public", "internal"],
}


class TestSearch(QdrantStoreCase):
    async def test_empty_index_returns_nothing(self):
        self.assertEqual(await self.store.search([1.0, 0.0, 0.0, 0.0], **_ALL), [])

    async def test_hits_come_back_by_descending_score(self):
        await self.store.upsert_chunks(
            [_chunk(1, vector=[1.0, 0.0, 0.0, 0.0]), _chunk(2, vector=[0.0, 1.0, 0.0, 0.0])],
            family=_FAMILY,
            model="test-model",
            dim=4,
        )

        hits = await self.store.search([1.0, 0.0, 0.0, 0.0], **_ALL)

        self.assertEqual([hit.obj_id for hit in hits], [1, 2])
        self.assertGreater(hits[0].score, hits[1].score)

    async def test_an_object_matching_twice_appears_once(self):
        # A ticket similar in both its description and its solution is one result,
        # scored by its best chunk — not two results
        await self.store.upsert_chunks(
            [
                _chunk(1, "body", vector=[1.0, 0.0, 0.0, 0.0]),
                _chunk(1, "solution", vector=[0.99, 0.01, 0.0, 0.0]),
            ],
            family=_FAMILY,
            model="test-model",
            dim=4,
        )

        hits = await self.store.search([1.0, 0.0, 0.0, 0.0], **_ALL)

        self.assertEqual([hit.obj_id for hit in hits], [1])

    async def test_a_search_never_crosses_into_another_family(self):
        await self.store.ensure_version("kb_articles", "test-model", 4)
        await self.store.upsert_chunks(
            [_chunk(1, vector=[1.0, 0.0, 0.0, 0.0])], family=_FAMILY, model="test-model", dim=4
        )
        await self.store.upsert_chunks(
            [_chunk(1, obj_class="KnowledgeBaseArticle", vector=[1.0, 0.0, 0.0, 0.0])],
            family="kb_articles",
            model="test-model",
            dim=4,
        )

        hits = await self.store.search(
            [1.0, 0.0, 0.0, 0.0],
            family=_FAMILY,
            classes=["UserRequest", "KnowledgeBaseArticle"],
            filters={"status": ["resolved", "closed"]},
            visibilities=["public", "internal"],
        )

        self.assertEqual([(hit.obj_class, hit.obj_id) for hit in hits], [("UserRequest", 1)])

    async def test_status_filter_excludes_the_rest(self):
        await self.store.upsert_chunks(
            [_chunk(1, status="resolved"), _chunk(2, status="closed")], family=_FAMILY, model="test-model", dim=4
        )

        hits = await self.store.search(
            [1.0, 0.0, 0.0, 0.0],
            family=_FAMILY,
            classes=["UserRequest"],
            filters={"status": ["closed"]},
            visibilities=["public", "internal"],
        )

        self.assertEqual([hit.obj_id for hit in hits], [2])

    async def test_visibility_filter_excludes_internal(self):
        await self.store.upsert_chunks(
            [_chunk(1, visibility="public"), _chunk(2, visibility="internal")],
            family=_FAMILY,
            model="test-model",
            dim=4,
        )

        hits = await self.store.search(
            [1.0, 0.0, 0.0, 0.0],
            family=_FAMILY,
            classes=["UserRequest"],
            filters={"status": ["resolved", "closed"]},
            visibilities=["public"],
        )

        self.assertEqual([hit.obj_id for hit in hits], [1])

    async def test_allowed_orgs_none_means_unrestricted(self):
        await self.store.upsert_chunks(
            [_chunk(1, org_id="1"), _chunk(2, org_id="2")], family=_FAMILY, model="test-model", dim=4
        )

        # No "org_id" key in filters — unrestricted, unlike a present-but-empty one
        hits = await self.store.search([1.0, 0.0, 0.0, 0.0], **_ALL)

        self.assertEqual({hit.obj_id for hit in hits}, {1, 2})

    async def test_allowed_orgs_narrows_the_candidates(self):
        await self.store.upsert_chunks(
            [_chunk(1, org_id="1"), _chunk(2, org_id="2")], family=_FAMILY, model="test-model", dim=4
        )

        hits = await self.store.search(
            [1.0, 0.0, 0.0, 0.0],
            family=_FAMILY,
            classes=["UserRequest"],
            filters={"status": ["resolved", "closed"], "org_id": ["2"]},
            visibilities=["public", "internal"],
        )

        self.assertEqual([hit.obj_id for hit in hits], [2])

    async def test_the_asking_ticket_can_be_excluded(self):
        await self.store.upsert_chunks([_chunk(1), _chunk(2)], family=_FAMILY, model="test-model", dim=4)

        hits = await self.store.search([1.0, 0.0, 0.0, 0.0], exclude=("UserRequest", 1), **_ALL)

        self.assertEqual([hit.obj_id for hit in hits], [2])

    async def test_exclusion_is_by_class_and_id_together(self):
        # `Ticket` subclasses share one numbering, but root hierarchies do not:
        # excluding UserRequest 1 must not silence some other class's id 1
        await self.store.upsert_chunks(
            [_chunk(1), _chunk(1, obj_class="KnowledgeBaseArticle")], family=_FAMILY, model="test-model", dim=4
        )

        hits = await self.store.search(
            [1.0, 0.0, 0.0, 0.0],
            family=_FAMILY,
            classes=["UserRequest", "KnowledgeBaseArticle"],
            filters={"status": ["resolved", "closed"]},
            visibilities=["public", "internal"],
            exclude=("UserRequest", 1),
        )

        self.assertEqual([(hit.obj_class, hit.obj_id) for hit in hits], [("KnowledgeBaseArticle", 1)])

    async def test_the_same_id_in_two_hierarchies_stays_two_objects(self):
        await self.store.upsert_chunks(
            [_chunk(1), _chunk(1, obj_class="KnowledgeBaseArticle")], family=_FAMILY, model="test-model", dim=4
        )

        hits = await self.store.search(
            [1.0, 0.0, 0.0, 0.0],
            family=_FAMILY,
            classes=["UserRequest", "KnowledgeBaseArticle"],
            filters={"status": ["resolved", "closed"]},
            visibilities=["public", "internal"],
        )

        self.assertEqual(
            {(hit.obj_class, hit.obj_id) for hit in hits}, {("UserRequest", 1), ("KnowledgeBaseArticle", 1)}
        )

    async def test_updated_after_keeps_only_the_recent(self):
        await self.store.upsert_chunks(
            [
                _chunk(1, updated_at=datetime(2026, 7, 1, tzinfo=UTC)),
                _chunk(2, updated_at=datetime(2024, 1, 1, tzinfo=UTC)),
            ],
            family=_FAMILY,
            model="test-model",
            dim=4,
        )

        hits = await self.store.search([1.0, 0.0, 0.0, 0.0], updated_after=datetime(2026, 1, 1, tzinfo=UTC), **_ALL)

        self.assertEqual([hit.obj_id for hit in hits], [1])

    async def test_an_object_without_a_date_never_passes_the_window(self):
        # "Unknown" must not read as "recent" — the payload key is absent, and
        # an absent key matches no range condition
        await self.store.upsert_chunks([_chunk(1, updated_at=None)], family=_FAMILY, model="test-model", dim=4)

        found = await self.store.search([1.0, 0.0, 0.0, 0.0], **_ALL)
        self.assertEqual([hit.obj_id for hit in found], [1])
        self.assertEqual(
            await self.store.search([1.0, 0.0, 0.0, 0.0], updated_after=datetime(2000, 1, 1, tzinfo=UTC), **_ALL), []
        )

    async def test_limit_caps_the_number_of_objects(self):
        await self.store.upsert_chunks([_chunk(1), _chunk(2), _chunk(3)], family=_FAMILY, model="test-model", dim=4)

        self.assertEqual(len(await self.store.search([1.0, 0.0, 0.0, 0.0], limit=2, **_ALL)), 2)

    async def test_empty_filter_value_list_is_rejected(self):
        # A present-but-empty value list is almost certainly a caller mistake
        # (a config field that resolved to []), not "no results" — see the
        # module's design note on why this fails loudly instead of silently
        # zeroing the query.
        with self.assertRaises(ValueError):
            await self.store.search(
                [1.0, 0.0, 0.0, 0.0], family=_FAMILY, filters={"status": []}, visibilities=["public"]
            )

    async def test_empty_classes_list_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.store.search([1.0, 0.0, 0.0, 0.0], family=_FAMILY, classes=[], visibilities=["public"])

    async def test_classes_none_searches_whole_family(self):
        await self.store.upsert_chunks(
            [
                _chunk(1, obj_class="UserRequest", vector=[1.0, 0.0, 0.0, 0.0]),
                _chunk(1, obj_class="Incident", vector=[0.0, 1.0, 0.0, 0.0]),
            ],
            family=_FAMILY,
            model="test-model",
            dim=4,
        )

        hits = await self.store.search(
            [1.0, 0.0, 0.0, 0.0], family=_FAMILY, classes=None, visibilities=["public", "internal"]
        )

        self.assertEqual({(hit.obj_class, hit.obj_id) for hit in hits}, {("UserRequest", 1), ("Incident", 1)})


class TestMetadataUpdate(QdrantStoreCase):
    async def test_changes_status_without_touching_the_vector(self):
        await self.store.upsert_chunks([_chunk(1, status="new")], family=_FAMILY, model="test-model", dim=4)
        before, _ = await self.store.client.scroll(
            collection_name=self.store.collection_name(_FAMILY, 1), limit=10, with_payload=False, with_vectors=True
        )

        new_meta = _meta(1, status="resolved")
        updated = await self.store.update_chunk_metadata([new_meta], family=_FAMILY)

        self.assertEqual(updated, 1)
        digests = await self.store.get_chunk_digests(_FAMILY, "UserRequest", 1)
        self.assertEqual(digests[("body", 0)].meta_hash, new_meta.meta_hash)
        after, _ = await self.store.client.scroll(
            collection_name=self.store.collection_name(_FAMILY, 1), limit=10, with_payload=False, with_vectors=True
        )
        self.assertEqual(before[0].vector, after[0].vector)

    async def test_a_dropped_filter_key_is_removed_not_merged(self):
        # `_meta`'s `status`/`org_id` kwargs always land in `filters` now, so
        # a bare `ChunkMetadata` (not the helper) is what exercises "no
        # filters at all" — the helper can't express that state.
        await self.store.upsert_chunks([_chunk(1)], family=_FAMILY, model="test-model", dim=4)
        bare = ChunkMetadata(
            obj_class="UserRequest",
            obj_id=1,
            chunk_kind="body",
            chunk_n=0,
            visibility="public",
            content_hash="hash",
            created_at=_NOW,
            filters=None,
        )

        await self.store.update_chunk_metadata([bare], family=_FAMILY)

        records, _ = await self.store.client.scroll(
            collection_name=self.store.collection_name(_FAMILY, 1), limit=10, with_payload=True
        )
        self.assertEqual(records[0].payload["fields"], {})

    async def test_digest_of_a_point_written_outside_upsert_has_no_meta_hash(self):
        # Simulates a chunk indexed before this field existed.
        await self.store.client.upsert(
            collection_name=self.store.collection_name(_FAMILY, 1),
            points=[
                models.PointStruct(
                    id=1,
                    vector={"dense": [1.0, 0.0, 0.0, 0.0]},
                    payload={
                        "obj_class": "UserRequest",
                        "obj_id": 99,
                        "chunk_kind": "body",
                        "chunk_n": 0,
                        "visibility": "public",
                        "status": "resolved",
                        "content_hash": "legacy",
                    },
                )
            ],
            wait=True,
        )

        digests = await self.store.get_chunk_digests(_FAMILY, "UserRequest", 99)

        self.assertEqual(digests[("body", 0)], ChunkDigest(content_hash="legacy", meta_hash=None))

    async def test_without_an_active_version_returns_zero(self):
        store = QdrantChunkStore(":memory:")
        try:
            self.assertEqual(await store.update_chunk_metadata([_meta(1)], family=_FAMILY), 0)
        finally:
            await store.aclose()


if __name__ == "__main__":
    unittest.main()
