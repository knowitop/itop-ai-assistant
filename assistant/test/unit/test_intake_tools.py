import unittest
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage, ToolMessage

from agents.intake import tools
from agents.intake.tools import ToolRejection
from config import IntakeConfig
from domain.catalog import Service, ServiceSubcategory
from domain.ticket import Ticket
from state.ticket_state import TicketState


def _ticket(**overrides) -> Ticket:
    defaults = dict(obj_class="Incident", id="1", org_id="42", title="Printer is dead", caller_name="John Doe")
    return Ticket(**{**defaults, **overrides})


def _services() -> list[Service]:
    return [
        Service(id="10", name="Printing", description="Printer issues"),
        Service(id="20", name="Network", description="Network problems"),
    ]


def _subcategories() -> list[ServiceSubcategory]:
    return [
        ServiceSubcategory(id="101", name="Hardware fault", description="Provide the printer model", service_id="10"),
        ServiceSubcategory(id="102", name="Driver issue", service_id="10"),
    ]


def _make_runtime(ticket: Ticket | None = None, messages: list | None = None, **state_kwargs) -> MagicMock:
    """Mirror of test_nodes_classify._make_runtime, for ToolRuntime."""
    runtime = MagicMock()
    runtime.tool_call_id = "current"
    runtime.state = {"messages": messages or []}
    runtime.context.ticket = ticket or _ticket()
    runtime.context.intake = IntakeConfig()
    runtime.context.ai_name = "ai-assistant"
    runtime.context.think_tags = ("think",)
    runtime.context.catalog_repo.find_services = AsyncMock(return_value=_services())
    runtime.context.catalog_repo.find_subcategories = AsyncMock(return_value=_subcategories())
    runtime.context.ticket_repo.set_fields = AsyncMock()
    runtime.context.ticket_repo.append_public_log = AsyncMock()
    runtime.context.ticket_repo.append_private_log = AsyncMock()
    runtime.context.state_manager.get = AsyncMock(return_value=TicketState(**state_kwargs))
    runtime.context.state_manager.increment_rounds = AsyncMock()
    runtime.context.state_manager.increment_classify_rounds = AsyncMock()
    runtime.context.state_manager.mark_done = AsyncMock()
    return runtime


class TestToolSchemas(unittest.TestCase):
    """The injected runtime must never reach the model's tool schema."""

    def test_runtime_is_not_in_the_model_facing_schema(self):
        self.assertEqual(
            set(tools.get_subcategories.tool_call_schema.model_json_schema()["properties"]), {"service_id"}
        )
        self.assertEqual(
            set(tools.set_classification.tool_call_schema.model_json_schema()["properties"]),
            {"service_id", "subcategory_id"},
        )
        self.assertEqual(
            set(tools.post_public_question.tool_call_schema.model_json_schema()["properties"]), {"question"}
        )
        self.assertEqual(set(tools.finish_handoff.tool_call_schema.model_json_schema()["properties"]), {"note"})
        self.assertEqual(tools.get_service_catalog.tool_call_schema.model_json_schema().get("properties", {}), {})

    def test_ids_are_typed_as_integers(self):
        # pydantic v2 does not coerce int -> str in lax mode, so a model
        # sending {"service_id": 10} must not hit a ValidationError
        props = tools.set_classification.tool_call_schema.model_json_schema()["properties"]
        self.assertEqual(props["service_id"]["type"], "integer")
        self.assertEqual(props["subcategory_id"]["type"], "integer")

    def test_every_tool_is_registered(self):
        self.assertEqual(
            [t.name for t in tools.TOOLS],
            [
                "get_service_catalog",
                "get_subcategories",
                "set_classification",
                "post_public_question",
                "finish_handoff",
            ],
        )


class TestGetServiceCatalog(unittest.IsolatedAsyncioTestCase):
    async def test_lists_services_with_ids(self):
        runtime = _make_runtime()

        result = await tools.get_service_catalog.coroutine(runtime=runtime)

        self.assertIn("- ID 10: Printing", result)
        self.assertIn("- ID 20: Network", result)

    async def test_oql_is_bound_to_the_ticket(self):
        runtime = _make_runtime(_ticket(org_id="7"))

        await tools.get_service_catalog.coroutine(runtime=runtime)

        oql = runtime.context.catalog_repo.find_services.await_args.args[0]
        self.assertIn("org_id = 7", oql)


class TestGetSubcategories(unittest.IsolatedAsyncioTestCase):
    async def test_lists_subcategories_with_requirements(self):
        runtime = _make_runtime()

        result = await tools.get_subcategories.coroutine(service_id=10, runtime=runtime)

        self.assertIn("- ID 101: Hardware fault — Provide the printer model", result)
        oql = runtime.context.catalog_repo.find_subcategories.await_args.args[0]
        self.assertIn("service_id = 10", oql)

    async def test_empty_result_is_rejected_with_an_alternative(self):
        runtime = _make_runtime()
        runtime.context.catalog_repo.find_subcategories = AsyncMock(return_value=[])

        with self.assertRaises(ToolRejection) as ctx:
            await tools.get_subcategories.coroutine(service_id=99, runtime=runtime)

        self.assertIn("different service", str(ctx.exception))


class TestRepeatGuard(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _history(name: str, args: dict, result: str) -> list:
        return [
            AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "earlier"}]),
            ToolMessage(content=result, name=name, tool_call_id="earlier"),
        ]

    async def test_identical_repeat_is_rejected_with_the_previous_result(self):
        runtime = _make_runtime(messages=self._history("get_subcategories", {"service_id": 10}, "ID 101: Hardware"))

        with self.assertRaises(ToolRejection) as ctx:
            await tools.get_subcategories.coroutine(service_id=10, runtime=runtime)

        self.assertIn("ID 101: Hardware", str(ctx.exception))
        runtime.context.catalog_repo.find_subcategories.assert_not_called()

    async def test_string_and_int_arguments_count_as_the_same_call(self):
        runtime = _make_runtime(messages=self._history("get_subcategories", {"service_id": "10"}, "previous"))

        with self.assertRaises(ToolRejection):
            await tools.get_subcategories.coroutine(service_id=10, runtime=runtime)

    async def test_different_arguments_pass(self):
        runtime = _make_runtime(messages=self._history("get_subcategories", {"service_id": 10}, "previous"))

        result = await tools.get_subcategories.coroutine(service_id=20, runtime=runtime)

        self.assertIn("- ID 101:", result)

    async def test_the_current_call_does_not_count_as_its_own_repeat(self):
        runtime = _make_runtime(
            messages=[AIMessage(content="", tool_calls=[{"name": "get_service_catalog", "args": {}, "id": "current"}])]
        )

        result = await tools.get_service_catalog.coroutine(runtime=runtime)

        self.assertIn("- ID 10: Printing", result)


class TestSetClassification(unittest.IsolatedAsyncioTestCase):
    async def test_valid_ids_are_written_and_the_snapshot_follows(self):
        runtime = _make_runtime()

        result = await tools.set_classification.coroutine(service_id=10, subcategory_id=101, runtime=runtime)

        runtime.context.ticket_repo.set_fields.assert_awaited_once()
        self.assertEqual(
            runtime.context.ticket_repo.set_fields.await_args.args[1],
            {"service_id": "10", "subcategory_id": "101"},
        )
        self.assertEqual(runtime.context.ticket.service_id, "10")
        self.assertEqual(runtime.context.ticket.subcategory_id, "101")
        # The subcategory requirements come back with the confirmation
        self.assertIn("Provide the printer model", result)

    async def test_unknown_service_is_rejected_with_valid_ids_and_a_way_out(self):
        runtime = _make_runtime()

        with self.assertRaises(ToolRejection) as ctx:
            await tools.set_classification.coroutine(service_id=999, subcategory_id=101, runtime=runtime)

        message = str(ctx.exception)
        self.assertIn("10", message)
        self.assertIn("20", message)
        self.assertIn("post_public_question", message)
        runtime.context.ticket_repo.set_fields.assert_not_called()

    async def test_subcategory_of_another_service_is_rejected(self):
        runtime = _make_runtime()
        runtime.context.catalog_repo.find_subcategories = AsyncMock(
            return_value=[ServiceSubcategory(id="101", name="Hardware fault", service_id="10")]
        )

        with self.assertRaises(ToolRejection) as ctx:
            await tools.set_classification.coroutine(service_id=10, subcategory_id=999, runtime=runtime)

        self.assertIn("101", str(ctx.exception))
        self.assertIn("post_public_question", str(ctx.exception))
        runtime.context.ticket_repo.set_fields.assert_not_called()

    async def test_repeating_the_same_classification_does_not_write_twice(self):
        runtime = _make_runtime(_ticket(service_id="10", subcategory_id="101"))

        result = await tools.set_classification.coroutine(service_id=10, subcategory_id=101, runtime=runtime)

        runtime.context.ticket_repo.set_fields.assert_not_called()
        self.assertIn("already has this classification", result)


class TestPostPublicQuestion(unittest.IsolatedAsyncioTestCase):
    async def test_empty_question_is_rejected(self):
        runtime = _make_runtime()

        with self.assertRaises(ToolRejection):
            await tools.post_public_question.coroutine(question="   ", runtime=runtime)

        runtime.context.ticket_repo.append_public_log.assert_not_called()

    async def test_unclassified_ticket_spends_the_classify_budget(self):
        runtime = _make_runtime()

        await tools.post_public_question.coroutine(question="What exactly broke?", runtime=runtime)

        runtime.context.ticket_repo.append_public_log.assert_awaited_once_with(
            runtime.context.ticket, "What exactly broke?"
        )
        runtime.context.state_manager.increment_classify_rounds.assert_awaited_once_with("Incident::1")
        runtime.context.state_manager.increment_rounds.assert_not_called()

    async def test_classified_ticket_spends_the_completeness_budget(self):
        runtime = _make_runtime(_ticket(service_id="10", subcategory_id="101"))

        await tools.post_public_question.coroutine(question="Which printer model?", runtime=runtime)

        runtime.context.state_manager.increment_rounds.assert_awaited_once_with("Incident::1")
        runtime.context.state_manager.increment_classify_rounds.assert_not_called()

    async def test_exhausted_classify_budget_hands_the_ticket_over(self):
        runtime = _make_runtime(classify_rounds=2)

        # Success, not a rejection: the run must end here, so the model never
        # gets a turn in which it could call finish_handoff over this note
        result = await tools.post_public_question.coroutine(question="one more?", runtime=runtime)

        runtime.context.ticket_repo.append_private_log.assert_awaited_once_with(
            runtime.context.ticket, IntakeConfig().classify_fallback_note
        )
        runtime.context.state_manager.mark_done.assert_awaited_once_with("Incident::1")
        runtime.context.ticket_repo.append_public_log.assert_not_called()
        self.assertIn("Your session is over", result)

    async def test_exhausted_rounds_budget_points_at_the_handoff(self):
        runtime = _make_runtime(_ticket(service_id="10", subcategory_id="101"), rounds=2)

        with self.assertRaises(ToolRejection) as ctx:
            await tools.post_public_question.coroutine(question="one more?", runtime=runtime)

        self.assertIn("finish_handoff", str(ctx.exception))
        runtime.context.ticket_repo.append_public_log.assert_not_called()
        runtime.context.ticket_repo.append_private_log.assert_not_called()
        runtime.context.state_manager.mark_done.assert_not_called()


class TestFinishHandoff(unittest.IsolatedAsyncioTestCase):
    async def test_note_is_written_and_the_ticket_is_marked_done(self):
        runtime = _make_runtime()

        await tools.finish_handoff.coroutine(note="Printer in room 3 is dead.", runtime=runtime)

        runtime.context.ticket_repo.append_private_log.assert_awaited_once_with(
            runtime.context.ticket, "Printer in room 3 is dead."
        )
        runtime.context.state_manager.mark_done.assert_awaited_once_with("Incident::1")

    async def test_empty_note_is_rejected(self):
        runtime = _make_runtime()

        with self.assertRaises(ToolRejection):
            await tools.finish_handoff.coroutine(note="", runtime=runtime)

        runtime.context.ticket_repo.append_private_log.assert_not_called()
        runtime.context.state_manager.mark_done.assert_not_called()


if __name__ == "__main__":
    unittest.main()
