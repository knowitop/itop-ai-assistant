"""Load-bearing assumptions about qdrant-client's local mode.

The whole plan for TASK-002 rests on three things working without a server:
an async client over `:memory:`, server-side grouping by a payload key, and
an ordered scroll for keyset pagination. If any of them is missing, the
design changes — so this stays in the suite as a canary, not as a one-off
spike.
"""

import unittest
import uuid

from qdrant_client import AsyncQdrantClient, models

_COLLECTION = "smoke"


class TestQdrantLocalMode(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncQdrantClient(location=":memory:")
        await self.client.create_collection(
            collection_name=_COLLECTION,
            vectors_config={"dense": models.VectorParams(size=2, distance=models.Distance.COSINE)},
            sparse_vectors_config={"sparse": models.SparseVectorParams(index=models.SparseIndexParams())},
        )
        await self.client.create_payload_index(
            collection_name=_COLLECTION, field_name="obj_id", field_schema=models.PayloadSchemaType.INTEGER
        )
        points = [
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector={"dense": vector},
                payload={"obj_id": obj_id, "chunk_kind": kind},
            )
            for obj_id, kind, vector in [
                (1, "body", [1.0, 0.0]),
                (1, "solution", [0.9, 0.1]),
                (2, "body", [0.0, 1.0]),
            ]
        ]
        await self.client.upsert(collection_name=_COLLECTION, points=points, wait=True)

    async def asyncTearDown(self):
        await self.client.close()

    async def test_grouping_returns_one_hit_per_object(self):
        response = await self.client.query_points_groups(
            collection_name=_COLLECTION,
            query=[1.0, 0.0],
            using="dense",
            group_by="obj_id",
            limit=10,
            group_size=1,
        )

        self.assertEqual([group.id for group in response.groups], [1, 2])
        self.assertEqual([len(group.hits) for group in response.groups], [1, 1])

    async def test_scroll_is_ordered_and_resumable(self):
        records, _ = await self.client.scroll(
            collection_name=_COLLECTION,
            scroll_filter=models.Filter(must=[models.FieldCondition(key="obj_id", range=models.Range(gt=1))]),
            order_by=models.OrderBy(key="obj_id"),
            limit=10,
            with_payload=True,
            with_vectors=False,
        )

        self.assertEqual([record.payload["obj_id"] for record in records], [2])

    async def test_sparse_slot_exists_but_stays_empty(self):
        info = await self.client.get_collection(_COLLECTION)

        self.assertIn("sparse", info.config.params.sparse_vectors)
        self.assertEqual(info.points_count, 3)
