import unittest
from unittest.mock import AsyncMock, MagicMock

import graph.enrichment.nodes.guard as guard_module
from config import TicketMappingConfig
from domain.ticket import Ticket
from graph.enrichment.state import Action, EnrichmentState
from state.ticket_state import TicketState


def _make_ticket(status: str = "new") -> Ticket:
    return Ticket(obj_class="UserRequest", id="1", ref="R-000001", status=status)


def _make_runtime(ticket_state: TicketState) -> MagicMock:
    runtime = MagicMock()
    runtime.context.state_manager.get = AsyncMock(return_value=ticket_state)
    runtime.context.ticket_mapping = TicketMappingConfig()
    return runtime


class TestGuardAiDone(unittest.IsolatedAsyncioTestCase):
    async def test_ai_done_returns_stop(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime(TicketState(rounds=0, ai_done=True))

        result = await guard_module.run(state, runtime)

        self.assertEqual(result["action"], Action.STOP)

    async def test_not_ai_done_continues(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime(TicketState(rounds=0, ai_done=False))

        result = await guard_module.run(state, runtime)

        self.assertNotIn("action", result)


class TestGuardTicketStatus(unittest.IsolatedAsyncioTestCase):
    async def test_status_new_continues(self):
        state: EnrichmentState = {"ticket": _make_ticket("new"), "action": None, "question": None}
        runtime = _make_runtime(TicketState(rounds=0, ai_done=False))

        result = await guard_module.run(state, runtime)

        self.assertNotIn("action", result)

    async def test_status_assigned_returns_stop(self):
        state: EnrichmentState = {"ticket": _make_ticket("assigned"), "action": None, "question": None}
        runtime = _make_runtime(TicketState(rounds=0, ai_done=False))

        result = await guard_module.run(state, runtime)

        self.assertEqual(result["action"], Action.STOP)

    async def test_status_resolved_returns_stop(self):
        state: EnrichmentState = {"ticket": _make_ticket("resolved"), "action": None, "question": None}
        runtime = _make_runtime(TicketState(rounds=0, ai_done=False))

        result = await guard_module.run(state, runtime)

        self.assertEqual(result["action"], Action.STOP)

    async def test_custom_active_statuses_allow_processing(self):
        state: EnrichmentState = {"ticket": _make_ticket("approved"), "action": None, "question": None}
        runtime = _make_runtime(TicketState(rounds=0, ai_done=False))
        runtime.context.ticket_mapping = TicketMappingConfig(active_statuses=["new", "approved"])

        result = await guard_module.run(state, runtime)

        self.assertNotIn("action", result)


if __name__ == "__main__":
    unittest.main()
