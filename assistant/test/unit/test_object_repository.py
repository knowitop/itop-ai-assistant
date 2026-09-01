"""The one repository, over both of today's families: what a kind does to a
raw iTop value, what the projection asks for, and what a write refuses."""

import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import fakeredis

from itop_ai_assistant.config import FaqFieldMap, FaqMappingConfig, TicketMappingConfig
from itop_ai_assistant.domain.faq_schema import FAQ_SCHEMA
from itop_ai_assistant.domain.identity import ObjectIdentity
from itop_ai_assistant.domain.schema import Role
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA
from itop_ai_assistant.repositories.object_repo import ObjectRepository
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

_RAW_ARTICLE = {
    "id": "7",
    "title": "How to reset your password",
    "summary": "Quick steps to reset a forgotten password",
    "category_name": "Accounts",
    "error_code": "",
    "key_words": "password, reset, login",
    "description": "<p>Go to the login page and click...</p>",
}


def _tickets(mapping=None, counters=None) -> tuple[ObjectRepository, MagicMock]:
    return _repo(TICKET_SCHEMA, mapping or TicketMappingConfig(), counters)


def _faq(mapping=None) -> tuple[ObjectRepository, MagicMock]:
    return _repo(FAQ_SCHEMA, mapping or FaqMappingConfig(), None)


def _repo(schema, mapping, counters) -> tuple[ObjectRepository, MagicMock]:
    itop_schema = MagicMock()
    itop_schema.find = AsyncMock()
    itop_schema.find_one = AsyncMock()
    itop_schema.update = AsyncMock()
    itop = MagicMock()
    itop.schema = MagicMock(return_value=itop_schema)
    counters = counters or DailyCounters(fakeredis.aioredis.FakeRedis(decode_responses=True))
    return ObjectRepository(itop, schema, mapping, counters), itop_schema


class TestReadingByKind(unittest.TestCase):
    """The kind is the whole of "what to do with what iTop returned" — there
    is no per-field decision left to get wrong."""

    def test_each_kind_reads_its_own_way(self):
        repo, _ = _tickets()

        view = repo.to_view("UserRequest", _RAW_TICKET)

        self.assertEqual("Printer broken", view.text("title"))
        self.assertEqual("new", view.state("status"))
        self.assertEqual("5", view.identifier("service_id"))
        self.assertEqual(datetime(2026, 7, 10, 12, 0, tzinfo=UTC), view.moment("last_update"))
        self.assertEqual(["John Doe"], [entry.user_login for entry in view.log("public_log")])
        self.assertEqual("UserRequest::42", str(view.identity))

    def test_a_case_log_entry_is_marked_against_the_requester(self):
        # Marked here because here is the only place that knows both the log
        # and the field naming the requester — downstream a log entry is a
        # line with a flag on it, and nothing has to know what a ticket is.
        repo, _ = _tickets()
        raw = {
            **_RAW_TICKET,
            "public_log": {
                "entries": [
                    {"user_login": "John Doe", "message": "Help!"},
                    {"user_login": "Jane Agent", "message": "On it."},
                ]
            },
        }

        entries = repo.to_view("UserRequest", raw).log("public_log")

        self.assertEqual([True, False], [entry.is_requester for entry in entries])

    def test_with_no_requester_mapped_nobody_is_the_requester(self):
        repo, _ = _tickets(TicketMappingConfig(fields={"caller_name": None}))

        entries = repo.to_view("UserRequest", _RAW_TICKET).log("public_log")

        self.assertEqual([False], [entry.is_requester for entry in entries])

    def test_itops_unset_external_key_reads_as_no_value(self):
        repo, _ = _tickets()

        view = repo.to_view("UserRequest", {**_RAW_TICKET, "service_id": "0", "org_id": "0"})

        self.assertIsNone(view.identifier("service_id"))
        self.assertEqual((), view.identifiers("org_id"))

    def test_an_unmapped_field_is_absent_not_empty(self):
        # Incident has no request_type: the value must not be there at all, so
        # a typed model over the view falls back to its own default.
        repo, _ = _tickets()

        view = repo.to_view("Incident", _RAW_TICKET)

        self.assertNotIn("request_type", view.values)
        self.assertIn("request_type", repo.to_view("UserRequest", _RAW_TICKET).values)

    def test_a_field_the_row_does_not_carry_is_absent_too(self):
        repo, _ = _tickets()

        view = repo.to_view("UserRequest", {k: v for k, v in _RAW_TICKET.items() if k != "last_update"})

        self.assertNotIn("last_update", view.values)
        self.assertIsNone(view.moment("last_update"))

    def test_a_custom_attribute_code_is_read_from_where_it_says(self):
        repo, _ = _tickets(TicketMappingConfig(fields={"title": "custom_title"}))

        view = repo.to_view("UserRequest", {**_RAW_TICKET, "custom_title": "Custom!"})

        self.assertEqual("Custom!", view.text("title"))
        self.assertEqual("John Doe", view.text("caller_name"))

    def test_a_link_set_yields_every_id_it_holds(self):
        repo, _ = _faq(FaqMappingConfig(fields=FaqFieldMap(customer_org_ids="customers_list:customer_id")))
        raw = {**_RAW_ARTICLE, "customers_list": [{"customer_id": "7"}, {"customer_id": "3"}]}

        view = repo.to_view("FAQ", raw)

        self.assertEqual(("3", "7"), view.identifiers("customer_org_ids"))

    def test_stock_faq_declares_no_status_no_organization_and_no_dates(self):
        repo, _ = _faq()

        view = repo.to_view("FAQ", _RAW_ARTICLE)

        self.assertEqual("", view.state_of(Role.LIFECYCLE_STATE))
        self.assertIsNone(view.moment_of(Role.MODIFIED_AT))
        self.assertEqual((), view.identifiers("org_id"))
        self.assertEqual("Accounts", view.text("category_name"))


class TestProjection(unittest.IsolatedAsyncioTestCase):
    async def test_asks_only_for_mapped_attributes_and_honours_exclude(self):
        repo, itop = _tickets()
        itop.find_one.return_value = _RAW_TICKET

        await repo.read("UserRequest", "42", exclude={"private_log"})

        projection = itop.find_one.await_args.kwargs["projection"]
        self.assertIn("id", projection)
        self.assertIn("servicesubcategory_id", projection)
        self.assertNotIn("private_log", projection)

    async def test_a_class_override_removes_the_attribute_from_the_projection(self):
        repo, itop = _tickets()
        itop.find_one.return_value = _RAW_TICKET

        await repo.read("Incident", "42")

        self.assertNotIn("request_type", itop.find_one.await_args.kwargs["projection"])

    async def test_a_link_set_is_asked_for_by_its_own_name(self):
        repo, itop = _faq(FaqMappingConfig(fields=FaqFieldMap(customer_org_ids="customers_list:customer_id")))
        itop.find.return_value = []

        await repo.find_modified_since("FAQ", None, page=1, page_size=10)

        projection = itop.find.await_args.kwargs["projection"]
        self.assertIn("customers_list", projection)
        self.assertNotIn("customers_list:customer_id", projection)

    async def test_a_missing_object_reads_as_none(self):
        repo, itop = _tickets()
        itop.find_one.return_value = None

        self.assertIsNone(await repo.read("UserRequest", "42"))


class TestFindModifiedSince(unittest.IsolatedAsyncioTestCase):
    async def test_the_cursor_is_the_modification_date_and_there_is_no_relevance_predicate(self):
        repo, itop = _tickets()
        itop.find.return_value = [_RAW_TICKET]

        views = await repo.find_modified_since(
            "UserRequest", datetime(2026, 7, 10, 12, 0, tzinfo=UTC), page=2, page_size=50
        )

        self.assertEqual({"last_update": (">=", "2026-07-10 12:00:00")}, itop.find.await_args.args[0])
        self.assertEqual("50", itop.find.await_args.kwargs["limit"])
        self.assertEqual("2", itop.find.await_args.kwargs["page"])
        self.assertEqual("42", views[0].id)

    async def test_no_since_is_a_full_scan(self):
        repo, itop = _tickets()
        itop.find.return_value = []

        await repo.find_modified_since("UserRequest", None, page=1, page_size=100)

        self.assertEqual({}, itop.find.await_args.args[0])

    async def test_an_unmapped_cursor_is_a_full_scan_and_says_so_once(self):
        # Stock iTop's FAQ has no date attribute at all: refusing here would
        # make an optional field mandatory for every family.
        repo, itop = _tickets(TicketMappingConfig(fields={"last_update": None}))
        itop.find.return_value = []

        with self.assertLogs("itop_ai_assistant.repositories.object_repo", level="WARNING") as logs:
            await repo.find_modified_since("UserRequest", datetime.now(UTC), page=1, page_size=10)
            await repo.find_modified_since("UserRequest", datetime.now(UTC), page=2, page_size=10)

        self.assertEqual({}, itop.find.await_args.args[0])
        self.assertEqual(1, len(logs.output))


class TestFindExistingIds(unittest.IsolatedAsyncioTestCase):
    async def test_queries_ids_and_returns_found(self):
        repo, itop = _tickets()
        itop.find.return_value = [{"id": "1"}, {"id": "3"}]

        self.assertEqual({1, 3}, await repo.find_existing_ids("UserRequest", [1, 2, 3]))
        self.assertEqual("SELECT UserRequest WHERE id IN (1,2,3)", itop.find.await_args.args[0])

    async def test_empty_ids_no_call(self):
        repo, itop = _tickets()

        self.assertEqual(set(), await repo.find_existing_ids("UserRequest", []))
        itop.find.assert_not_awaited()


class TestWrites(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ticket = ObjectIdentity(obj_class="UserRequest", obj_id="42")

    async def test_semantic_names_become_attribute_codes(self):
        repo, itop = _tickets()

        await repo.set_fields(self.ticket, {"service_id": "10", "subcategory_id": "101"})

        itop.update.assert_awaited_once_with({"id": "42"}, {"service_id": "10", "servicesubcategory_id": "101"})

    async def test_a_field_iTop_computes_cannot_be_written(self):
        repo, _ = _tickets()

        with self.assertRaises(ValueError) as raised:
            await repo.set_fields(self.ticket, {"ref": "R-1"})

        self.assertIn("read-only", str(raised.exception))

    async def test_a_field_this_deployment_does_not_map_is_skipped(self):
        repo, itop = _tickets()
        incident = ObjectIdentity(obj_class="Incident", obj_id="42")

        await repo.set_fields(incident, {"request_type": "incident", "service_id": "10"})

        itop.update.assert_awaited_once_with({"id": "42"}, {"service_id": "10"})

    async def test_an_update_that_never_reached_itop_is_not_counted(self):
        counters = DailyCounters(fakeredis.aioredis.FakeRedis(decode_responses=True))
        repo, itop = _tickets(counters=counters)

        await repo.set_fields(ObjectIdentity(obj_class="Incident", obj_id="42"), {"request_type": "incident"})

        itop.update.assert_not_called()
        self.assertEqual(0, (await counters.read(datetime.now(UTC).date()))[Counter.ITOP_FIELD_UPDATE])

    async def test_a_case_log_is_appended_to_never_rewritten(self):
        repo, itop = _tickets()

        await repo.append_log(self.ticket, "public_log", "A question", counter=Counter.ITOP_PUBLIC_COMMENT)

        itop.update.assert_awaited_once_with(
            {"id": "42"}, {"public_log": {"add_item": {"message": "A question", "format": "text"}}}
        )

    async def test_only_a_case_log_can_be_appended_to(self):
        repo, _ = _tickets()

        with self.assertRaises(ValueError):
            await repo.append_log(self.ticket, "title", "text", counter=Counter.ITOP_PUBLIC_COMMENT)

    async def test_a_family_nothing_writes_to_can_write_nothing(self):
        # Every FAQ field is read-only, which is the whole of "the sweep only
        # ever reads FAQ content".
        repo, _ = _faq()

        with self.assertRaises(ValueError):
            await repo.set_fields(ObjectIdentity(obj_class="FAQ", obj_id="7"), {"title": "New"})


class TestUnmapped(unittest.TestCase):
    def test_names_what_this_deployment_does_not_map(self):
        repo, _ = _tickets(TicketMappingConfig(fields={"caller_name": None}))

        self.assertEqual(("caller_name",), repo.unmapped("UserRequest", ("title", "caller_name")))
        self.assertEqual((), repo.unmapped("UserRequest", ("title",)))


if __name__ == "__main__":
    unittest.main()
