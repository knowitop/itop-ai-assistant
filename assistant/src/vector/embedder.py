"""OpenAI-compatible embeddings client (LM Studio, LiteLLM, cloud).

Same integration pattern as the chat LLM: any /v1/embeddings endpoint,
Bearer auth optional. The OpenAI `dimensions` truncation parameter is
deliberately not sent — many local servers reject unknown params; instead
`cfg.dimension` is a declaration validated against what the endpoint
actually returns.
"""

import logging
from collections.abc import Sequence

import httpx

from config import EmbeddingsConfig

logger = logging.getLogger(__name__)


class EmbeddingsError(Exception):
    """The embeddings endpoint failed or returned an unusable response."""


class EmbeddingsClient:
    def __init__(self, cfg: EmbeddingsConfig, client: httpx.AsyncClient | None = None) -> None:
        self._cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=cfg.timeout)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts in batches of `cfg.batch_size`, preserving input order."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._cfg.batch_size):
            batch = list(texts[start : start + self._cfg.batch_size])
            batch_vectors = await self.embed_raw(batch)
            for vector in batch_vectors:
                if len(vector) != self._cfg.dimension:
                    raise EmbeddingsError(
                        f"Embedding dimension mismatch: endpoint returned {len(vector)}, "
                        f"config expects {self._cfg.dimension} (embeddings: dimension)"
                    )
            vectors.extend(batch_vectors)
        return vectors

    async def embed_raw(self, texts: list[str]) -> list[list[float]]:
        """One /embeddings call, no dimension check — the setup probe uses this
        directly to *measure* the endpoint's real dimension."""
        if not self._cfg.base_url:
            raise EmbeddingsError("Embeddings endpoint is not configured (embeddings: base_url)")
        if not self._cfg.model:
            raise EmbeddingsError("Embeddings model is not configured (embeddings: model)")

        headers = {}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"
        url = self._cfg.base_url.rstrip("/") + "/embeddings"
        try:
            response = await self._client.post(url, json={"model": self._cfg.model, "input": texts}, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as e:
            raise EmbeddingsError(f"Embeddings request failed: {e.response.status_code} {e.response.text}") from e
        except httpx.HTTPError as e:
            raise EmbeddingsError(f"Embeddings request failed: {e}") from e

        try:
            items = sorted(payload["data"], key=lambda item: item["index"])
            vectors = [item["embedding"] for item in items]
        except (KeyError, TypeError) as e:
            raise EmbeddingsError(f"Malformed embeddings response: {e}") from e
        if len(vectors) != len(texts):
            raise EmbeddingsError(f"Embeddings count mismatch: sent {len(texts)} texts, got {len(vectors)} vectors")
        return vectors

    async def aclose(self) -> None:
        await self._client.aclose()
