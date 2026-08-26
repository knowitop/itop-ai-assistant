"""
Integration tests for the intake agent against a real model.

- iTop HTTP calls are intercepted by ItopMockTransport (no real iTop needed).
- LLM calls are REAL — requires a running LLM at LLM_BASE_URL (set in .env.test).
- Redis is faked via fakeredis.

These are the only tests that exercise the prompts and the model's tool-calling
end to end: the unit suite asserts everything against a scripted fake model.
The guard is not covered here — it is plain code in `IntakeRun.stop_reason`,
called by the core run shell and unit-tested there.

Run: uv run pytest test/integration/ -v
"""

from itop_ai_assistant.agents.intake.agent import build_intake_agent
from itop_ai_assistant.agents.intake.domain import IntakeScope
from itop_ai_assistant.agents.intake.prompt import build_initial_messages
from itop_ai_assistant.agents.intake.tools import tools_for
from itop_ai_assistant.config import get_settings
from itop_ai_assistant.core.deps import create_llm

from .conftest import _PROMPTS, _SUBCATEGORY_WITH_REQUIREMENTS, ItopMockTransport, make_ctx, make_ticket


async def _run(ctx):
    llm_cfg = get_settings().llm
    agent = build_intake_agent(
        create_llm(llm_cfg),
        ctx.intake,
        tools_for(ctx.ticket, ctx.scope, ctx.classification),
        force_tool_choice=llm_cfg.endpoint_forces_tool_choice,
    )
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
        assert state.questions_asked == 1
        assert _log_updates(transport, "public_log")


class TestReducedScope:
    async def test_note_only_deployment_writes_nothing_but_the_note(self, state_manager):
        """Only the handoff note is switched on → no question, no classification."""
        ticket = make_ticket(
            title="HP LaserJet 400 M401dn not printing after Windows 11 update",
            description="<p>Error 'Driver unavailable' since yesterday. Already restarted printer and PC.</p>",
            service_id=None,
            subcategory_id=None,
        )
        scope = IntakeScope(classify=False, clarify=False, handoff_note=True, similar=False)
        ctx, transport = make_ctx(state_manager, ticket, _SUBCATEGORY_WITH_REQUIREMENTS, scope=scope)

        await _run(ctx)

        state = await ctx.state_manager.get("UserRequest::42")
        assert state.ai_done is True
        assert _log_updates(transport, "private_log")
        assert not _log_updates(transport, "public_log")
        assert not [op for op in transport.update_calls() if "service_id" in op.get("fields", {})]


class TestClassification:
    async def test_unclassified_ticket_gets_a_service(self, state_manager):
        """No service → the agent reads the catalog and classifies the ticket."""
        # None, not iTop's "0": unset external keys are normalized away by
        # `TicketRepository`, and the domain model never sees them
        ticket = make_ticket(service_id=None, subcategory_id=None)
        ctx, transport = make_ctx(state_manager, ticket, _SUBCATEGORY_WITH_REQUIREMENTS)

        await _run(ctx)

        classification = [op for op in transport.update_calls() if "service_id" in op.get("fields", {})]
        assert classification
