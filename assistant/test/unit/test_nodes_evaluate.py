import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_openai import ChatOpenAI

import graph.enrichment.nodes.evaluate as evaluate_module
from graph.enrichment.state import Action, EnrichmentState
from state.ticket_state import TicketState


def _make_ticket() -> dict:
    return {
        "id": 1,
        "ref": "R-000001",
        "finalclass": "UserRequest",
        "title": "Broken laptop",
        "description": "My laptop does not turn on.",
        "service_id": "5",
        "servicesubcategory_id": "3",
        "caller_id_friendlyname": "John Doe",
        "public_log": {"entries": []},
    }


def _make_runtime() -> MagicMock:
    def _schema(class_name):
        m = MagicMock()
        data = {
            "Service": {"name": "IT", "description": ""},
            "ServiceSubcategory": {"name": "Hardware", "description": ""},
            "Person": {"friendlyname": "ai-assistant"},
        }
        m.find = AsyncMock(return_value=data.get(class_name, {}))
        return m

    runtime = MagicMock()
    runtime.context.state_manager.get = AsyncMock(return_value=TicketState(rounds=0, ai_done=False))
    runtime.context.itop_client.schema = MagicMock(side_effect=_schema)
    return runtime


class TestEvaluateEmptyLLMResponse(unittest.IsolatedAsyncioTestCase):
    async def test_none_content_returns_enrich(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=MagicMock(content=None))):
            result = await evaluate_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ENRICH)
        self.assertNotIn("question", result)

    async def test_empty_string_content_returns_enrich(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=MagicMock(content=""))):
            result = await evaluate_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ENRICH)

    async def test_only_think_block_returns_enrich(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(
            ChatOpenAI, "ainvoke", new=AsyncMock(return_value=MagicMock(content="<think>reasoning</think>"))
        ):
            result = await evaluate_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ENRICH)

    async def test_sufficient_response_returns_enrich(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=MagicMock(content="SUFFICIENT"))):
            result = await evaluate_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ENRICH)

    async def test_question_response_returns_ask(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(
            ChatOpenAI,
            "ainvoke",
            new=AsyncMock(return_value=MagicMock(content="What is the model of your laptop?")),
        ):
            result = await evaluate_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ASK)
        self.assertEqual(result["question"], "What is the model of your laptop?")


if __name__ == "__main__":
    unittest.main()
