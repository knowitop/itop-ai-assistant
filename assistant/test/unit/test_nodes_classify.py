import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_openai import ChatOpenAI

import graph.enrichment.nodes.classify as classify_module
from graph.enrichment.state import Action, EnrichmentState
from state.ticket_state import TicketState


def _make_ticket(service_id: str = "0", subcategory_id: str = "0") -> dict:
    return {
        "id": 1,
        "ref": "R-000001",
        "finalclass": "UserRequest",
        "org_id": "42",
        "request_type": "incident",
        "title": "My printer is broken",
        "description": "Cannot print anything.",
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
    schema_mock = MagicMock()
    schema_mock.find = AsyncMock(return_value=[])
    schema_mock.update = AsyncMock()

    runtime = MagicMock()
    runtime.context.itop_client.schema = MagicMock(return_value=schema_mock)
    runtime.context.state_manager.get = AsyncMock(
        return_value=TicketState(rounds=0, classify_rounds=classify_rounds, ai_done=False)
    )
    runtime.context.state_manager.increment_classify_rounds = AsyncMock()
    runtime.context.state_manager.mark_done = AsyncMock()
    return runtime


def _llm_response(content: str) -> MagicMock:
    return MagicMock(content=content)


class TestClassifySkip(unittest.IsolatedAsyncioTestCase):
    async def test_classification_disabled_skips(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch("graph.enrichment.nodes.classify.get_settings") as mock_settings:
            mock_settings.return_value.enrichment.classification_enabled = False
            mock_settings.return_value.llm_model = "test"
            mock_settings.return_value.llm_api_key = "x"
            mock_settings.return_value.llm_base_url = "http://localhost"
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

        def _schema(class_name):
            m = MagicMock()
            if class_name == "Service":
                m.find = AsyncMock(return_value=_make_services())
            elif class_name == "ServiceSubcategory":
                m.find = AsyncMock(return_value=_make_subcategories())
            else:
                m.find = AsyncMock(return_value=[])
            m.update = AsyncMock()
            return m

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema)

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
        ticket_schema.update = AsyncMock()

        def _schema(class_name):
            if class_name == "Service":
                m = MagicMock()
                m.find = AsyncMock(return_value=_make_services())
                return m
            if class_name == "ServiceSubcategory":
                m = MagicMock()
                m.find = AsyncMock(return_value=_make_subcategories())
                return m
            return ticket_schema

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema)

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

        def _schema(class_name):
            m = MagicMock()
            if class_name == "ServiceSubcategory":
                m.find = AsyncMock(return_value=_make_subcategories())
            else:
                m.find = AsyncMock(return_value=[])
            m.update = AsyncMock()
            return m

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema)

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

        def _schema(class_name):
            m = MagicMock()
            m.find = AsyncMock(return_value=_make_services() if class_name == "Service" else [])
            m.update = AsyncMock()
            return m

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema)

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

        def _schema(class_name):
            m = MagicMock()
            m.find = AsyncMock(return_value=_make_subcategories() if class_name == "ServiceSubcategory" else [])
            m.update = AsyncMock()
            return m

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema)

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

        def _schema(class_name):
            m = MagicMock()
            m.find = AsyncMock(return_value=_make_services() if class_name == "Service" else [])
            m.update = AsyncMock()
            return m

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema)

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
        ticket_schema.update = AsyncMock()

        def _schema(class_name):
            if class_name == "Service":
                m = MagicMock()
                m.find = AsyncMock(return_value=_make_services())
                return m
            return ticket_schema

        runtime.context.itop_client.schema = MagicMock(side_effect=_schema)

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
