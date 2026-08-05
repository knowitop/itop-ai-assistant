import unittest
from datetime import UTC, datetime

from qdrant_client import models

from itop_ai_assistant.vector.qdrant_store import QdrantChunkStore
from itop_ai_assistant.vector.store import ChunkDigest, ChunkMetadata, ChunkRecord, ChunkStore, FingerprintMismatchError

_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _meta(
    obj_id: int,
    kind: str = "body",
    n: int = 0,
    *,
    digest="hash",
    status="resolved",
    org_id: str | None = "1",
    visibility="public",
    filters: dict[str, str] | None = {"service_id": "5"},
) -> ChunkMetadata:
    return ChunkMetadata(
        obj_class="UserRequest",
        obj_id=obj_id,
        chunk_kind=kind,
        chunk_n=n,
        visibility=visibility,
        status=status,
        content_hash=digest,
        created_at=_NOW,
        org_id=org_id,
        filters=filters,
    )


def _chunk(
    obj_id: int,
    kind: str = "body",
    n: int = 0,
    *,
    vector=None,
    digest="hash",
    status="resolved",
    org_id: str | None = "1",
    visibility="public",
) -> ChunkRecord:
    return ChunkRecord(
        meta=_meta(obj_id, kind, n, digest=digest, status=status, org_id=org_id, visibility=visibility),
        embedding=vector or [1.0, 0.0, 0.0, 0.0],
    )


class QdrantStoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = QdrantChunkStore(":memory:")
        self.meta = await self.store.ensure_version("test-model", 4)

    async def asyncTearDown(self):
        await self.store.aclose()


class TestVersioning(QdrantStoreCase):
    async def test_implements_the_port(self):
        self.assertIsInstance(self.store, ChunkStore)

    async def test_first_use_creates_v1(self):
        self.assertEqual((self.meta.version, self.meta.model, self.meta.dim), (1, "test-model", 4))

    async def test_active_meta_survives_a_new_client(self):
        self.assertEqual(await self.store.active_meta(), self.meta)

    async def test_same_fingerprint_reuses_the_version(self):
        self.assertEqual(await self.store.ensure_version("test-model", 4), self.meta)

    async def test_a_different_model_refuses_to_write(self):
        with self.assertRaises(FingerprintMismatchError):
            await self.store.ensure_version("other-model", 4)

    async def test_a_different_dimension_refuses_to_write(self):
        with self.assertRaises(FingerprintMismatchError):
            await self.store.ensure_version("test-model", 8)


class TestUpsert(QdrantStoreCase):
    async def test_chunks_are_written_and_counted(self):
        written = await self.store.upsert_chunks([_chunk(1), _chunk(1, "solution")], model="test-model", dim=4)

        self.assertEqual(written, 2)
        stats = await self.store.stats()
        self.assertEqual(stats.rows, 2)

    async def test_rewriting_the_same_chunk_updates_it(self):
        await self.store.upsert_chunks([_chunk(1, digest="old")], model="test-model", dim=4)
        await self.store.upsert_chunks([_chunk(1, digest="new")], model="test-model", dim=4)

        self.assertEqual((await self.store.stats()).rows, 1)
        digests = await self.store.get_chunk_digests("UserRequest", 1)
        self.assertEqual(digests[("body", 0)].content_hash, "new")

    async def test_hashes_are_keyed_by_kind_and_ordinal(self):
        await self.store.upsert_chunks(
            [
                _chunk(1, "body", 0, digest="a"),
                _chunk(1, "body", 1, digest="b"),
                _chunk(1, "solution", 0, digest="c"),
            ],
            model="test-model",
            dim=4,
        )

        digests = await self.store.get_chunk_digests("UserRequest", 1)
        self.assertEqual(
            {key: digest.content_hash for key, digest in digests.items()},
            {("body", 0): "a", ("body", 1): "b", ("solution", 0): "c"},
        )

    async def test_upserted_chunks_carry_a_meta_hash(self):
        meta = _meta(1)
        await self.store.upsert_chunks(
            [ChunkRecord(meta=meta, embedding=[1.0, 0.0, 0.0, 0.0])], model="test-model", dim=4
        )

        digests = await self.store.get_chunk_digests("UserRequest", 1)

        self.assertEqual(digests[("body", 0)].meta_hash, meta.meta_hash)

    async def test_hashes_of_an_unknown_object_are_empty(self):
        self.assertEqual(await self.store.get_chunk_digests("UserRequest", 999), {})

    async def test_writing_under_a_stale_fingerprint_is_refused(self):
        with self.assertRaises(FingerprintMismatchError):
            await self.store.upsert_chunks([_chunk(1)], model="other-model", dim=4)

    async def test_no_chunk_payload_carries_text(self):
        # The index is a derived cache; anything shown to a human is re-read from iTop
        await self.store.upsert_chunks([_chunk(1)], model="test-model", dim=4)

        records, _ = await self.store.client.scroll(
            collection_name=self.store.collection_name(1), limit=10, with_payload=True
        )
        for record in records:
            self.assertNotIn("text", record.payload)
            self.assertNotIn("content", record.payload)


class TestDeletion(QdrantStoreCase):
    async def test_deleting_an_object_removes_all_of_its_chunks(self):
        await self.store.upsert_chunks(
            [_chunk(1, "body"), _chunk(1, "solution"), _chunk(2, "body")], model="test-model", dim=4
        )

        removed = await self.store.delete_object("UserRequest", 1)

        self.assertEqual(removed, 2)
        self.assertEqual((await self.store.stats()).rows, 1)
        self.assertEqual(await self.store.get_chunk_digests("UserRequest", 1), {})

    async def test_deleting_an_absent_object_removes_nothing(self):
        self.assertEqual(await self.store.delete_object("UserRequest", 999), 0)

    async def test_named_chunks_are_deleted_and_the_rest_stay(self):
        await self.store.upsert_chunks(
            [_chunk(1, "body", 0), _chunk(1, "body", 1), _chunk(1, "solution", 0)],
            model="test-model",
            dim=4,
        )

        removed = await self.store.delete_chunks("UserRequest", 1, [("body", 1)])

        self.assertEqual(removed, 1)
        self.assertEqual(set(await self.store.get_chunk_digests("UserRequest", 1)), {("body", 0), ("solution", 0)})

    async def test_deleting_an_empty_list_touches_nothing(self):
        await self.store.upsert_chunks([_chunk(1)], model="test-model", dim=4)

        self.assertEqual(await self.store.delete_chunks("UserRequest", 1, []), 0)
        self.assertEqual((await self.store.stats()).rows, 1)


class TestReconciliationWalk(QdrantStoreCase):
    async def test_object_ids_come_back_ascending_and_deduplicated(self):
        await self.store.upsert_chunks(
            [_chunk(3, "body"), _chunk(1, "body"), _chunk(1, "solution"), _chunk(2, "body")],
            model="test-model",
            dim=4,
        )

        self.assertEqual(await self.store.list_object_ids("UserRequest"), [1, 2, 3])

    async def test_the_walk_resumes_after_the_last_id_seen(self):
        await self.store.upsert_chunks([_chunk(1), _chunk(2), _chunk(3)], model="test-model", dim=4)

        self.assertEqual(await self.store.list_object_ids("UserRequest", after=1), [2, 3])

    async def test_the_walk_respects_its_limit(self):
        await self.store.upsert_chunks([_chunk(1), _chunk(2), _chunk(3)], model="test-model", dim=4)

        self.assertEqual(await self.store.list_object_ids("UserRequest", limit=2), [1, 2])

    async def test_another_class_is_not_walked(self):
        await self.store.upsert_chunks([_chunk(1)], model="test-model", dim=4)

        self.assertEqual(await self.store.list_object_ids("Incident"), [])


_ALL = {"classes": ["UserRequest"], "statuses": ["resolved", "closed"], "visibilities": ["public", "internal"]}


class TestSearch(QdrantStoreCase):
    async def test_empty_index_returns_nothing(self):
        self.assertEqual(await self.store.search([1.0, 0.0, 0.0, 0.0], **_ALL), [])

    async def test_hits_come_back_by_descending_score(self):
        await self.store.upsert_chunks(
            [_chunk(1, vector=[1.0, 0.0, 0.0, 0.0]), _chunk(2, vector=[0.0, 1.0, 0.0, 0.0])],
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
            model="test-model",
            dim=4,
        )

        hits = await self.store.search([1.0, 0.0, 0.0, 0.0], **_ALL)

        self.assertEqual([hit.obj_id for hit in hits], [1])

    async def test_status_filter_excludes_the_rest(self):
        await self.store.upsert_chunks(
            [_chunk(1, status="resolved"), _chunk(2, status="closed")], model="test-model", dim=4
        )

        hits = await self.store.search(
            [1.0, 0.0, 0.0, 0.0],
            classes=["UserRequest"],
            statuses=["closed"],
            visibilities=["public", "internal"],
        )

        self.assertEqual([hit.obj_id for hit in hits], [2])

    async def test_visibility_filter_excludes_internal(self):
        await self.store.upsert_chunks(
            [_chunk(1, visibility="public"), _chunk(2, visibility="internal")], model="test-model", dim=4
        )

        hits = await self.store.search(
            [1.0, 0.0, 0.0, 0.0],
            classes=["UserRequest"],
            statuses=["resolved", "closed"],
            visibilities=["public"],
        )

        self.assertEqual([hit.obj_id for hit in hits], [1])

    async def test_allowed_orgs_none_means_unrestricted(self):
        await self.store.upsert_chunks([_chunk(1, org_id="1"), _chunk(2, org_id="2")], model="test-model", dim=4)

        hits = await self.store.search([1.0, 0.0, 0.0, 0.0], allowed_orgs=None, **_ALL)

        self.assertEqual({hit.obj_id for hit in hits}, {1, 2})

    async def test_allowed_orgs_narrows_the_candidates(self):
        await self.store.upsert_chunks([_chunk(1, org_id="1"), _chunk(2, org_id="2")], model="test-model", dim=4)

        hits = await self.store.search([1.0, 0.0, 0.0, 0.0], allowed_orgs=["2"], **_ALL)

        self.assertEqual([hit.obj_id for hit in hits], [2])

    async def test_the_asking_ticket_can_be_excluded(self):
        await self.store.upsert_chunks([_chunk(1), _chunk(2)], model="test-model", dim=4)

        hits = await self.store.search([1.0, 0.0, 0.0, 0.0], exclude_obj_id=1, **_ALL)

        self.assertEqual([hit.obj_id for hit in hits], [2])

    async def test_limit_caps_the_number_of_objects(self):
        await self.store.upsert_chunks([_chunk(1), _chunk(2), _chunk(3)], model="test-model", dim=4)

        self.assertEqual(len(await self.store.search([1.0, 0.0, 0.0, 0.0], limit=2, **_ALL)), 2)


class TestMetadataUpdate(QdrantStoreCase):
    async def test_changes_status_without_touching_the_vector(self):
        await self.store.upsert_chunks([_chunk(1, status="new")], model="test-model", dim=4)
        before, _ = await self.store.client.scroll(
            collection_name=self.store.collection_name(1), limit=10, with_payload=False, with_vectors=True
        )

        new_meta = _meta(1, status="resolved")
        updated = await self.store.update_chunk_metadata([new_meta])

        self.assertEqual(updated, 1)
        digests = await self.store.get_chunk_digests("UserRequest", 1)
        self.assertEqual(digests[("body", 0)].meta_hash, new_meta.meta_hash)
        after, _ = await self.store.client.scroll(
            collection_name=self.store.collection_name(1), limit=10, with_payload=False, with_vectors=True
        )
        self.assertEqual(before[0].vector, after[0].vector)

    async def test_a_dropped_filter_key_is_removed_not_merged(self):
        await self.store.upsert_chunks([_chunk(1)], model="test-model", dim=4)

        await self.store.update_chunk_metadata([_meta(1, filters=None)])

        records, _ = await self.store.client.scroll(
            collection_name=self.store.collection_name(1), limit=10, with_payload=True
        )
        self.assertNotIn("service_id", records[0].payload)

    async def test_digest_of_a_point_written_outside_upsert_has_no_meta_hash(self):
        # Simulates a chunk indexed before this field existed.
        await self.store.client.upsert(
            collection_name=self.store.collection_name(1),
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

        digests = await self.store.get_chunk_digests("UserRequest", 99)

        self.assertEqual(digests[("body", 0)], ChunkDigest(content_hash="legacy", meta_hash=None))

    async def test_without_an_active_version_returns_zero(self):
        store = QdrantChunkStore(":memory:")
        try:
            self.assertEqual(await store.update_chunk_metadata([_meta(1)]), 0)
        finally:
            await store.aclose()


if __name__ == "__main__":
    unittest.main()
