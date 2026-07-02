import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_openai import ChatOpenAI

import graph.enrichment.nodes.classify as classify_module
from config import EnrichmentConfig
from domain.catalog import Service, ServiceSubcategory
from domain.ticket import Ticket
from graph.enrichment.prompts import build_enrichment_prompts
from graph.enrichment.state import Action, EnrichmentState
from prompt_store import read_prompt_dir
from state.ticket_state import TicketState

_TEST_LLM = ChatOpenAI(model="test-model", api_key="test-key", base_url="http://localhost:9")
_PROMPTS = build_enrichment_prompts(read_prompt_dir(Path(__file__).parents[2] / "prompts" / "enrichment"))


def _make_ticket(service_id: str = "0", subcategory_id: str = "0") -> Ticket:
    return Ticket(
        obj_class="UserRequest",
        id="1",
        ref="R-000001",
        org_id="42",
        request_type="incident",
        title="My printer is broken",
        description="Cannot print anything.",
        caller_name="John Doe",
        service_id=service_id,
        subcategory_id=subcategory_id,
    )


def _make_services() -> list[Service]:
    return [
        Service(id="10", name="Printing", description="Printer issues"),
        Service(id="20", name="Network", description="Network problems"),
    ]


def _make_subcategories() -> list[ServiceSubcategory]:
    return [
        ServiceSubcategory(id="101", name="Hardware fault", service_id="10"),
        ServiceSubcategory(id="102", name="Driver issue", service_id="10"),
    ]


def _make_runtime(classify_rounds: int = 0) -> MagicMock:
    runtime = MagicMock()
    runtime.context.catalog_repo.find_services = AsyncMock(return_value=[])
    runtime.context.catalog_repo.find_subcategories = AsyncMock(return_value=[])
    runtime.context.ticket_repo.get_ai_person_name = AsyncMock(return_value="ai-assistant")
    runtime.context.ticket_repo.set_fields = AsyncMock()
    runtime.context.ticket_repo.append_private_log = AsyncMock()
    runtime.context.state_manager.get = AsyncMock(
        return_value=TicketState(rounds=0, classify_rounds=classify_rounds, ai_done=False)
    )
    runtime.context.state_manager.increment_classify_rounds = AsyncMock()
    runtime.context.state_manager.mark_done = AsyncMock()
    runtime.context.enrichment = EnrichmentConfig()
    runtime.context.prompts = _PROMPTS
    runtime.context.think_tags = ("think", "thinking", "reasoning")
    runtime.context.llm_classify = _TEST_LLM
    return runtime


def _llm_response(content: str) -> MagicMock:
    return MagicMock(content=content)


class TestClassifySkip(unittest.IsolatedAsyncioTestCase):
    async def test_classification_disabled_skips(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()
        runtime.context.enrichment = EnrichmentConfig(classification_enabled=False)

        result = await classify_module.run(state, runtime)

        self.assertEqual(result, {})
        runtime.context.catalog_repo.find_services.assert_not_called()

    async def test_zero_id_string_not_treated_as_set(self):
        state: EnrichmentState = {
            "ticket": _make_ticket(service_id="0", subcategory_id="0"),
            "action": None,
            "question": None,
        }
        runtime = _make_runtime()
        runtime.context.catalog_repo.find_services = AsyncMock(return_value=_make_services())
        runtime.context.catalog_repo.find_subcategories = AsyncMock(return_value=_make_subcategories())

        service_response = _llm_response(
            "<result><service_id>10</service_id><confidence>high</confidence><reason>ok</reason></result>"
        )
        subcategory_response = _llm_response(
            "<result><subcategory_id>101</subcategory_id><confidence>high</confidence><reason>ok</reason></result>"
        )

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(side_effect=[service_response, subcategory_response])):
            result = await classify_module.run(state, runtime)

        # Both stages should run even though IDs were "0"
        self.assertIn("ticket", result)

    async def test_both_fields_set_skips(self):
        state: EnrichmentState = {
            "ticket": _make_ticket(service_id="5", subcategory_id="3"),
            "action": None,
            "question": None,
        }
        runtime = _make_runtime()

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock()) as mock_llm:
            result = await classify_module.run(state, runtime)

        self.assertEqual(result, {})
        mock_llm.assert_not_called()


class TestClassifyHighConfidence(unittest.IsolatedAsyncioTestCase):
    async def test_high_confidence_both_stages_updates_itop(self):
        ticket = _make_ticket()
        state: EnrichmentState = {"ticket": ticket, "action": None, "question": None}
        runtime = _make_runtime()
        runtime.context.catalog_repo.find_services = AsyncMock(return_value=_make_services())
        runtime.context.catalog_repo.find_subcategories = AsyncMock(return_value=_make_subcategories())

        service_response = _llm_response(
            "<result><service_id>10</service_id><confidence>high</confidence><reason>clear match</reason></result>"
        )
        subcategory_response = _llm_response(
            "<result><subcategory_id>101</subcategory_id><confidence>high</confidence><reason>ok</reason></result>"
        )

        responses = [service_response, subcategory_response]
        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(side_effect=responses)):
            result = await classify_module.run(state, runtime)

        self.assertIn("ticket", result)
        self.assertEqual(result["ticket"].service_id, "10")
        self.assertEqual(result["ticket"].subcategory_id, "101")
        self.assertNotIn("action", result)

        # iTop updated once with both semantic fields
        runtime.context.ticket_repo.set_fields.assert_awaited_once_with(
            ticket, {"service_id": "10", "subcategory_id": "101"}
        )

    async def test_service_already_set_only_runs_stage2(self):
        state: EnrichmentState = {
            "ticket": _make_ticket(service_id="10"),
            "action": None,
            "question": None,
        }
        runtime = _make_runtime()
        runtime.context.catalog_repo.find_subcategories = AsyncMock(return_value=_make_subcategories())

        subcategory_response = _llm_response(
            "<result><subcategory_id>101</subcategory_id><confidence>high</confidence><reason>ok</reason></result>"
        )

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=subcategory_response)) as mock_llm:
            result = await classify_module.run(state, runtime)

        # Only one LLM call (subcategory, not service)
        self.assertEqual(mock_llm.call_count, 1)
        runtime.context.catalog_repo.find_services.assert_not_called()
        self.assertIn("ticket", result)
        self.assertEqual(result["ticket"].subcategory_id, "101")


class TestClassifyLowConfidence(unittest.IsolatedAsyncioTestCase):
    async def test_low_confidence_service_asks_question(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime(classify_rounds=0)
        runtime.context.catalog_repo.find_services = AsyncMock(return_value=_make_services())

        service_response = _llm_response(
            "<result><service_id></service_id><confidence>low</confidence><reason>unclear</reason></result>"
        )
        ask_response = _llm_response("Could you describe what exactly stopped working?")

        responses = [service_response, ask_response]
        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(side_effect=responses)):
            result = await classify_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ASK)
        self.assertEqual(result["question"], "Could you describe what exactly stopped working?")
        runtime.context.state_manager.increment_classify_rounds.assert_called_once_with("UserRequest::1")

    async def test_low_confidence_subcategory_asks_question(self):
        state: EnrichmentState = {
            "ticket": _make_ticket(service_id="10"),
            "action": None,
            "question": None,
        }
        runtime = _make_runtime(classify_rounds=0)
        runtime.context.catalog_repo.find_subcategories = AsyncMock(return_value=_make_subcategories())

        subcategory_response = _llm_response(
            "<result><subcategory_id></subcategory_id><confidence>low</confidence><reason>unclear</reason></result>"
        )
        ask_response = _llm_response("What exactly is happening with the printer?")

        responses = [subcategory_response, ask_response]
        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(side_effect=responses)):
            result = await classify_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ASK)
        self.assertIn("question", result)

    async def test_invalid_service_id_treated_as_low_confidence(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime(classify_rounds=0)
        runtime.context.catalog_repo.find_services = AsyncMock(return_value=_make_services())

        # LLM returns ID not in the list
        service_response = _llm_response(
            "<result><service_id>999</service_id><confidence>high</confidence><reason>match</reason></result>"
        )
        ask_response = _llm_response("What is the issue?")

        responses = [service_response, ask_response]
        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(side_effect=responses)):
            result = await classify_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ASK)


class TestClassifyFallback(unittest.IsolatedAsyncioTestCase):
    async def test_classify_rounds_exhausted_triggers_fallback(self):
        ticket = _make_ticket()
        state: EnrichmentState = {"ticket": ticket, "action": None, "question": None}
        runtime = _make_runtime(classify_rounds=2)
        runtime.context.catalog_repo.find_services = AsyncMock(return_value=_make_services())

        service_response = _llm_response(
            "<result><service_id></service_id><confidence>low</confidence><reason>unclear</reason></result>"
        )

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=service_response)):
            result = await classify_module.run(state, runtime)

        self.assertEqual(result["action"], Action.STOP)
        runtime.context.state_manager.mark_done.assert_called_once_with("UserRequest::1")
        runtime.context.state_manager.increment_classify_rounds.assert_not_called()

        # Fallback note written to private log
        runtime.context.ticket_repo.append_private_log.assert_awaited_once()
        note = runtime.context.ticket_repo.append_private_log.await_args.args[1]
        self.assertEqual(note, EnrichmentConfig().classify_fallback_note)


if __name__ == "__main__":
    unittest.main()
