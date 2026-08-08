import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.config import ChunkFragmentConfig, VectorClassConfig
from itop_ai_assistant.domain.catalog import Service, ServiceSubcategory
from itop_ai_assistant.domain.ticket import LogEntry, Ticket
from itop_ai_assistant.vector_sources.tickets import FIELDS, FRAGMENTS, TicketVectorSource, _conversation

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _cfg(fragments: dict[str, ChunkFragmentConfig]) -> VectorClassConfig:
    return VectorClassConfig(chunks=fragments)


_CFG = _cfg(
    {
        "profile": ChunkFragmentConfig(fields=["title", "service", "subcategory"]),
        "body": ChunkFragmentConfig(fields=["description"]),
        "log:public": ChunkFragmentConfig(),
    }
)


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
        "start_date": _NOW,
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

    async def test_requests_private_log_from_the_repository(self):
        deps, bundle = _deps_with_bundle()
        bundle.ticket_repo.find_modified_since = AsyncMock(return_value=[_ticket()])
        source = TicketVectorSource(deps, classes=["UserRequest"])
        await source.prepare()

        await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        bundle.ticket_repo.find_modified_since.assert_awaited_once_with(
            "UserRequest", None, page=1, page_size=100, include_private_log=True
        )

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

        chunks = await source.chunk("UserRequest", record, _CFG, max_chunk_tokens=100, log_entries_per_chunk=5)

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
            await source.chunk("UserRequest", record, _CFG, max_chunk_tokens=100, log_entries_per_chunk=5)

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

        chunks = await source.chunk("UserRequest", record, _CFG, max_chunk_tokens=100, log_entries_per_chunk=5)

        log_chunk = next(c for c in chunks if c.kind == "log:public")
        self.assertIn("caller: I have a problem", log_chunk.text)
        self.assertIn("agent: Looking into it", log_chunk.text)

    async def test_private_log_entries_labeled_by_caller_name(self):
        deps, bundle = _deps_with_bundle()
        ticket = _ticket(private_log=[LogEntry(user_login="Jane Agent", message="Ordered a replacement part")])
        bundle.ticket_repo.find_modified_since = AsyncMock(return_value=[ticket])
        source = TicketVectorSource(deps, classes=["UserRequest"])
        await source.prepare()
        [record] = await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        cfg = _cfg({"log:private": ChunkFragmentConfig()})
        chunks = await source.chunk("UserRequest", record, cfg, max_chunk_tokens=100, log_entries_per_chunk=5)

        log_chunk = next(c for c in chunks if c.kind == "log:private")
        self.assertIn("agent: Ordered a replacement part", log_chunk.text)
        self.assertEqual(log_chunk.visibility, "internal")


class TestDeclaration(unittest.IsolatedAsyncioTestCase):
    """The vocabulary served to the admin UI (ADR-018) must describe what the
    source can actually do — a stale declaration would put fields and
    fragments in the editor that quietly produce nothing."""

    async def _chunk(self, cfg: VectorClassConfig, ticket: Ticket | None = None):
        deps, bundle = _deps_with_bundle()
        bundle.ticket_repo.find_modified_since = AsyncMock(return_value=[ticket or _ticket()])
        source = TicketVectorSource(deps, classes=["UserRequest"])
        await source.prepare()
        [record] = await source.find_modified_since("UserRequest", None, page=1, page_size=100)
        return await source.chunk("UserRequest", record, cfg, max_chunk_tokens=100, log_entries_per_chunk=5)

    async def test_declared_fields_are_exactly_the_chunkable_ones(self):
        deps, _ = _deps_with_bundle()
        source = TicketVectorSource(deps, classes=["UserRequest"])
        await source.prepare()

        fields = await source._semantic_fields(_ticket())

        self.assertEqual(set(FIELDS), set(fields))

    async def test_every_declared_fragment_can_be_produced(self):
        ticket = _ticket(
            solution="Replaced the cartridge.",
            public_log=[LogEntry(user_login="John Doe", message="hi")],
            private_log=[LogEntry(user_login="Jane Agent", message="ordered a part")],
        )
        cfg = _cfg(
            {spec.kind: ChunkFragmentConfig(fields=list(FIELDS) if not spec.optional else []) for spec in FRAGMENTS}
        )

        chunks = await self._chunk(cfg, ticket)

        self.assertEqual({c.kind for c in chunks}, {spec.kind for spec in FRAGMENTS})

    async def test_declared_visibility_reaches_the_chunk(self):
        ticket = _ticket(private_log=[LogEntry(user_login="Jane Agent", message="ordered a part")])
        cfg = _cfg({"log:private": ChunkFragmentConfig(), "body": ChunkFragmentConfig(fields=["description"])})

        chunks = await self._chunk(cfg, ticket)

        by_kind = {c.kind: c.visibility for c in chunks}
        self.assertEqual(by_kind, {"log:private": "internal", "body": "public"})

    async def test_optional_fragment_absent_from_config_is_off(self):
        ticket = _ticket(private_log=[LogEntry(user_login="Jane Agent", message="ordered a part")])

        chunks = await self._chunk(_cfg({"body": ChunkFragmentConfig(fields=["description"])}), ticket)

        self.assertEqual({c.kind for c in chunks}, {"body"})

    async def test_optional_fragment_switched_off_explicitly(self):
        ticket = _ticket(private_log=[LogEntry(user_login="Jane Agent", message="ordered a part")])
        cfg = _cfg({"log:private": ChunkFragmentConfig(enabled=False)})

        self.assertEqual(await self._chunk(cfg, ticket), [])

    async def test_required_fragment_without_fields_produces_nothing(self):
        self.assertEqual(await self._chunk(_cfg({"body": ChunkFragmentConfig(fields=[])})), [])

    async def test_unknown_field_warns_and_is_treated_as_empty(self):
        cfg = _cfg({"body": ChunkFragmentConfig(fields=["description", "no_such_field"])})

        with self.assertLogs("itop_ai_assistant.vector_sources.tickets", level="WARNING"):
            chunks = await self._chunk(cfg)

        self.assertEqual([c.text for c in chunks], ["Not printing."])

    async def test_unknown_fragment_kind_in_config_is_ignored(self):
        cfg = _cfg({"no_such_fragment": ChunkFragmentConfig(fields=["description"])})

        self.assertEqual(await self._chunk(cfg), [])


class TestConversation(unittest.TestCase):
    def test_labels_matching_login_as_caller(self):
        entries = [
            LogEntry(user_login="John Doe", message="hi"),
            LogEntry(user_login="Support Bot", message="hello"),
        ]

        result = _conversation(entries, _ticket())

        self.assertEqual(result, ["caller: hi", "agent: hello"])


class TestNoPrincipal(unittest.IsolatedAsyncioTestCase):
    async def test_the_sweep_takes_the_plain_connection(self):
        """No run, no principal: the sweep is infrastructure, and the index it
        builds is global on purpose. Acting as somebody would be the mistake."""
        deps, _ = _deps_with_bundle()

        await TicketVectorSource(deps, classes=["UserRequest"]).prepare()

        deps.itop.get.assert_awaited_once_with()
        deps.itop.for_principal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
