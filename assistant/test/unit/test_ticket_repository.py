import unittest
from unittest.mock import AsyncMock, MagicMock

from config import TicketMappingConfig
from domain.ticket import Ticket
from itop.repository import TicketRepository

_RAW_TICKET = {
    "id": "42",
    "ref": "R-000042",
    "title": "Printer broken",
    "description": "<p>Not printing.</p>",
    "status": "new",
    "service_id": "5",
    "servicesubcategory_id": "3",
    "caller_id_friendlyname": "John Doe",
    "org_id": "7",
    "request_type": "incident",
    "public_log": {"entries": [{"user_login": "John Doe", "message": "Help!"}]},
}


def _make_repo(mapping: TicketMappingConfig | None = None) -> tuple[TicketRepository, MagicMock]:
    schema = MagicMock()
    schema.find_one = AsyncMock()
    schema.update = AsyncMock()
    itop = MagicMock()
    itop.schema = MagicMock(return_value=schema)
    return TicketRepository(itop, mapping or TicketMappingConfig()), schema


class TestToTicket(unittest.TestCase):
    def test_maps_default_attributes(self):
        repo, _ = _make_repo()

        ticket = repo.to_ticket("UserRequest", _RAW_TICKET)

        self.assertEqual(ticket.label, "UserRequest::42")
        self.assertEqual(ticket.title, "Printer broken")
        self.assertEqual(ticket.status, "new")
        self.assertEqual(ticket.service_id, "5")
        self.assertEqual(ticket.subcategory_id, "3")
        self.assertEqual(ticket.caller_name, "John Doe")
        self.assertEqual(ticket.request_type, "incident")
        self.assertEqual(len(ticket.public_log), 1)
        self.assertEqual(ticket.public_log[0].user_login, "John Doe")

    def test_incident_has_no_request_type_by_default(self):
        repo, _ = _make_repo()
        raw = {k: v for k, v in _RAW_TICKET.items() if k != "request_type"}

        ticket = repo.to_ticket("Incident", raw)

        self.assertIsNone(ticket.request_type)

    def test_custom_field_mapping(self):
        mapping = TicketMappingConfig(fields={"title": "custom_title"})
        repo, _ = _make_repo(mapping)
        raw = {**_RAW_TICKET, "custom_title": "Custom!"}

        ticket = repo.to_ticket("UserRequest", raw)

        self.assertEqual(ticket.title, "Custom!")
        # Non-overridden fields keep their defaults
        self.assertEqual(ticket.caller_name, "John Doe")

    def test_unset_service_id_normalized(self):
        repo, _ = _make_repo()
        raw = {**_RAW_TICKET, "service_id": "0", "servicesubcategory_id": "0"}

        ticket = repo.to_ticket("UserRequest", raw)

        self.assertFalse(ticket.has_service)
        self.assertFalse(ticket.has_subcategory)


class TestFetch(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_returns_ticket(self):
        repo, schema = _make_repo()
        schema.find_one.return_value = _RAW_TICKET

        ticket = await repo.fetch("UserRequest", "42")

        self.assertIsInstance(ticket, Ticket)
        self.assertEqual(ticket.id, "42")

    async def test_fetch_returns_none_when_missing(self):
        repo, schema = _make_repo()
        schema.find_one.return_value = None

        self.assertIsNone(await repo.fetch("UserRequest", "42"))


class TestSetFields(unittest.IsolatedAsyncioTestCase):
    async def test_translates_semantic_names(self):
        repo, schema = _make_repo()
        ticket = Ticket(obj_class="UserRequest", id="42")

        await repo.set_fields(ticket, {"service_id": "10", "subcategory_id": "101"})

        schema.update.assert_awaited_once_with({"id": "42"}, {"service_id": "10", "servicesubcategory_id": "101"})

    async def test_unmapped_field_skipped(self):
        repo, schema = _make_repo()
        ticket = Ticket(obj_class="Incident", id="42")

        await repo.set_fields(ticket, {"request_type": "incident", "service_id": "10"})

        schema.update.assert_awaited_once_with({"id": "42"}, {"service_id": "10"})

    async def test_no_mapped_fields_no_update(self):
        repo, schema = _make_repo()
        ticket = Ticket(obj_class="Incident", id="42")

        await repo.set_fields(ticket, {"request_type": "incident"})

        schema.update.assert_not_called()


class TestAppendLogs(unittest.IsolatedAsyncioTestCase):
    async def test_public_log_payload_shape(self):
        repo, schema = _make_repo()
        ticket = Ticket(obj_class="UserRequest", id="42")

        await repo.append_public_log(ticket, "A question")

        schema.update.assert_awaited_once_with(
            {"id": "42"},
            {"public_log": {"add_item": {"message": "A question", "format": "text"}}},
        )

    async def test_private_log_payload_shape(self):
        repo, schema = _make_repo()
        ticket = Ticket(obj_class="UserRequest", id="42")

        await repo.append_private_log(ticket, "A note")

        schema.update.assert_awaited_once_with(
            {"id": "42"},
            {"private_log": {"add_item": {"message": "A note", "format": "text"}}},
        )

    async def test_custom_log_attribute(self):
        mapping = TicketMappingConfig(fields={"public_log": "user_log"})
        repo, schema = _make_repo(mapping)
        ticket = Ticket(obj_class="UserRequest", id="42")

        await repo.append_public_log(ticket, "Hi")

        raw_fields = schema.update.await_args.args[1]
        self.assertIn("user_log", raw_fields)


class TestAiPersonCache(unittest.IsolatedAsyncioTestCase):
    async def test_ai_person_fetched_once(self):
        repo, schema = _make_repo()
        schema.find_one.return_value = {"friendlyname": "ai-assistant"}

        name1 = await repo.get_ai_person_name()
        name2 = await repo.get_ai_person_name()

        self.assertEqual(name1, "ai-assistant")
        self.assertEqual(name2, "ai-assistant")
        schema.find_one.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
