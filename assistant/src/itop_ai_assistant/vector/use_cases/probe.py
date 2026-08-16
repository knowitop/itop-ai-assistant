"""Measuring the embeddings endpoint's real vector dimension.

The setup wizard's `POST /api/setup/test-embeddings` used to build and close
its own `EmbeddingsClient` — the same rule 6.2 gap `SimilarSearch` had before
TASK-033. This is that operation as a door of its own rather than folded into
the search door: the wizard is not searching, and giving it `SimilarSearch`
just to reach a client through it would be the detail-passing this task
removes, one layer further in.
"""

from itop_ai_assistant.config import EmbeddingsConfig
from itop_ai_assistant.vector.adapters.embedder import EmbeddingsClient


async def measure_embedding_dimension(cfg: EmbeddingsConfig) -> int:
    """The vector length one `/embeddings` call actually returns, or 0 for an
    empty response. Errors from the endpoint propagate as-is — the wizard's
    own `except` turns them into `{"ok": false, "error": ...}`."""
    client = EmbeddingsClient(cfg)
    try:
        vectors = await client.embed_raw(["ping"])
    finally:
        await client.aclose()
    return len(vectors[0]) if vectors else 0
