"""The dry run, asserted where the ban is enforced (REQ-006).

Not at the module's level: what is promised to the customer is "nothing
reaches iTop", and a module-level assertion would only prove that *this*
module remembered to ask. So the run below is driven through the real
`ItopRepositories.for_principal` → `ItopConnection` → `Itop`, and what is
counted is the operations that reached the HTTP transport.

Only the construction of the client is patched, because `create_itop_client`
takes no transport; everything the ban passes through on the way down is the
real thing.
"""

import json
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs
from uuid import uuid4

import fakeredis
import httpx

from itop_ai_assistant.agents.intake.config import IntakeConfig
from itop_ai_assistant.agents.intake.prompts import MODULE
from itop_ai_assistant.agents.intake.prompts import PROMPTS_DIR as INTAKE_PROMPTS_DIR
from itop_ai_assistant.agents.intake.run import IntakeRun
from itop_ai_assistant.agents.intake.state import TicketState
from itop_ai_assistant.config import (
    EmbeddingsConfig,
    FaqMappingConfig,
    ItopConfig,
    LlmConfig,
    PlatformConfig,
    TicketMappingConfig,
)
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.itop.connection import ItopConnection
from itop_ai_assistant.itop.write_policy import WritePolicy
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.repositories.sets import ItopRepositories
from itop_ai_assistant.settings.prompt_store import PromptSet, read_prompt_dir
from itop_ai_assistant.state.counters import Counter, DailyCounters
from itop_ai_assistant.vector import VectorConfig
from itop_ai_assistant.webhook.models import WebhookPayload

from .agents.intake.test_intake_agent import FakeToolCallingModel, ai, call

_PROMPT_FILES = read_prompt_dir(INTAKE_PROMPTS_DIR)
_ITOP_URL = "http://mock-itop/webservices/rest.php"

_SERVICE = {"name": "Printing", "description": "Printer issues"}
_SUBCATEGORY = {"name": "Hardware fault", "description": "State the model", "service_id": "5"}


class RecordingTransport(httpx.AsyncBaseTransport):
    """Answers the catalog reads and records every operation it is handed."""

    def __init__(self) -> None:
        self.operations: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(parse_qs(request.content.decode())["json_data"][0])
        self.operations.append(payload.get("operation", ""))
        objects = {
            "Service": {"Service::5": {"key": "5", "fields": _SERVICE}},
            "ServiceSubcategory": {"ServiceSubcategory::3": {"key": "3", "fields": _SUBCATEGORY}},
        }.get(payload.get("class"))
        return httpx.Response(200, json={"code": 0, "objects": objects})


class FakeConfigStore:
    def __init__(self, dry_run: bool):
        self.sections: dict = {
            "itop": ItopConfig(url=_ITOP_URL, token="tok"),
            "ticket_mapping": TicketMappingConfig(),
            "faq_mapping": FaqMappingConfig(),
            "platform": PlatformConfig(dry_run=dry_run),
            MODULE: IntakeConfig(),
            "llm": LlmConfig(base_url="http://llm/v1", model="m"),
            "vector": VectorConfig(),
            "embeddings": EmbeddingsConfig(),
        }

    async def get(self, module, model):
        return self.sections[module]


class TestIntakeWritesNothingOnADryRun(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = RecordingTransport()
        self.store = FakeConfigStore(dry_run=True)
        self.counters = DailyCounters(fakeredis.aioredis.FakeRedis(decode_responses=True))
        self.connection = ItopConnection(self.store)
        self.repositories = ItopRepositories(self.connection, self.store, WritePolicy(self.store), self.counters)

        self.ticket = Ticket(
            obj_class="Incident",
            id="123",
            org_id="42",
            status="new",
            title="Printer is dead",
            description="Cannot print",
            caller_name="John Doe",
        )
        self.ticket_state = TicketState()
        self.deps = MagicMock()
        self.deps.journal = AsyncMock()
        self.deps.state_manager.get = AsyncMock(return_value=self.ticket_state)
        self.deps.state_manager.increment = AsyncMock()
        self.deps.state_manager.set_flag = AsyncMock()
        self.deps.prompt_store.get = AsyncMock(return_value=PromptSet(defaults=_PROMPT_FILES))
        self.deps.config_store.get = AsyncMock(side_effect=self.store.get)
        self.deps.vector_search.available = AsyncMock(return_value=False)

    async def asyncTearDown(self):
        await self.connection.aclose()

    async def _run(self) -> None:
        """One intake run: classify the ticket, then ask the requester."""
        model = FakeToolCallingModel(
            responses=[
                ai([call("set_classification", {"service_id": 5, "subcategory_id": 3})]),
                ai([call("post_public_question", {"question": "Which printer model is it?"}, "q1")]),
            ]
        )
        self.deps.create_llm = MagicMock(return_value=model)
        run = IntakeRun(
            WebhookPayload.model_validate({"id": "123", "class": "Incident", "event": "created"}),
            RunContext(processing_id=uuid4(), module=MODULE, dry_run=True),
            self.deps,
            lock=self.deps.state_manager,
            itop=self.repositories,
            ai_identity=self.deps.ai_identity,
            journal=self.deps.journal,
        )
        with patch("itop_ai_assistant.itop.connection.create_itop_client", side_effect=self._client):
            run.repos = await self.repositories.for_principal(Principal.service(), comment="run")
            await run.body(self.ticket, "ai-assistant")

    def _client(self, cfg) -> Itop:
        return Itop(url=cfg.url, version=cfg.api_version, auth_token=cfg.token, transport=self.transport)

    def _steps(self) -> list[tuple[str, str]]:
        return [(c.args[1], c.args[2]) for c in self.deps.journal.add_step.await_args_list]

    async def test_a_full_run_sends_iTop_nothing_but_reads(self):
        await self._run()

        # Both writes of this run — the classification and the public question —
        # would have been core/update; nothing but reads left the client.
        self.assertEqual({"core/get"}, set(self.transport.operations))

    async def test_the_dry_run_still_counts_what_it_would_have_done(self):
        """The counters sit above the point the write is dropped, so a dry run
        reports intent (REQ-009 R3). Deliberately: an installation running a
        week in dry run must not read as a dead one, and the mode travels in
        the document beside the counters."""
        await self._run()

        counted = await self.counters.read(datetime.now(UTC).date())

        self.assertEqual(1, counted[Counter.ITOP_FIELD_UPDATE])
        self.assertEqual(1, counted[Counter.ITOP_PUBLIC_COMMENT])

    async def test_the_model_is_not_told_that_its_writes_went_nowhere(self):
        await self._run()

        tools = [detail for node, detail in self._steps() if node.startswith("tool:")]
        self.assertEqual(2, len(tools))
        for detail in tools:
            self.assertIn("[success]", detail)


if __name__ == "__main__":
    unittest.main()
