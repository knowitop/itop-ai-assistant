import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_openai import ChatOpenAI

import graph.enrichment.nodes.classify as classify_module
from config import EnrichmentConfig
from graph.enrichment.state import Action, EnrichmentState
from state.ticket_state import TicketState

_TEST_LLM = ChatOpenAI(model="test-model", api_key="test-key", base_url="http://localhost:9")


def _make_ticket(service_id: str = "0", subcategory_id: str = "0") -> dict:
    return {
        "id": 1,
        "ref": "R-000001",
        "finalclass": "UserRequest",
        "org_id": "42",
        "request_type": "incident",
        "title": "My printer is broken",
        "description": "Cannot print anything.",
        "caller_id_friendlyname": "John Doe",
        "service_id": service_id,
        "servicesubcategory_id": subcategory_id,
        "public_log": {"entries": []},
    }


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


def _make_runtime(classify_rounds: int = 0) -> MagicMock:
    def _schema(class_name):
        m = MagicMock()
        m.update = AsyncMock()
        if class_name == "Person":
            m.find_one = AsyncMock(return_value={"friendlyname": "ai-assistant"})
        else:
            m.find = AsyncMock(return_value=[])
        return m

    runtime = MagicMock()
    runtime.context.itop_client.schema = MagicMock(side_effect=_schema)
    runtime.context.state_manager.get = AsyncMock(
        return_value=TicketState(rounds=0, classify_rounds=classify_rounds, ai_done=False)
    )
    runtime.context.state_manager.increment_classify_rounds = AsyncMock()
    runtime.context.state_manager.mark_done = AsyncMock()
    runtime.context.enrichment = EnrichmentConfig()
    runtime.context.llm_classify = _TEST_LLM
    return runtime


def _llm_response(content: str) -> MagicMock:
    return MagicMock(content=content)


def _schema_with(service=None, subcategory=None, ticket_schema=None):
    """Factory: returns a _schema(class_name) that handles Person + optionally Service/Subcategory."""

    def _schema(class_name):
        m = MagicMock()
        m.update = AsyncMock()
        if class_name == "Person":
            m.find_one = AsyncMock(return_value={"friendlyname": "ai-assistant"})
        elif class_name == "Service" and service is not None:
            m.find = AsyncMock(return_value=service)
        elif class_name == "ServiceSubcategory" and subcategory is not None:
            m.find = AsyncMock(return_value=subcategory)
        elif ticket_schema is not None:
            return ticket_schema
        else:
            m.find = AsyncMock(return_value=[])
        return m

    return _schema


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
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        service_response = _llm_response(
            "<result><service_id>10</service_id><confidence>high</confidence><reasoning>clear match</reasoning></result>"
        )
        subcategory_response = _llm_response(
            "<result><subcategory_id>101</subcategory_id><confidence>high</confidence><reasoning>ok</reasoning></result>"
        )

        ticket_schema = MagicMock()
        ticket_schema.find = AsyncMock(return_value={"friendlyname": "ai-assistant"})
        ticket_schema.update = AsyncMock()

        runtime.context.itop_client.schema = MagicMock(
            side_effect=_schema_with(
                service=_make_services(), subcategory=_make_subcategories(), ticket_schema=ticket_schema
            )
        )

        responses = [service_response, subcategory_response]
        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(side_effect=responses)):
            result = await classify_module.run(state, runtime)

        self.assertIn("ticket", result)
        self.assertEqual(result["ticket"]["service_id"], "10")
        self.assertEqual(result["ticket"]["servicesubcategory_id"], "101")
        self.assertNotIn("action", result)

        # iTop updated once with both fields
        ticket_schema.update.assert_called_once()
        update_fields = ticket_schema.update.call_args[0][1]
        self.assertEqual(update_fields["service_id"], "10")
        self.assertEqual(update_fields["servicesubcategory_id"], "101")

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
        self.assertEqual(result["ticket"]["servicesubcategory_id"], "101")


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
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime(classify_rounds=2)

        service_response = _llm_response(
            "<result><service_id></service_id><confidence>low</confidence><reasoning>unclear</reasoning></result>"
        )

        ticket_schema = MagicMock()
        ticket_schema.find = AsyncMock(return_value={"friendlyname": "ai-assistant"})
        ticket_schema.update = AsyncMock()

        runtime.context.itop_client.schema = MagicMock(
            side_effect=_schema_with(service=_make_services(), ticket_schema=ticket_schema)
        )

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=service_response)):
            result = await classify_module.run(state, runtime)

        self.assertEqual(result["action"], Action.STOP)
        runtime.context.state_manager.mark_done.assert_called_once_with("UserRequest::1")
        runtime.context.state_manager.increment_classify_rounds.assert_not_called()

        # Fallback note written to private_log
        ticket_schema.update.assert_called_once()
        update_fields = ticket_schema.update.call_args[0][1]
        self.assertIn("private_log", update_fields)


if __name__ == "__main__":
    unittest.main()
