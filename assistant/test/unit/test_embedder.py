import json
import unittest

import httpx

from config import EmbeddingsConfig
from vector.embedder import EmbeddingsClient, EmbeddingsError

_DIM = 4


def _response(vectors: list[list[float]], shuffle: bool = False) -> dict:
    """OpenAI-style /v1/embeddings payload; optionally out of order."""
    data = [{"index": i, "embedding": vec} for i, vec in enumerate(vectors)]
    if shuffle:
        data = list(reversed(data))
    return {"object": "list", "data": data}


class EmbedderTestCase(unittest.IsolatedAsyncioTestCase):
    def _client(self, handler, **cfg_overrides) -> EmbeddingsClient:
        cfg = EmbeddingsConfig(base_url="http://emb/v1", model="bge-m3", dimension=_DIM, batch_size=3, **cfg_overrides)
        transport = httpx.MockTransport(handler)
        return EmbeddingsClient(cfg, client=httpx.AsyncClient(transport=transport))


class TestBatching(EmbedderTestCase):
    async def test_batches_by_batch_size_and_preserves_order(self):
        requests: list[list[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            texts = json.loads(request.content)["input"]
            requests.append(texts)
            # Distinct vector per text: [n, 0, 0, 0] where n encodes the text
            vectors = [[float(t.removeprefix("text-")), 0.0, 0.0, 0.0] for t in texts]
            return httpx.Response(200, json=_response(vectors))

        client = self._client(handler)
        vectors = await client.embed([f"text-{n}" for n in range(7)])

        self.assertEqual([len(batch) for batch in requests], [3, 3, 1])  # 7 texts / batch_size 3
        self.assertEqual([vec[0] for vec in vectors], [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    async def test_response_order_restored_by_index(self):
        def handler(request: httpx.Request) -> httpx.Response:
            texts = json.loads(request.content)["input"]
            vectors = [[float(i), 0.0, 0.0, 0.0] for i in range(len(texts))]
            return httpx.Response(200, json=_response(vectors, shuffle=True))

        client = self._client(handler)
        vectors = await client.embed(["a", "b"])
        self.assertEqual([vec[0] for vec in vectors], [0.0, 1.0])


class TestRequestShape(EmbedderTestCase):
    async def test_payload_url_and_auth_header(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["payload"] = json.loads(request.content)
            return httpx.Response(200, json=_response([[0.0] * _DIM]))

        client = self._client(handler, api_key="sk-emb")
        await client.embed(["hello"])

        self.assertEqual(seen["url"], "http://emb/v1/embeddings")
        self.assertEqual(seen["auth"], "Bearer sk-emb")
        self.assertEqual(seen["payload"], {"model": "bge-m3", "input": ["hello"]})

    async def test_no_auth_header_without_api_key(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=_response([[0.0] * _DIM]))

        client = self._client(handler)
        await client.embed(["hello"])
        self.assertIsNone(seen["auth"])


class TestErrors(EmbedderTestCase):
    async def test_http_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = self._client(handler)
        with self.assertRaises(EmbeddingsError):
            await client.embed(["x"])

    async def test_dimension_mismatch_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_response([[0.0, 0.0]]))  # 2 != _DIM

        client = self._client(handler)
        with self.assertRaises(EmbeddingsError) as ctx:
            await client.embed(["x"])
        self.assertIn("dimension", str(ctx.exception).lower())

    async def test_embed_raw_skips_dimension_check(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_response([[0.0, 0.0]]))

        client = self._client(handler)
        vectors = await client.embed_raw(["x"])  # the setup probe measures the real dim
        self.assertEqual(len(vectors[0]), 2)

    async def test_count_mismatch_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_response([[0.0] * _DIM]))  # 1 vector for 2 texts

        client = self._client(handler)
        with self.assertRaises(EmbeddingsError):
            await client.embed(["a", "b"])

    async def test_unconfigured_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
            raise AssertionError("no request expected")

        cfg = EmbeddingsConfig()  # no base_url/model
        client = EmbeddingsClient(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with self.assertRaises(EmbeddingsError):
            await client.embed(["x"])
