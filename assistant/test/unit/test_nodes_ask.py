import unittest
from unittest.mock import AsyncMock, MagicMock

import graph.enrichment.nodes.ask as ask_module
from domain.ticket import Ticket
from graph.enrichment.state import EnrichmentState


def _make_ticket() -> Ticket:
    return Ticket(obj_class="UserRequest", id="1", ref="R-000001")


def _make_runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.context.ticket_repo.append_public_log = AsyncMock()
    runtime.context.state_manager.increment_rounds = AsyncMock()
    return runtime


class TestAskNode(unittest.IsolatedAsyncioTestCase):
    async def test_posts_question_to_public_log(self):
        question = "What is the serial number of your laptop?"
        ticket = _make_ticket()
        state: EnrichmentState = {"ticket": ticket, "action": None, "question": question}
        runtime = _make_runtime()

        await ask_module.run(state, runtime)

        runtime.context.ticket_repo.append_public_log.assert_awaited_once_with(ticket, question)

    async def test_increments_rounds(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": "Any question?"}
        runtime = _make_runtime()

        await ask_module.run(state, runtime)

        runtime.context.state_manager.increment_rounds.assert_awaited_once_with("UserRequest::1")

    async def test_returns_empty_dict(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": "Any question?"}
        runtime = _make_runtime()

        result = await ask_module.run(state, runtime)

        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
