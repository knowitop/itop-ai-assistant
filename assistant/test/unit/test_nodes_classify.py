import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_openai import ChatOpenAI

import graph.enrichment.nodes.classify as classify_module
from config import EnrichmentConfig
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


def _make_services() -> list[dict]:
    return [
        {"id": "10", "name": "Printing", "description": "Printer issues"},
        {"id": "20", "name": "Network", "description": "Network problems"},
    ]


def _make_subcategories() -> list[dict]:
    return [
        {"id": "101", "name": "Hardware fault", "description": ""},
        {"id": "102", "name": "Driver issue", "description": ""},
    ]


def _schema_with(service: list | None = None, subcategory: list | None = None):
    """Factory: returns a _schema(class_name) serving Service/ServiceSubcategory option lists."""

    def _schema(class_name):
        m = MagicMock()
        if class_name == "Service" and service is not None:
            m.find = AsyncMock(return_value=service)
        elif class_name == "ServiceSubcategory" and subcategory is not None:
            m.find = AsyncMock(return_value=subcategory)
        else:
            m.find = AsyncMock(return_value=[])
        return m

    return _schema


def _make_runtime(classify_rounds: int = 0) -> MagicMock:
    runtime = MagicMock()
    runtime.context.itop_client.schema = MagicMock(side_effect=_schema_with())
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
        runtime.context.itop_client.schema.assert_not_called()

    async def test_zero_id_string_not_treated_as_set(self):
        state: EnrichmentState = {
            "ticket": _make_ticket(service_id="0", subcategory_id="0"),
            "action": None,
            "question": None,
        }
        runtime = _make_runtime()

        service_response = _llm_response(
            "<result><service_id>10</service_id><confidence>high</confidence><reasoning>ok</reasoning></result>"
        )
        subcategory_response = _llm_response(
            "<result><subcategory_id>101</subcategory_id><confidence>high</confidence><reasoning>ok</reasoning></result>"
        )

        runtime.context.itop_client.schema = MagicMock(
            side_effect=_schema_with(service=_make_services(), subcategory=_make_subcategories())
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

        service_response = _llm_response(
            "<result><service_id>10</service_id><confidence>high</confidence><reasoning>clear match</reasoning></result>"
        )
        subcategory_response = _llm_response(
            "<result><subcategory_id>101</subcategory_id><confidence>high</confidence><reasoning>ok</reasoning></result>"
        )

        runtime.context.itop_client.schema = MagicMock(
            side_effect=_schema_with(service=_make_services(), subcategory=_make_subcategories())
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

        subcategory_response = _llm_response(
            "<result><subcategory_id>101</subcategory_id><confidence>high</confidence><reasoning>ok</reasoning></result>"
        )

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema_with(subcategory=_make_subcategories()))

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=subcategory_response)) as mock_llm:
            result = await classify_module.run(state, runtime)

        # Only one LLM call (subcategory, not service)
        self.assertEqual(mock_llm.call_count, 1)
        self.assertIn("ticket", result)
        self.assertEqual(result["ticket"].subcategory_id, "101")


class TestClassifyLowConfidence(unittest.IsolatedAsyncioTestCase):
    async def test_low_confidence_service_asks_question(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime(classify_rounds=0)

        service_response = _llm_response(
            "<result><service_id></service_id><confidence>low</confidence><reasoning>unclear</reasoning></result>"
        )
        ask_response = _llm_response("Could you describe what exactly stopped working?")

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema_with(service=_make_services()))

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

        subcategory_response = _llm_response(
            "<result><subcategory_id></subcategory_id><confidence>low</confidence><reasoning>unclear</reasoning></result>"
        )
        ask_response = _llm_response("What exactly is happening with the printer?")

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema_with(subcategory=_make_subcategories()))

        responses = [subcategory_response, ask_response]
        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(side_effect=responses)):
            result = await classify_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ASK)
        self.assertIn("question", result)

    async def test_invalid_service_id_treated_as_low_confidence(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime(classify_rounds=0)

        # LLM returns ID not in the list
        service_response = _llm_response(
            "<result><service_id>999</service_id><confidence>high</confidence><reasoning>match</reasoning></result>"
        )
        ask_response = _llm_response("What is the issue?")

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema_with(service=_make_services()))

        responses = [service_response, ask_response]
        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(side_effect=responses)):
            result = await classify_module.run(state, runtime)

        self.assertEqual(result["action"], Action.ASK)


class TestClassifyFallback(unittest.IsolatedAsyncioTestCase):
    async def test_classify_rounds_exhausted_triggers_fallback(self):
        ticket = _make_ticket()
        state: EnrichmentState = {"ticket": ticket, "action": None, "question": None}
        runtime = _make_runtime(classify_rounds=2)

        service_response = _llm_response(
            "<result><service_id></service_id><confidence>low</confidence><reasoning>unclear</reasoning></result>"
        )

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema_with(service=_make_services()))

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
