import unittest
from datetime import UTC, datetime

from itop_ai_assistant.vector.qdrant_store import QdrantChunkStore
from itop_ai_assistant.vector.store import ChunkRecord, ChunkStore, FingerprintMismatchError

_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


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
        obj_class="UserRequest",
        obj_id=obj_id,
        chunk_kind=kind,
        chunk_n=n,
        visibility=visibility,
        status=status,
        content_hash=digest,
        embedding=vector or [1.0, 0.0, 0.0, 0.0],
        created_at=_NOW,
        org_id=org_id,
        filters={"service_id": "5"},
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
        self.assertEqual(await self.store.get_chunk_hashes("UserRequest", 1), {("body", 0): "new"})

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

        self.assertEqual(
            await self.store.get_chunk_hashes("UserRequest", 1),
            {("body", 0): "a", ("body", 1): "b", ("solution", 0): "c"},
        )

    async def test_hashes_of_an_unknown_object_are_empty(self):
        self.assertEqual(await self.store.get_chunk_hashes("UserRequest", 999), {})

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
        self.assertEqual(await self.store.get_chunk_hashes("UserRequest", 1), {})

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
        self.assertEqual(set(await self.store.get_chunk_hashes("UserRequest", 1)), {("body", 0), ("solution", 0)})

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


if __name__ == "__main__":
    unittest.main()
