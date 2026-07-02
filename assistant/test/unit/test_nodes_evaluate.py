import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_openai import ChatOpenAI

import graph.enrichment.nodes.evaluate as evaluate_module
from config import EnrichmentConfig
from domain.ticket import Ticket
from graph.enrichment.prompts import build_enrichment_prompts
from graph.enrichment.state import Action, EnrichmentState
from prompt_store import read_prompt_dir
from state.ticket_state import TicketState

_TEST_LLM = ChatOpenAI(model="test-model", api_key="test-key", base_url="http://localhost:9")
_PROMPTS = build_enrichment_prompts(read_prompt_dir(Path(__file__).parents[2] / "prompts" / "enrichment"))


def _make_ticket(**overrides) -> Ticket:
    base = {
        "obj_class": "UserRequest",
        "id": "1",
        "ref": "R-000001",
        "title": "Broken laptop",
        "description": "My laptop does not turn on.",
        "service_id": "5",
        "subcategory_id": "3",
        "caller_name": "John Doe",
    }
    return Ticket(**{**base, **overrides})


def _make_runtime() -> MagicMock:
    def _schema(class_name):
        m = MagicMock()
        data = {
            "Service": {"name": "IT", "description": ""},
            "ServiceSubcategory": {"name": "Hardware", "description": ""},
        }
        m.find_one = AsyncMock(return_value=data.get(class_name))
        return m

    runtime = MagicMock()
    runtime.context.state_manager.get = AsyncMock(return_value=TicketState(rounds=0, ai_done=False))
    runtime.context.itop_client.schema = MagicMock(side_effect=_schema)
    runtime.context.ticket_repo.get_ai_person_name = AsyncMock(return_value="ai-assistant")
    runtime.context.enrichment = EnrichmentConfig()
    runtime.context.prompts = _PROMPTS
    runtime.context.llm_evaluate = _TEST_LLM
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

        with patch.object(
            ChatOpenAI,
            "ainvoke",
            new=AsyncMock(return_value=MagicMock(content="<result>SUFFICIENT</result>")),
        ):
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


class TestEvaluateEarlyReturns(unittest.IsolatedAsyncioTestCase):
    async def test_no_service_context_returns_enrich(self):
        state: EnrichmentState = {"ticket": _make_ticket(service_id="0"), "action": None, "question": None}
        runtime = _make_runtime()

        result = await evaluate_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ENRICH)
        runtime.context.state_manager.get.assert_not_called()

    async def test_rounds_exhausted_returns_enrich(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()
        runtime.context.state_manager.get = AsyncMock(return_value=TicketState(rounds=2, ai_done=False))

        result = await evaluate_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ENRICH)


class TestBuildServiceContext(unittest.IsolatedAsyncioTestCase):
    async def _run_with_schema(self, service: dict | None, subcategory: dict | None) -> str:
        def _schema(class_name):
            m = MagicMock()
            data = {"Service": service, "ServiceSubcategory": subcategory}
            m.find_one = AsyncMock(return_value=data.get(class_name))
            return m

        itop_client = MagicMock()
        itop_client.schema = MagicMock(side_effect=_schema)
        ticket = _make_ticket()
        return await evaluate_module._build_service_context(ticket, itop_client)

    async def test_service_and_subcategory_with_descriptions(self):
        result = await self._run_with_schema(
            service={"name": "IT", "description": "IT services"},
            subcategory={"name": "Hardware", "description": "Hardware issues"},
        )

        self.assertIn("Service: IT", result)
        self.assertIn("Service description:\nIT services", result)
        self.assertIn("Subcategory: Hardware", result)
        self.assertIn("Subcategory description:\nHardware issues", result)

    async def test_service_without_description(self):
        result = await self._run_with_schema(
            service={"name": "IT", "description": ""},
            subcategory=None,
        )

        self.assertIn("Service: IT", result)
        self.assertNotIn("Service description", result)

    async def test_subcategory_without_description(self):
        result = await self._run_with_schema(
            service=None,
            subcategory={"name": "Hardware", "description": ""},
        )

        self.assertIn("Subcategory: Hardware", result)
        self.assertNotIn("Subcategory description", result)

    async def test_no_service_no_subcategory_returns_fallback(self):
        result = await self._run_with_schema(service=None, subcategory=None)

        self.assertEqual(result, "No service context provided.")


if __name__ == "__main__":
    unittest.main()
