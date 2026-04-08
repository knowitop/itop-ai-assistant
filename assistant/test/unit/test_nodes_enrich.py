import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_openai import ChatOpenAI

import graph.enrichment.nodes.enrich as enrich_module
from graph.enrichment.state import EnrichmentState


def _make_ticket() -> dict:
    return {
        "id": 1,
        "ref": "R-000001",
        "finalclass": "UserRequest",
        "title": "Broken laptop",
        "description": "My laptop does not turn on.",
        "caller_id_friendlyname": "John Doe",
        "public_log": {"entries": []},
    }


def _make_runtime() -> MagicMock:
    schema_mock = MagicMock()
    schema_mock.update = AsyncMock()

    runtime = MagicMock()
    runtime.context.itop_client.schema = MagicMock(return_value=schema_mock)
    runtime.context.state_manager.mark_done = AsyncMock()
    return runtime


class TestEnrichEmptyLLMResponse(unittest.IsolatedAsyncioTestCase):
    async def test_none_content_skips_update_but_marks_done(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=MagicMock(content=None))):
            await enrich_module.run(state, runtime)

        runtime.context.itop_client.schema.assert_not_called()
        runtime.context.state_manager.mark_done.assert_called_once_with("UserRequest::1")

    async def test_empty_string_content_skips_update_but_marks_done(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=MagicMock(content=""))):
            await enrich_module.run(state, runtime)

        runtime.context.itop_client.schema.assert_not_called()
        runtime.context.state_manager.mark_done.assert_called_once_with("UserRequest::1")

    async def test_normal_content_updates_itop_and_marks_done(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(
            ChatOpenAI,
            "ainvoke",
            new=AsyncMock(return_value=MagicMock(content="Summary: laptop issue.")),
        ):
            await enrich_module.run(state, runtime)

        runtime.context.itop_client.schema.assert_called_once_with("UserRequest")
        runtime.context.itop_client.schema.return_value.update.assert_called_once_with(
            {"id": 1},
            {"private_log": {"add_item": {"message": "Summary: laptop issue.", "format": "text"}}},
        )
        runtime.context.state_manager.mark_done.assert_called_once_with("UserRequest::1")


if __name__ == "__main__":
    unittest.main()
