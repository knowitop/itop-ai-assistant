import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from config import TicketMappingConfig
from domain.ticket import Ticket
from ticket_repository import TicketRepository, _parse_dt

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
    "solution": "<p>Replaced cartridge.</p>",
    "last_update": "2026-07-10 12:00:00",
    "start_date": "2026-07-01 09:30:00",
}


def _make_repo(mapping: TicketMappingConfig | None = None) -> tuple[TicketRepository, MagicMock]:
    schema = MagicMock()
    schema.find = AsyncMock()
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

    def test_maps_solution_and_timestamps(self):
        repo, _ = _make_repo()

        ticket = repo.to_ticket("UserRequest", _RAW_TICKET)

        self.assertEqual(ticket.solution, "<p>Replaced cartridge.</p>")
        self.assertEqual(ticket.last_update, datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
        self.assertEqual(ticket.created_at, datetime(2026, 7, 1, 9, 30, tzinfo=UTC))

    def test_missing_timestamps_are_none(self):
        repo, _ = _make_repo()
        raw = {k: v for k, v in _RAW_TICKET.items() if k not in ("last_update", "start_date", "solution")}

        ticket = repo.to_ticket("UserRequest", raw)

        self.assertIsNone(ticket.last_update)
        self.assertIsNone(ticket.created_at)
        self.assertEqual(ticket.solution, "")


class TestParseDt(unittest.TestCase):
    def test_valid_itop_timestamp(self):
        self.assertEqual(_parse_dt("2026-07-10 12:00:00"), datetime(2026, 7, 10, 12, 0, tzinfo=UTC))

    def test_garbage_is_none(self):
        for value in (None, "", "not-a-date", "2026-07-10", 12345, {"a": 1}):
            self.assertIsNone(_parse_dt(value), value)


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

    async def test_fetch_projects_only_mapped_attributes(self):
        repo, schema = _make_repo()
        schema.find_one.return_value = _RAW_TICKET

        await repo.fetch("UserRequest", "42")

        projection = schema.find_one.await_args.kwargs["projection"]
        self.assertIn("id", projection)
        self.assertIn("servicesubcategory_id", projection)
        self.assertIn("public_log", projection)
        self.assertNotIn("private_log", projection)

    async def test_fetch_projection_respects_class_overrides(self):
        repo, schema = _make_repo()
        schema.find_one.return_value = _RAW_TICKET

        await repo.fetch("Incident", "42")

        projection = schema.find_one.await_args.kwargs["projection"]
        self.assertNotIn("request_type", projection)


class TestFindModifiedSince(unittest.IsolatedAsyncioTestCase):
    async def test_oql_quotes_timestamp_and_has_no_status_predicate(self):
        repo, schema = _make_repo()
        schema.find.return_value = [_RAW_TICKET]

        tickets = await repo.find_modified_since(
            "UserRequest", datetime(2026, 7, 10, 12, 0, tzinfo=UTC), page=2, page_size=50
        )

        oql = schema.find.await_args.args[0]
        self.assertEqual(oql, 'SELECT UserRequest WHERE last_update >= "2026-07-10 12:00:00"')
        self.assertNotIn("status", oql)
        self.assertEqual(schema.find.await_args.kwargs["limit"], "50")
        self.assertEqual(schema.find.await_args.kwargs["page"], "2")
        self.assertEqual(tickets[0].id, "42")

    async def test_none_since_is_full_scan(self):
        repo, schema = _make_repo()
        schema.find.return_value = []

        await repo.find_modified_since("UserRequest", None, page=1, page_size=100)

        self.assertEqual(schema.find.await_args.args[0], "SELECT UserRequest")

    async def test_projection_excludes_private_log(self):
        repo, schema = _make_repo()
        schema.find.return_value = []

        await repo.find_modified_since("UserRequest", None, page=1, page_size=100)

        projection = schema.find.await_args.kwargs["projection"]
        self.assertIn("id", projection)
        self.assertIn("last_update", projection)
        self.assertNotIn("private_log", projection)

    async def test_unmapped_last_update_raises(self):
        repo, _ = _make_repo(TicketMappingConfig(fields={"last_update": None}))

        with self.assertRaises(ValueError):
            await repo.find_modified_since("UserRequest", None, page=1, page_size=100)


class TestFindExistingIds(unittest.IsolatedAsyncioTestCase):
    async def test_queries_ids_and_returns_found(self):
        repo, schema = _make_repo()
        schema.find.return_value = [{"id": "1"}, {"id": "3"}]

        existing = await repo.find_existing_ids("UserRequest", [1, 2, 3])

        self.assertEqual(existing, {1, 3})
        self.assertEqual(schema.find.await_args.args[0], "SELECT UserRequest WHERE id IN (1,2,3)")
        self.assertEqual(schema.find.await_args.kwargs["projection"], ["id"])

    async def test_empty_ids_no_call(self):
        repo, schema = _make_repo()

        self.assertEqual(await repo.find_existing_ids("UserRequest", []), set())
        schema.find.assert_not_awaited()


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
