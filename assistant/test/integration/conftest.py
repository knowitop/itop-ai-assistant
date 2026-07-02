# Load .env.test BEFORE any project imports so get_settings() (cached on first
# call) picks up the test LLM endpoint.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env.test", override=False)

import json
import logging
from urllib.parse import parse_qs
from uuid import uuid4

import fakeredis.aioredis
import httpx
import pytest

logger = logging.getLogger(__name__)

from config import get_settings
from deps import create_llm
from graph.enrichment.context import GraphContext
from graph.enrichment.prompts import build_enrichment_prompts
from itop_client import Itop
from prompt_store import read_prompt_dir
from state.ticket_state import TicketStateManager

ITOP_URL = "http://mock-itop/webservices/rest.php"

_PROMPTS = build_enrichment_prompts(read_prompt_dir(Path(__file__).parents[2] / "prompts" / "enrichment"))

_SERVICE_FIELDS = {"name": "IT Support", "description": "General IT support services"}
_SUBCATEGORY_FIELDS = {"name": "Hardware", "description": "Hardware-related issues"}
_SUBCATEGORY_WITH_REQUIREMENTS = {
    "name": "Hardware",
    "description": (
        "Hardware equipment failures and malfunctions. "
        "Required information: device manufacturer and model, "
        "operating system, exact error message or failure symptom."
    ),
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
    state_manager: TicketStateManager, subcategory_fields: dict | None = None
) -> tuple[GraphContext, ItopMockTransport]:
    """Create a GraphContext with a fresh ItopMockTransport. Returns both for assertions."""
    transport = ItopMockTransport(subcategory_fields=subcategory_fields)
    itop = Itop(url=ITOP_URL, version="1.3", auth_user="dummy", auth_pwd="dummy", transport=transport)
    settings = get_settings()
    enrichment = settings.enrichment
    llm = create_llm(settings)
    ctx = GraphContext(
        processing_id=uuid4(),
        itop_client=itop,
        state_manager=state_manager,
        enrichment=enrichment,
        prompts=_PROMPTS,
        llm_classify=llm,
        llm_evaluate=llm,
        llm_enrich=llm,
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


@pytest.fixture
def ctx(itop: Itop, state_manager: TicketStateManager) -> GraphContext:
    settings = get_settings()
    llm = create_llm(settings)
    return GraphContext(
        processing_id=uuid4(),
        itop_client=itop,
        state_manager=state_manager,
        enrichment=settings.enrichment,
        prompts=_PROMPTS,
        llm_classify=llm,
        llm_evaluate=llm,
        llm_enrich=llm,
    )


def make_ticket(**overrides: object) -> dict:
    base: dict = {
        "id": "42",
        "ref": "R-000042",
        "finalclass": "UserRequest",
        "title": "Printer does not print after Windows update",
        "description": (
            "<p>My HP LaserJet 400 M401dn stopped printing after a Windows update yesterday. "
            "The printer shows as online in Windows, but print jobs disappear from the queue immediately "
            "without printing. I have already tried restarting both the printer and the PC. "
            "This affects all applications. The printer is connected via USB.</p>"
        ),
        "service_id": "5",
        "servicesubcategory_id": "3",
        "status": "new",
        "caller_id_friendlyname": "John Doe",
        "public_log": {"entries": []},
    }
    return {**base, **overrides}
