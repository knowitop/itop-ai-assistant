"""The agent loop without a real LLM.

`create_agent` calls `bind_tools`, which `BaseChatModel` leaves as
`NotImplementedError` — so the ready-made fakes in langchain-core do not fit
and the loop is driven by a scripted model of our own.
"""

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from itop_ai_assistant.agents.intake.pipeline import IntakeRun
from itop_ai_assistant.config import EmbeddingsConfig, IntakeConfig, LlmConfig, VectorConfig
from itop_ai_assistant.domain.catalog import Service, ServiceSubcategory
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.prompt_store import PACKAGED_PROMPTS_DIR, read_prompt_dir
from itop_ai_assistant.state.ticket_state import TicketState
from itop_ai_assistant.vector.store import SearchHit
from itop_ai_assistant.webhook.models import WebhookPayload

_PROMPT_FILES = read_prompt_dir(PACKAGED_PROMPTS_DIR / "intake")


class FakeToolCallingModel(BaseChatModel):
    """Replays a scripted list of AIMessages, one per model call."""

    responses: list[AIMessage] = []
    calls: int = 0
    # kwargs of every bind_tools call — where tool_choice shows up
    bindings: list[dict] = []

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    # Names of the tools handed to the model, per bind_tools call
    bound_tools: list[list[str]] = []

    def bind_tools(self, tools, **kwargs) -> Any:
        self.bindings.append(kwargs)
        self.bound_tools.append([t.name for t in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def ai(tool_calls: list[dict] | None = None, content: str = "", **kwargs) -> AIMessage:
    return AIMessage(content=content, tool_calls=tool_calls or [], **kwargs)


def call(name: str, args: dict, call_id: str = "1") -> dict:
    return {"name": name, "args": args, "id": call_id}


class IntakeAgentTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ticket = Ticket(
            obj_class="Incident",
            id="123",
            org_id="42",
            status="new",
            title="Printer is dead",
            description="Cannot print",
            caller_name="John Doe",
        )
        self.intake_cfg = IntakeConfig()

        # Stateful on purpose: the epilogue re-reads the state to see whether
        # a tool already finished the ticket, so writes must be visible
        self.ticket_state = TicketState()

        async def mark_done(_ref):
            self.ticket_state.ai_done = True

        async def increment_rounds(_ref):
            self.ticket_state.rounds += 1

        async def increment_classify_rounds(_ref):
            self.ticket_state.classify_rounds += 1

        self.deps = MagicMock()
        self.deps.journal = AsyncMock()
        self.deps.state_manager.get = AsyncMock(side_effect=lambda _ref: self.ticket_state)
        self.deps.state_manager.mark_done = AsyncMock(side_effect=mark_done)
        self.deps.state_manager.increment_rounds = AsyncMock(side_effect=increment_rounds)
        self.deps.state_manager.increment_classify_rounds = AsyncMock(side_effect=increment_classify_rounds)
        self.deps.prompt_store.get = AsyncMock(return_value=_PROMPT_FILES)

        self.llm_cfg = LlmConfig(base_url="http://x", model="m")
        self.vector_cfg = VectorConfig()
        self.embeddings_cfg = EmbeddingsConfig()
        # Default deployment for these tests: no vector store, hence no
        # similar-ticket tool. The wiring of the tool has its own case below.
        self.deps.vector_store.configured = False

        async def config_get(module, model):
            return {
                "intake": self.intake_cfg,
                "llm": self.llm_cfg,
                "vector": self.vector_cfg,
                "embeddings": self.embeddings_cfg,
            }[module]

        self.deps.config_store.get = AsyncMock(side_effect=config_get)

        self.bundle = MagicMock()
        self.bundle.ticket_repo.set_fields = AsyncMock()
        self.bundle.ticket_repo.append_public_log = AsyncMock()
        self.bundle.ticket_repo.append_private_log = AsyncMock()
        self.bundle.catalog_repo.find_services = AsyncMock(
            return_value=[Service(id="10", name="Printing", description="Printer issues")]
        )
        self.bundle.catalog_repo.find_subcategories = AsyncMock(
            return_value=[ServiceSubcategory(id="101", name="Hardware fault", description="Model", service_id="10")]
        )
        self.bundle.catalog_repo.get_service = AsyncMock(return_value=None)
        self.bundle.catalog_repo.get_subcategory = AsyncMock(return_value=None)

    def intake_run(self) -> IntakeRun:
        """A run whose body is called directly — `execute()` is the shell's job
        and is covered in `test_intake_pipeline.py`, so `bundle` is set by hand."""
        payload = WebhookPayload.model_validate({"id": "123", "class": "Incident", "event": "created"})
        run = IntakeRun(payload, RunContext(processing_id=uuid4(), module="intake"), self.deps)
        run.bundle = self.bundle
        return run

    async def run_agent(self, responses: list[AIMessage]) -> FakeToolCallingModel:
        model = FakeToolCallingModel(responses=responses)
        with patch("itop_ai_assistant.agents.intake.pipeline.create_llm", return_value=model):
            await self.intake_run().body(self.ticket, "ai-assistant")
        return model

    def journal_steps(self) -> list[tuple[str, str]]:
        return [(c.args[1], c.args[2]) for c in self.deps.journal.add_step.await_args_list]


class TestReadOnlyLoop(IntakeAgentTestCase):
    async def test_plain_text_answer_triggers_the_epilogue(self):
        await self.run_agent([ai(content="I think this is a printer problem.")])

        self.bundle.ticket_repo.append_private_log.assert_awaited_once()
        self.assertEqual(
            self.bundle.ticket_repo.append_private_log.await_args.args[1],
            self.intake_cfg.handoff_fallback_note,
        )
        self.deps.state_manager.mark_done.assert_awaited_once_with("Incident::123")

    async def test_tool_results_and_model_turns_are_journalled(self):
        await self.run_agent(
            [
                ai([call("get_subcategories", {"service_id": 10})]),
                ai(content="done thinking"),
            ]
        )

        steps = self.journal_steps()
        self.assertIn(("agent", "calls: get_subcategories({'service_id': 10})"), steps)
        tool_steps = [s for s in steps if s[0] == "tool:get_subcategories"]
        self.assertEqual(len(tool_steps), 1)
        self.assertIn("[success]", tool_steps[0][1])
        self.assertIn("Hardware fault", tool_steps[0][1])
        self.assertEqual(steps[-2][0], "epilogue")  # usage is always last

    async def test_rejected_call_comes_back_as_feedback_and_the_loop_continues(self):
        self.bundle.catalog_repo.find_subcategories = AsyncMock(return_value=[])

        model = await self.run_agent(
            [
                ai([call("get_subcategories", {"service_id": 99})]),
                ai([call("finish_handoff", {"note": "Cannot classify."}, "h1")]),
            ]
        )

        self.assertEqual(model.calls, 2)
        tool_step = next(s for s in self.journal_steps() if s[0].startswith("tool:"))
        self.assertIn("[error]", tool_step[1])
        self.assertIn("different service", tool_step[1])

    async def test_invalid_arguments_come_back_as_a_tool_error(self):
        model = await self.run_agent(
            [
                ai([call("get_subcategories", {"wrong_arg": 1})]),
                ai([call("finish_handoff", {"note": "Handing over."}, "h1")]),
            ]
        )

        self.assertEqual(model.calls, 2)
        tool_step = next(s for s in self.journal_steps() if s[0].startswith("tool:"))
        self.assertIn("[error]", tool_step[1])

    async def test_model_call_limit_stops_the_loop_and_the_epilogue_closes_it(self):
        self.intake_cfg = IntakeConfig(max_iterations=3)

        model = await self.run_agent([ai([call("get_service_catalog", {})])])

        self.assertEqual(model.calls, 3)
        self.deps.state_manager.mark_done.assert_awaited_once_with("Incident::123")

    async def test_usage_is_the_last_journal_step(self):
        await self.run_agent(
            [
                ai(
                    [call("get_service_catalog", {})],
                    usage_metadata={"input_tokens": 1200, "output_tokens": 40, "total_tokens": 1240},
                ),
                ai(
                    [call("finish_handoff", {"note": "Done."}, "h1")],
                    usage_metadata={"input_tokens": 1500, "output_tokens": 60, "total_tokens": 1560},
                ),
            ]
        )

        node, detail = self.journal_steps()[-1]
        self.assertEqual(node, "usage")
        self.assertIn("model calls: 2", detail)
        self.assertIn("tokens in/out: 2700/100", detail)
        self.assertIn("wall time:", detail)

    async def test_usage_is_recorded_even_when_the_run_fails(self):
        self.bundle.catalog_repo.find_subcategories = AsyncMock(side_effect=RuntimeError("iTop is down"))

        with self.assertRaises(RuntimeError):
            await self.run_agent([ai([call("get_subcategories", {"service_id": 10})])])

        self.assertEqual(self.journal_steps()[-1][0], "usage")

    async def test_itop_failure_propagates(self):
        self.bundle.catalog_repo.find_subcategories = AsyncMock(side_effect=RuntimeError("iTop is down"))

        with self.assertRaises(RuntimeError):
            await self.run_agent([ai([call("get_subcategories", {"service_id": 10})])])

        self.deps.state_manager.mark_done.assert_not_called()

    async def test_epilogue_skipped_when_a_tool_already_finished_the_ticket(self):
        self.ticket_state = TicketState(ai_done=True)

        await self.run_agent([ai(content="nothing to do")])

        self.bundle.ticket_repo.append_private_log.assert_not_called()
        self.deps.state_manager.mark_done.assert_not_called()
        self.assertEqual(self.journal_steps()[-2], ("epilogue", "ticket already finished — nothing to close"))


class TestProseInsteadOfToolCall(IntakeAgentTestCase):
    """Observed in production: on round 2 the model answers the requester in
    prose instead of calling `post_public_question`."""

    async def test_prose_turn_is_retried_and_the_question_still_goes_out(self):
        model = await self.run_agent(
            [
                ai(content="Отлично, спасибо! Какая версия OpenVPN у вас установлена?"),
                ai([call("post_public_question", {"question": "Какая версия OpenVPN у вас установлена?"}, "q1")]),
            ]
        )

        self.assertEqual(model.calls, 2)
        self.bundle.ticket_repo.append_public_log.assert_awaited_once()
        # The round was not wasted and the ticket was not closed behind the requester
        self.deps.state_manager.mark_done.assert_not_called()
        self.bundle.ticket_repo.append_private_log.assert_not_called()
        self.assertNotIn("epilogue", [node for node, _ in self.journal_steps()])

    async def test_the_failed_turn_and_the_retry_are_both_journalled(self):
        await self.run_agent(
            [
                ai(content="I think we have everything we need."),
                ai([call("finish_handoff", {"note": "VPN error 868 on a home PC."}, "h1")]),
            ]
        )

        steps = self.journal_steps()
        self.assertTrue(any(node == "agent" and detail.startswith("no tool call:") for node, detail in steps))
        self.assertTrue(any(node == "agent" and "finish_handoff" in detail for node, detail in steps))
        self.assertIn("model calls: 2", steps[-1][1])

    async def test_second_prose_turn_in_a_row_falls_through_to_the_epilogue(self):
        # One retry, not a loop: a model that cannot use tools must not burn
        # the whole budget two calls at a time
        model = await self.run_agent([ai(content="still just talking")])

        self.assertEqual(model.calls, 2)
        self.deps.state_manager.mark_done.assert_awaited_once_with("Incident::123")


class TestForcedToolChoice(IntakeAgentTestCase):
    """`tool_choice="any"` goes out only where the endpoint accepts it."""

    async def test_not_forced_on_an_endpoint_that_does_not_accept_it(self):
        # openai_compatible with no explicit answer — could be DeepSeek
        model = await self.run_agent([ai([call("finish_handoff", {"note": "n"}, "h1")])])

        # create_agent always passes the kwarg; unset means None
        self.assertTrue(model.bindings)
        self.assertTrue(all(binding.get("tool_choice") is None for binding in model.bindings))

    async def test_forced_when_the_endpoint_accepts_it(self):
        self.llm_cfg = LlmConfig(base_url="http://x", model="m", supports_forced_tool_choice=True)

        model = await self.run_agent([ai([call("finish_handoff", {"note": "n"}, "h1")])])

        self.assertTrue(model.bindings)
        self.assertTrue(all(binding.get("tool_choice") == "any" for binding in model.bindings))

    async def test_prose_is_still_retried_when_the_endpoint_drops_the_field(self):
        # Ollama-shaped failure: the forced choice is accepted and ignored, so
        # the model answers in prose anyway and only the retry catches it
        self.llm_cfg = LlmConfig(base_url="http://x", model="m", supports_forced_tool_choice=True)

        model = await self.run_agent(
            [
                ai(content="Which OpenVPN version do you have?"),
                ai([call("post_public_question", {"question": "Which OpenVPN version do you have?"}, "q1")]),
            ]
        )

        self.assertEqual(model.calls, 2)
        self.bundle.ticket_repo.append_public_log.assert_awaited_once()


class TestClassifiedTicket(IntakeAgentTestCase):
    """Round 2+: the ticket is already classified, so the run is about the
    ticket's completeness only."""

    def setUp(self):
        super().setUp()
        self.ticket.service_id = "8"
        self.ticket.subcategory_id = "43"

    async def test_classification_is_not_redone(self):
        model = await self.run_agent([ai([call("finish_handoff", {"note": "VPN error 868."}, "h1")])])

        self.assertEqual(model.calls, 1)
        self.bundle.catalog_repo.find_services.assert_not_called()
        self.bundle.catalog_repo.find_subcategories.assert_not_called()
        self.bundle.ticket_repo.set_fields.assert_not_called()

    async def test_the_model_is_not_even_offered_the_classification_tools(self):
        captured: list[list] = []

        class Recording(FakeToolCallingModel):
            def bind_tools(self, tools, **kwargs):
                captured.append([t.name for t in tools])
                return self

        model = Recording(responses=[ai([call("finish_handoff", {"note": "n"}, "h1")])])
        with patch("itop_ai_assistant.agents.intake.pipeline.create_llm", return_value=model):
            await self.intake_run().body(self.ticket, "ai-assistant")

        self.assertEqual(captured[0], ["post_public_question", "finish_handoff"])


class TestTerminalTools(IntakeAgentTestCase):
    async def test_happy_path_classify_then_hand_off(self):
        model = await self.run_agent(
            [
                ai([call("set_classification", {"service_id": 10, "subcategory_id": 101}, "c1")]),
                ai([call("finish_handoff", {"note": "Printer in room 3 is dead."}, "c2")]),
                ai(content="MUST NOT BE REACHED"),
            ]
        )

        self.assertEqual(model.calls, 2, "the run must end without a closing model call")
        self.bundle.ticket_repo.set_fields.assert_awaited_once()
        self.bundle.ticket_repo.append_private_log.assert_awaited_once_with(self.ticket, "Printer in room 3 is dead.")
        self.deps.state_manager.mark_done.assert_awaited_once_with("Incident::123")
        self.assertNotIn("epilogue", [node for node, _ in self.journal_steps()])

    async def test_public_question_ends_the_run_without_marking_done(self):
        model = await self.run_agent(
            [
                ai([call("post_public_question", {"question": "What exactly stopped working?"}, "q1")]),
                ai(content="MUST NOT BE REACHED"),
            ]
        )

        self.assertEqual(model.calls, 1)
        self.bundle.ticket_repo.append_public_log.assert_awaited_once()
        self.deps.state_manager.increment_classify_rounds.assert_awaited_once_with("Incident::123")
        self.deps.state_manager.mark_done.assert_not_called()
        self.bundle.ticket_repo.append_private_log.assert_not_called()

    async def test_invalid_service_id_is_fed_back_and_the_agent_recovers(self):
        model = await self.run_agent(
            [
                ai([call("set_classification", {"service_id": 999, "subcategory_id": 101}, "c1")]),
                ai([call("set_classification", {"service_id": 10, "subcategory_id": 101}, "c2")]),
                ai([call("finish_handoff", {"note": "Printer is dead."}, "c3")]),
            ]
        )

        self.assertEqual(model.calls, 3)
        steps = self.journal_steps()
        rejection = next(detail for node, detail in steps if node == "tool:set_classification")
        self.assertIn("[error]", rejection)
        self.assertIn("Valid service IDs: 10", rejection)
        self.bundle.ticket_repo.set_fields.assert_awaited_once()
        self.deps.state_manager.mark_done.assert_awaited_once()

    async def test_exhausted_classify_budget_closes_the_ticket_inside_the_tool(self):
        self.ticket_state = TicketState(classify_rounds=2)

        model = await self.run_agent(
            [
                ai([call("post_public_question", {"question": "again?"}, "q1")]),
                # The model must never get this turn: writing a handoff note
                # here would overwrite the fallback the tool just posted
                ai([call("finish_handoff", {"note": "sneaking a second note in"}, "h1")]),
            ]
        )

        self.assertEqual(model.calls, 1)
        self.bundle.ticket_repo.append_public_log.assert_not_called()
        self.bundle.ticket_repo.append_private_log.assert_awaited_once_with(
            self.ticket, self.intake_cfg.classify_fallback_note
        )
        self.deps.state_manager.mark_done.assert_awaited_once_with("Incident::123")
        self.assertNotIn("epilogue", [node for node, _ in self.journal_steps()])


class TestSimilarTicketsWiring(IntakeAgentTestCase):
    """Whether the run can search at all is decided here, once, by the shell —
    the tool is either given or withheld, never given and then apologetic."""

    def enable_vectors(self) -> MagicMock:
        self.deps.vector_store.configured = True
        self.deps.vector_store.search = AsyncMock(return_value=[SearchHit("UserRequest", 12, 0.9)])
        self.bundle.ticket_repo.find_existing_ids = AsyncMock(return_value={12})
        self.vector_cfg = VectorConfig(enabled=True)
        self.embeddings_cfg = EmbeddingsConfig(base_url="http://emb/v1", model="e5")
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[[1.0, 0.0]])
        embedder.aclose = AsyncMock()
        patcher = patch("itop_ai_assistant.agents.intake.pipeline.EmbeddingsClient", return_value=embedder)
        patcher.start()
        self.addCleanup(patcher.stop)
        return embedder

    async def test_the_tool_is_given_when_everything_is_configured(self):
        self.enable_vectors()

        model = await self.run_agent([ai([call("finish_handoff", {"note": "Printer is dead."})])])

        self.assertIn("find_similar_resolved_tickets", model.bound_tools[0])

    async def test_no_vector_store_no_tool(self):
        model = await self.run_agent([ai([call("finish_handoff", {"note": "Printer is dead."})])])

        self.assertNotIn("find_similar_resolved_tickets", model.bound_tools[0])

    async def test_indexing_switched_off_means_no_tool(self):
        # The index is there but nobody refreshes it — quoting it as current
        # is worse than quoting nothing
        self.enable_vectors()
        self.vector_cfg = VectorConfig(enabled=False)

        model = await self.run_agent([ai([call("finish_handoff", {"note": "Printer is dead."})])])

        self.assertNotIn("find_similar_resolved_tickets", model.bound_tools[0])

    async def test_no_embeddings_endpoint_means_no_tool(self):
        self.enable_vectors()
        self.embeddings_cfg = EmbeddingsConfig()

        model = await self.run_agent([ai([call("finish_handoff", {"note": "Printer is dead."})])])

        self.assertNotIn("find_similar_resolved_tickets", model.bound_tools[0])

    async def test_a_call_reaches_the_index_and_the_source(self):
        self.enable_vectors()

        await self.run_agent(
            [
                ai([call("find_similar_resolved_tickets", {}, "s1")]),
                ai([call("finish_handoff", {"note": "Printer is dead.\n[[UserRequest:12]]"}, "h1")]),
            ]
        )

        self.deps.vector_store.search.assert_awaited_once()
        self.bundle.ticket_repo.find_existing_ids.assert_awaited_once_with("UserRequest", [12])
        self.bundle.ticket_repo.append_private_log.assert_awaited_once_with(
            self.ticket, "Printer is dead.\n[[UserRequest:12]]"
        )
        # TASK-014: the tool's artifact (never sent to the model) reaches the
        # journal via AgentRun._journal_update, appended to the usual detail
        tool_step = next(d for n, d in self.journal_steps() if n == "tool:find_similar_resolved_tickets")
        self.assertIn("[success]", tool_step)
        self.assertIn("requested=", tool_step)
        self.assertIn("found=1", tool_step)
        self.assertIn("kept=1", tool_step)
        self.assertIn("dropped_by_resolve=0", tool_step)
        self.assertIn("scores=[0.9]", tool_step)

    async def test_the_embeddings_client_is_closed_even_when_the_run_fails(self):
        embedder = self.enable_vectors()
        self.bundle.ticket_repo.append_private_log = AsyncMock(side_effect=RuntimeError("iTop is down"))

        with self.assertRaises(RuntimeError):
            await self.run_agent([ai([call("finish_handoff", {"note": "Printer is dead."})])])

        embedder.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
