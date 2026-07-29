"""
Integration tests for the intake agent against a real model.

- iTop HTTP calls are intercepted by ItopMockTransport (no real iTop needed).
- LLM calls are REAL — requires a running LLM at LLM_BASE_URL (set in .env.test).
- Redis is faked via fakeredis.

These are the only tests that exercise the prompts and the model's tool-calling
end to end: the unit suite asserts everything against a scripted fake model.
The guard is not covered here — it is plain code in `pipeline._stop_reason` and
unit-tested there.

Run: uv run pytest test/integration/ -v
"""

from agents.intake.agent import build_intake_agent
from agents.intake.prompt import build_initial_messages
from agents.intake.tools import tools_for
from config import get_settings
from deps import create_llm

from .conftest import _PROMPTS, _SUBCATEGORY_WITH_REQUIREMENTS, ItopMockTransport, make_ctx, make_ticket


async def _run(ctx):
    agent = build_intake_agent(create_llm(get_settings().llm), ctx.intake, tools_for(ctx.ticket))
    messages = await build_initial_messages(ctx, _PROMPTS)
    return await agent.ainvoke({"messages": messages}, context=ctx)


def _log_updates(transport: ItopMockTransport, log_type: str) -> list[dict]:
    return [op for op in transport.update_calls() if log_type in op.get("fields", {})]


class TestFullIntakeFlow:
    async def test_complete_ticket_is_handed_off(self, state_manager):
        """Ticket with all required fields → the agent finishes the handoff."""
        ticket = make_ticket(
            title="HP LaserJet 400 M401dn not printing after Windows 11 update",
            description=(
                "<p>My HP LaserJet 400 M401dn stopped printing after a Windows 11 update "
                "yesterday evening. Error: 'Driver unavailable'. "
                "Already restarted both printer and PC.</p>"
            ),
        )
        ctx, transport = make_ctx(state_manager, ticket, _SUBCATEGORY_WITH_REQUIREMENTS)

        await _run(ctx)

        state = await ctx.state_manager.get("UserRequest::42")
        assert state.ai_done is True
        assert _log_updates(transport, "private_log")
        assert not _log_updates(transport, "public_log")

    async def test_vague_ticket_triggers_question(self, state_manager):
        """Ticket missing required fields → the agent asks one clarifying question."""
        ticket = make_ticket(title="printer broken", description="<p>Not printing.</p>")
        ctx, transport = make_ctx(state_manager, ticket, _SUBCATEGORY_WITH_REQUIREMENTS)

        await _run(ctx)

        state = await ctx.state_manager.get("UserRequest::42")
        assert state.rounds == 1
        assert _log_updates(transport, "public_log")


class TestClassification:
    async def test_unclassified_ticket_gets_a_service(self, state_manager):
        """service_id=0 → the agent reads the catalog and classifies the ticket."""
        ticket = make_ticket(service_id="0", subcategory_id="0")
        ctx, transport = make_ctx(state_manager, ticket, _SUBCATEGORY_WITH_REQUIREMENTS)

        await _run(ctx)

        classification = [op for op in transport.update_calls() if "service_id" in op.get("fields", {})]
        assert classification
