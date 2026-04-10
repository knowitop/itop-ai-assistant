import unittest
from unittest.mock import AsyncMock, MagicMock

import graph.enrichment.nodes.ask as ask_module
from graph.enrichment.state import EnrichmentState


def _make_ticket() -> dict:
    return {
        "id": 1,
        "ref": "R-000001",
        "finalclass": "UserRequest",
    }


def _make_runtime() -> MagicMock:
    ticket_schema = MagicMock()
    ticket_schema.update = AsyncMock()

    runtime = MagicMock()
    runtime.context.itop_client.schema = MagicMock(return_value=ticket_schema)
    runtime.context.state_manager.increment_rounds = AsyncMock()
    return runtime


class TestAskNode(unittest.IsolatedAsyncioTestCase):
    async def test_posts_question_to_public_log(self):
        question = "What is the serial number of your laptop?"
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": question}
        runtime = _make_runtime()

        await ask_module.run(state, runtime)

        runtime.context.itop_client.schema.assert_called_with("UserRequest")
        runtime.context.itop_client.schema("UserRequest").update.assert_called_once_with(
            {"id": 1},
            {"public_log": {"add_item": {"message": question, "format": "text"}}},
        )

    async def test_increments_rounds(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": "Any question?"}
        runtime = _make_runtime()

        await ask_module.run(state, runtime)

        runtime.context.state_manager.increment_rounds.assert_called_once_with("UserRequest::1")

    async def test_returns_empty_dict(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": "Any question?"}
        runtime = _make_runtime()

        result = await ask_module.run(state, runtime)

        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
