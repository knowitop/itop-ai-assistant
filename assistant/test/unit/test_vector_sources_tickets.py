import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from domain.catalog import Service, ServiceSubcategory
from domain.ticket import LogEntry, Ticket
from vector_sources.tickets import TicketVectorSource, _to_conversation

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
_PROFILE = {"profile": ["title", "service", "subcategory"], "body": ["description"], "log:public": []}


def _ticket(**overrides) -> Ticket:
    fields = {
        "obj_class": "UserRequest",
        "id": "1",
        "title": "Printer broken",
        "description": "Not printing.",
        "status": "resolved",
        "service_id": "5",
        "subcategory_id": "9",
        "org_id": "org1",
        "caller_name": "John Doe",
        "last_update": _NOW,
        "created_at": _NOW,
    }
    fields.update(overrides)
    return Ticket(**fields)


def _deps_with_bundle() -> tuple[MagicMock, MagicMock]:
    bundle = MagicMock()
    bundle.catalog_repo.get_service = AsyncMock(return_value=Service(id="5", name="Printing"))
    bundle.catalog_repo.get_subcategory = AsyncMock(
        return_value=ServiceSubcategory(id="9", name="Hardware", service_id="5")
    )
    deps = MagicMock()
    deps.itop.get = AsyncMock(return_value=bundle)
    return deps, bundle


class TestFindModifiedSince(unittest.IsolatedAsyncioTestCase):
    async def test_maps_ticket_fields_onto_vector_record(self):
        deps, bundle = _deps_with_bundle()
        bundle.ticket_repo.find_modified_since = AsyncMock(return_value=[_ticket()])
        source = TicketVectorSource(deps, classes=["UserRequest"])
        await source.prepare()

        records = await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.obj_id, 1)
        self.assertEqual(record.index_value, "resolved")
        self.assertEqual(record.org_id, "org1")
        self.assertEqual(record.filters, {"service_id": "5"})
        self.assertEqual(record.payload.id, "1")

    async def test_filters_none_when_no_service(self):
        deps, bundle = _deps_with_bundle()
        bundle.ticket_repo.find_modified_since = AsyncMock(return_value=[_ticket(service_id="0")])
        source = TicketVectorSource(deps, classes=["UserRequest"])
        await source.prepare()

        records = await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        self.assertIsNone(records[0].filters)


class TestFindExistingIds(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_to_ticket_repo(self):
        deps, bundle = _deps_with_bundle()
        bundle.ticket_repo.find_existing_ids = AsyncMock(return_value={1, 2})
        source = TicketVectorSource(deps, classes=["UserRequest"])
        await source.prepare()

        result = await source.find_existing_ids("UserRequest", [1, 2, 3])

        self.assertEqual(result, {1, 2})
        bundle.ticket_repo.find_existing_ids.assert_awaited_once_with("UserRequest", [1, 2, 3])


class TestChunk(unittest.IsolatedAsyncioTestCase):
    async def test_builds_profile_and_body_chunks_with_catalog_names(self):
        deps, bundle = _deps_with_bundle()
        bundle.ticket_repo.find_modified_since = AsyncMock(return_value=[_ticket()])
        source = TicketVectorSource(deps, classes=["UserRequest"])
        await source.prepare()
        [record] = await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        chunks = await source.chunk("UserRequest", record, _PROFILE, max_chunk_tokens=100, log_entries_per_chunk=5)

        by_kind = {c.kind: c for c in chunks}
        self.assertIn("Printing", by_kind["profile"].text)
        self.assertIn("Hardware", by_kind["profile"].text)
        self.assertEqual(by_kind["body"].text, "Not printing.")

    async def test_catalog_names_are_memoized_within_a_sweep(self):
        deps, bundle = _deps_with_bundle()
        bundle.ticket_repo.find_modified_since = AsyncMock(return_value=[_ticket(id="1"), _ticket(id="2")])
        source = TicketVectorSource(deps, classes=["UserRequest"])
        await source.prepare()
        records = await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        for record in records:
            await source.chunk("UserRequest", record, _PROFILE, max_chunk_tokens=100, log_entries_per_chunk=5)

        bundle.catalog_repo.get_service.assert_awaited_once()

    async def test_public_log_entries_labeled_by_caller_name(self):
        deps, bundle = _deps_with_bundle()
        ticket = _ticket(
            public_log=[
                LogEntry(user_login="John Doe", message="I have a problem"),
                LogEntry(user_login="Jane Agent", message="Looking into it"),
            ]
        )
        bundle.ticket_repo.find_modified_since = AsyncMock(return_value=[ticket])
        source = TicketVectorSource(deps, classes=["UserRequest"])
        await source.prepare()
        [record] = await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        chunks = await source.chunk("UserRequest", record, _PROFILE, max_chunk_tokens=100, log_entries_per_chunk=5)

        log_chunk = next(c for c in chunks if c.kind == "log:public")
        self.assertIn("caller: I have a problem", log_chunk.text)
        self.assertIn("agent: Looking into it", log_chunk.text)


class TestToConversation(unittest.TestCase):
    def test_labels_matching_login_as_caller(self):
        entries = [
            LogEntry(user_login="John Doe", message="hi"),
            LogEntry(user_login="Support Bot", message="hello"),
        ]

        result = _to_conversation(entries, caller_name="John Doe")

        self.assertEqual([e.speaker for e in result], ["caller", "agent"])


if __name__ == "__main__":
    unittest.main()
