"""
Integration tests for the enrichment LangGraph.

- iTop HTTP calls are intercepted by ItopMockTransport (no real iTop needed).
- LLM calls are REAL — requires a running LLM at LLM_BASE_URL (set in .env.test).
- Redis is faked via fakeredis.

Run: uv run pytest test/integration/ -v
"""

from graph.enrichment.graph import build_graph
from graph.enrichment.state import Action

from .conftest import _SUBCATEGORY_WITH_REQUIREMENTS, make_ctx, make_ticket


async def _run(ctx, ticket):
    graph = build_graph()
    return await graph.ainvoke(
        {"ticket": ticket, "action": None, "question": None},
        context=ctx,
    )


class TestGuardShortCircuits:
    async def test_already_done(self, ctx, itop_transport):
        """Guard stops immediately when ai_done=True — no LLM, no iTop calls."""
        await ctx.state_manager.mark_done("UserRequest::42")

        result = await _run(ctx, make_ticket())

        assert result["action"] == Action.STOP
        assert itop_transport.calls == []

    async def test_ticket_not_new(self, ctx, itop_transport):
        """Guard stops when ticket is already assigned — no LLM, no iTop calls."""
        result = await _run(ctx, make_ticket(status="assigned"))

        assert result["action"] == Action.STOP
        assert itop_transport.calls == []


class TestEnrichWithoutEvaluate:
    async def test_no_service_context_goes_to_enrich(self, ctx, itop_transport):
        """service_id=0 triggers classification, then evaluation and enrichment proceed normally."""
        result = await _run(ctx, make_ticket(service_id="0"))

        assert result["action"] == Action.ENRICH
        state = await ctx.state_manager.get("UserRequest::42")
        assert state.ai_done is True

        updates = itop_transport.update_calls()
        assert len(updates) == 2
        assert "service_id" in updates[0]["fields"]
        assert "private_log" in updates[1]["fields"]

    async def test_rounds_exhausted_goes_to_enrich(self, ctx, itop_transport):
        """When rounds >= 2, evaluate is bypassed and ticket is enriched directly."""
        await ctx.state_manager.increment_rounds("UserRequest::42")
        await ctx.state_manager.increment_rounds("UserRequest::42")

        result = await _run(ctx, make_ticket())

        assert result["action"] == Action.ENRICH
        state = await ctx.state_manager.get("UserRequest::42")
        assert state.ai_done is True

        updates = itop_transport.update_calls()
        assert len(updates) == 1
        assert "private_log" in updates[0]["fields"]


class TestFullEvaluationFlow:
    async def test_complete_ticket_is_enriched(self, state_manager):
        """Ticket with all required fields → LLM returns SUFFICIENT → enrich path."""
        ctx, transport = make_ctx(state_manager, _SUBCATEGORY_WITH_REQUIREMENTS)
        ticket = make_ticket(
            title="HP LaserJet 400 M401dn not printing after Windows 11 update",
            description=(
                "<p>My HP LaserJet 400 M401dn stopped printing after a Windows 11 update "
                "yesterday evening. Error: 'Driver unavailable'. "
                "Already restarted both printer and PC.</p>"
            ),
        )

        result = await _run(ctx, ticket)

        assert result["action"] == Action.ENRICH
        state = await ctx.state_manager.get("UserRequest::42")
        assert state.ai_done is True
        assert "private_log" in transport.update_calls()[0]["fields"]

    async def test_vague_ticket_triggers_question(self, state_manager):
        """Ticket missing required fields → LLM asks a clarifying question → ask path."""
        ctx, transport = make_ctx(state_manager, _SUBCATEGORY_WITH_REQUIREMENTS)
        ticket = make_ticket(
            title="printer broken",
            description="<p>Not printing.</p>",
        )

        result = await _run(ctx, ticket)

        assert result["action"] == Action.ASK
        state = await ctx.state_manager.get("UserRequest::42")
        assert state.rounds == 1
        assert "public_log" in transport.update_calls()[0]["fields"]
        assert result["question"]
