"""The typed ticket over the generic repository — what `intake` reads by name,
and what the installation counts when it writes."""

import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import fakeredis

from itop_ai_assistant.config import TicketMappingConfig
from itop_ai_assistant.domain.object_view import ObjectView
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA
from itop_ai_assistant.repositories.object_repo import ObjectRepository
from itop_ai_assistant.repositories.ticket import TicketRepository, to_ticket
from itop_ai_assistant.state.counters import Counter, DailyCounters

_RAW_TICKET = {
    "id": "42",
    "ref": "R-000042",
    "title": "Printer broken",
    "description": "<p>Not printing.</p>",
    "status": "new",
    "service_id": "5",
    "servicesubcategory_id": "3",
    "service_id_friendlyname": "Printing",
    "servicesubcategory_id_friendlyname": "Hardware",
    "caller_id_friendlyname": "John Doe",
    "org_id": "7",
    "request_type": "incident",
    "public_log": {"entries": [{"user_login": "John Doe", "message": "Help!"}]},
    "private_log": {"entries": [{"user_login": "engineer", "message": "Ordered a replacement part."}]},
    "solution": "<p>Replaced cartridge.</p>",
    "last_update": "2026-07-10 12:00:00",
    "start_date": "2026-07-01 09:30:00",
}


def _make_repo(
    mapping: TicketMappingConfig | None = None, counters: DailyCounters | None = None
) -> tuple[TicketRepository, MagicMock]:
    itop_schema = MagicMock()
    itop_schema.find_one = AsyncMock()
    itop_schema.update = AsyncMock()
    itop = MagicMock()
    itop.schema = MagicMock(return_value=itop_schema)
    counters = counters or DailyCounters(fakeredis.aioredis.FakeRedis(decode_responses=True))
    objects = ObjectRepository(itop, TICKET_SCHEMA, mapping or TicketMappingConfig(), counters)
    return TicketRepository(objects), itop_schema


def _view(**values) -> ObjectView:
    return ObjectView(schema=TICKET_SCHEMA, obj_class="UserRequest", id="42", values=values)


class TestToTicket(unittest.TestCase):
    """Every value arrives normalized, so there is nothing left to decide here
    — and a field the view does not carry falls back to the model's default."""

    def test_the_view_becomes_the_typed_ticket(self):
        ticket = to_ticket(
            _view(
                title="Printer broken",
                status="new",
                service_id="5",
                caller_name="John Doe",
                last_update=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
            )
        )

        self.assertIsInstance(ticket, Ticket)
        self.assertEqual("UserRequest::42", str(ticket.identity))
        self.assertEqual("Printer broken", ticket.title)
        self.assertEqual("5", ticket.service_id)
        self.assertEqual(datetime(2026, 7, 10, 12, 0, tzinfo=UTC), ticket.last_update)

    def test_an_absent_field_falls_back_to_the_models_own_default(self):
        ticket = to_ticket(_view(title="Printer broken"))

        self.assertEqual("", ticket.description)
        self.assertIsNone(ticket.request_type)
        self.assertIsNone(ticket.last_update)
        self.assertEqual([], ticket.public_log)

    def test_every_ticket_field_is_a_field_of_the_family(self):
        # The typed model is a view over the schema, not a second declaration:
        # a name only one of them knows would read as a default forever.
        declared = {spec.name for spec in TICKET_SCHEMA.fields}
        self.assertEqual(set(), set(Ticket.model_fields) - declared - {"obj_class", "id"})


class TestFetch(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_typed_ticket(self):
        repo, itop = _make_repo()
        itop.find_one.return_value = _RAW_TICKET

        ticket = await repo.fetch("UserRequest", "42")

        self.assertIsInstance(ticket, Ticket)
        self.assertEqual("42", ticket.id)
        self.assertEqual("John Doe", ticket.caller_name)

    async def test_returns_none_when_missing(self):
        repo, itop = _make_repo()
        itop.find_one.return_value = None

        self.assertIsNone(await repo.fetch("UserRequest", "42"))

    async def test_never_asks_itop_for_the_private_log(self):
        repo, itop = _make_repo()
        itop.find_one.return_value = _RAW_TICKET

        await repo.fetch("UserRequest", "42")

        projection = itop.find_one.await_args.kwargs["projection"]
        self.assertIn("public_log", projection)
        self.assertNotIn("private_log", projection)


class TestWritesAreCounted(unittest.IsolatedAsyncioTestCase):
    """Counted where the writes physically pass, not where they were meant:
    a rule every new module has to remember is one the first forgetful module
    breaks, and it breaks as "that customer somehow asks no questions"
    (REQ-009 R3). Which counter a log append belongs to is this class's word —
    a question to the requester and a note between engineers are the ticket
    family's distinction.
    """

    async def asyncSetUp(self):
        self.counters = DailyCounters(fakeredis.aioredis.FakeRedis(decode_responses=True))
        self.repo, self.itop = _make_repo(counters=self.counters)
        self.ticket = Ticket(obj_class="UserRequest", id="42")

    async def test_each_kind_of_write_has_its_own_counter(self):
        await self.repo.append_public_log(self.ticket, "A question")
        await self.repo.append_private_log(self.ticket, "A note")
        await self.repo.set_fields(self.ticket, {"service_id": "10"})

        counted = await self.counters.read(datetime.now(UTC).date())

        self.assertEqual(1, counted[Counter.ITOP_PUBLIC_COMMENT])
        self.assertEqual(1, counted[Counter.ITOP_PRIVATE_NOTE])
        self.assertEqual(1, counted[Counter.ITOP_FIELD_UPDATE])

    async def test_a_log_goes_to_the_attribute_the_deployment_maps(self):
        repo, itop = _make_repo(TicketMappingConfig(fields={"public_log": "user_log"}))

        await repo.append_public_log(self.ticket, "Hi")

        self.assertIn("user_log", itop.update.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
