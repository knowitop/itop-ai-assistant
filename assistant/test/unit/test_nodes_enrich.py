import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_openai import ChatOpenAI

import graph.enrichment.nodes.enrich as enrich_module
from config import EnrichmentConfig
from domain.ticket import Ticket
from graph.enrichment.prompts import build_enrichment_prompts
from graph.enrichment.state import EnrichmentState
from prompt_store import read_prompt_dir

_TEST_LLM = ChatOpenAI(model="test-model", api_key="test-key", base_url="http://localhost:9")
_PROMPTS = build_enrichment_prompts(read_prompt_dir(Path(__file__).parents[2] / "prompts" / "enrichment"))


def _make_ticket() -> Ticket:
    return Ticket(
        obj_class="UserRequest",
        id="1",
        ref="R-000001",
        title="Broken laptop",
        description="My laptop does not turn on.",
        caller_name="John Doe",
    )


def _make_runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.context.ticket_repo.append_private_log = AsyncMock()
    runtime.context.ticket_repo.get_ai_person_name = AsyncMock(return_value="AI Assistant")
    runtime.context.state_manager.mark_done = AsyncMock()
    runtime.context.enrichment = EnrichmentConfig()
    runtime.context.prompts = _PROMPTS
    runtime.context.llm_enrich = _TEST_LLM
    return runtime


class TestEnrichEmptyLLMResponse(unittest.IsolatedAsyncioTestCase):
    async def test_none_content_skips_update_but_marks_done(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=MagicMock(content=None))):
            await enrich_module.run(state, runtime)

        runtime.context.ticket_repo.append_private_log.assert_not_called()
        runtime.context.state_manager.mark_done.assert_called_once_with("UserRequest::1")

    async def test_empty_string_content_skips_update_but_marks_done(self):
        state: EnrichmentState = {"ticket": _make_ticket(), "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(ChatOpenAI, "ainvoke", new=AsyncMock(return_value=MagicMock(content=""))):
            await enrich_module.run(state, runtime)

        runtime.context.ticket_repo.append_private_log.assert_not_called()
        runtime.context.state_manager.mark_done.assert_called_once_with("UserRequest::1")

    async def test_normal_content_updates_itop_and_marks_done(self):
        ticket = _make_ticket()
        state: EnrichmentState = {"ticket": ticket, "action": None, "question": None}
        runtime = _make_runtime()

        with patch.object(
            ChatOpenAI,
            "ainvoke",
            new=AsyncMock(return_value=MagicMock(content="Summary: laptop issue.")),
        ):
            await enrich_module.run(state, runtime)

        runtime.context.ticket_repo.append_private_log.assert_awaited_once_with(ticket, "Summary: laptop issue.")
        runtime.context.state_manager.mark_done.assert_called_once_with("UserRequest::1")


if __name__ == "__main__":
    unittest.main()
