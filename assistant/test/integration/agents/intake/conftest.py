# Load .env.test BEFORE any project imports so get_settings() (cached on first
# call) picks up the test LLM endpoint. Searched for upwards rather than by a
# fixed number of parents: this file has moved down the tree before, and a
# stale relative path fails as "no model configured", far from its cause.
from pathlib import Path

from dotenv import load_dotenv

_env_test = next((p / ".env.test" for p in Path(__file__).resolve().parents if (p / ".env.test").is_file()), None)
if _env_test:
    load_dotenv(_env_test, override=False)

import json
import logging
from urllib.parse import parse_qs
from uuid import uuid4

import fakeredis.aioredis
import httpx
import pytest

logger = logging.getLogger(__name__)

from itop_ai_assistant.agents.intake.config import IntakeConfig
from itop_ai_assistant.agents.intake.context import IntakeContext
from itop_ai_assistant.agents.intake.domain import Classification, IntakeScope
from itop_ai_assistant.agents.intake.prompts import PROMPTS_DIR as INTAKE_PROMPTS_DIR
from itop_ai_assistant.agents.intake.prompts import build_intake_prompts
from itop_ai_assistant.agents.intake.state import IntakeState
from itop_ai_assistant.config import get_settings
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.repositories.catalog import CatalogRepository
from itop_ai_assistant.repositories.ticket import TicketRepository
from itop_ai_assistant.settings.prompt_store import read_prompt_dir
from itop_ai_assistant.state.counters import DailyCounters
from itop_ai_assistant.state.ticket_state import TicketStateManager

ITOP_URL = "http://mock-itop/webservices/rest.php"

_PROMPTS = build_intake_prompts(read_prompt_dir(INTAKE_PROMPTS_DIR))

_SERVICE_FIELDS = {"name": "IT Support", "description": "General IT support services"}
# service_id is a mandatory external key in iTop — always present in real responses
_SUBCATEGORY_FIELDS = {"name": "Hardware", "description": "Hardware-related issues", "service_id": "5"}
_SUBCATEGORY_WITH_REQUIREMENTS = {
    "name": "Hardware",
    "description": (
        "Hardware equipment failures and malfunctions. "
        "Required information: device manufacturer and model, "
        "operating system, exact error message or failure symptom."
    ),
    "service_id": "5",
}
_AI_PERSON_FIELDS = {"friendlyname": "ai-assistant", "email": "ai@example.com"}


def _itop_ok(cls: str, key: int | str, fields: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "objects": {f"{cls}::{key}": {"key": str(key), "fields": fields}},
        },
    )


class ItopMockTransport(httpx.AsyncBaseTransport):
    """Intercepts all httpx calls made by the Itop client and returns preset responses."""

    def __init__(self, subcategory_fields: dict | None = None) -> None:
        self.calls: list[dict] = []
        self._subcategory_fields = subcategory_fields or _SUBCATEGORY_FIELDS

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode())
        op = json.loads(body["json_data"][0])
        self.calls.append(op)

        match op.get("operation"), op.get("class"):
            case "core/get", "Service":
                return _itop_ok("Service", 5, _SERVICE_FIELDS)
            case "core/get", "ServiceSubcategory":
                return _itop_ok("ServiceSubcategory", 3, self._subcategory_fields)
            case "core/get", "Person":
                return _itop_ok("Person", 1, _AI_PERSON_FIELDS)
            case "core/update", cls_name:
                fields = op.get("fields", {})
                log_type = (
                    "public_log" if "public_log" in fields else "private_log" if "private_log" in fields else None
                )
                if log_type:
                    message = fields[log_type].get("add_item", {}).get("message", "")
                    logger.info("[iTop %s → %s]\n%s", cls_name, log_type, message)
                return httpx.Response(200, json={"code": 0, "message": "Updated: 1", "objects": None})
        return httpx.Response(200, json={"code": 0, "objects": None})

    def update_calls(self) -> list[dict]:
        """Return only core/update operations (state-changing calls)."""
        return [op for op in self.calls if op.get("operation") == "core/update"]


def make_ctx(
    state_manager: TicketStateManager,
    ticket: Ticket,
    subcategory_fields: dict | None = None,
    scope: IntakeScope | None = None,
) -> tuple[IntakeContext, ItopMockTransport]:
    """Create an IntakeContext with a fresh ItopMockTransport. Returns both for assertions."""
    transport = ItopMockTransport(subcategory_fields=subcategory_fields)
    itop = Itop(url=ITOP_URL, version="1.3", auth_user="dummy", auth_pwd="dummy", transport=transport)
    settings = get_settings()
    intake = settings.module_defaults("intake", IntakeConfig)
    ctx = IntakeContext(
        processing_id=uuid4(),
        principal=Principal.service(),
        ticket=ticket,
        ticket_repo=TicketRepository(
            itop, settings.ticket_mapping, DailyCounters(fakeredis.aioredis.FakeRedis(decode_responses=True))
        ),
        catalog_repo=CatalogRepository(itop),
        state_manager=IntakeState(state_manager),
        intake=intake,
        # No vector store in these tests, so `similar` is off regardless of
        # the switch — the same reduction `compose.assemble` makes
        scope=scope
        or IntakeScope(
            classify=intake.classify_enabled,
            clarify=intake.clarify_enabled,
            handoff_note=intake.handoff_note_enabled,
            similar=False,
        ),
        classification=Classification(unclassified_services=frozenset(intake.unclassified_service_ids)),
        # Matches _AI_PERSON_FIELDS, i.e. what IdentityRepository would return
        ai_name="ai-assistant",
    )
    return ctx, transport


@pytest.fixture
def itop_transport() -> ItopMockTransport:
    return ItopMockTransport()


@pytest.fixture
def itop(itop_transport: ItopMockTransport) -> Itop:
    return Itop(
        url=ITOP_URL,
        version="1.3",
        auth_user="dummy",
        auth_pwd="dummy",
        transport=itop_transport,
    )


@pytest.fixture
async def state_manager() -> TicketStateManager:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return TicketStateManager(redis)


def make_ticket(**overrides: object) -> Ticket:
    base: dict = {
        "obj_class": "UserRequest",
        "id": "42",
        "ref": "R-000042",
        "title": "Printer does not print after Windows update",
        "description": (
            "<p>My HP LaserJet 400 M401dn stopped printing after a Windows update yesterday. "
            "The printer shows as online in Windows, but print jobs disappear from the queue immediately "
            "without printing. I have already tried restarting both the printer and the PC. "
            "This affects all applications. The printer is connected via USB.</p>"
        ),
        "service_id": "5",
        "subcategory_id": "3",
        "status": "new",
        "caller_name": "John Doe",
        "org_id": "1",
        "public_log": [],
    }
    return Ticket(**{**base, **overrides})
