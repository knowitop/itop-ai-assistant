import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.config import ChunkFragmentConfig, VectorClassConfig
from itop_ai_assistant.content_sources.tickets import FIELDS, FRAGMENTS, TicketVectorSource, _conversation
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.ticket import LogEntry, Ticket

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
_ENGINEER = Principal.delegated("tok", login="ivanov", name="Ivan Ivanov")


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
        "service_name": "Printing",
        "subcategory_name": "Hardware",
        "org_id": "org1",
        "caller_name": "John Doe",
        "last_update": _NOW,
        "start_date": _NOW,
    }
    fields.update(overrides)
    return Ticket(**fields)


def _ticket_repo_factory() -> tuple[AsyncMock, AsyncMock, MagicMock, MagicMock]:
    """Both accessors a source is built from (TASK-032), and the repositories
    behind them.

    Two distinct objects on purpose: sweeping reads as the service account,
    confirming a search candidate reads as whoever asked, and a test that
    could not tell them apart would pass either way.
    """
    ticket_repo = MagicMock()
    as_principal_repo = MagicMock()
    get_ticket_repo = AsyncMock(return_value=ticket_repo)
    get_ticket_repo_as = AsyncMock(return_value=as_principal_repo)
    return get_ticket_repo, get_ticket_repo_as, ticket_repo, as_principal_repo


class TestFindModifiedSince(unittest.IsolatedAsyncioTestCase):
    async def test_maps_ticket_fields_onto_vector_record(self):
        get_ticket_repo, get_ticket_repo_as, ticket_repo, _ = _ticket_repo_factory()
        ticket_repo.find_modified_since = AsyncMock(return_value=[_ticket()])
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])
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
        get_ticket_repo, get_ticket_repo_as, ticket_repo, _ = _ticket_repo_factory()
        ticket_repo.find_modified_since = AsyncMock(return_value=[_ticket()])
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])
        await source.prepare()

        await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        ticket_repo.find_modified_since.assert_awaited_once_with(
            "UserRequest", None, page=1, page_size=100, include_private_log=True
        )

    async def test_filters_none_when_no_service(self):
        get_ticket_repo, get_ticket_repo_as, ticket_repo, _ = _ticket_repo_factory()
        ticket_repo.find_modified_since = AsyncMock(return_value=[_ticket(service_id="0")])
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])
        await source.prepare()

        records = await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        self.assertIsNone(records[0].filters)


class TestFindExistingIds(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_to_ticket_repo(self):
        get_ticket_repo, get_ticket_repo_as, ticket_repo, _ = _ticket_repo_factory()
        ticket_repo.find_existing_ids = AsyncMock(return_value={1, 2})
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])
        await source.prepare()

        result = await source.find_existing_ids("UserRequest", [1, 2, 3])

        self.assertEqual(result, {1, 2})
        ticket_repo.find_existing_ids.assert_awaited_once_with("UserRequest", [1, 2, 3])


class TestConfirmVisible(unittest.IsolatedAsyncioTestCase):
    """TASK-032: the search's gate, and the one operation here that must not
    go through the service account."""

    async def test_asks_the_repository_of_the_given_principal(self):
        get_ticket_repo, get_ticket_repo_as, ticket_repo, as_principal_repo = _ticket_repo_factory()
        as_principal_repo.find_existing_ids = AsyncMock(return_value={1})
        ticket_repo.find_existing_ids = AsyncMock(return_value={1, 2, 3})
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])

        result = await source.confirm_visible(_ENGINEER, "UserRequest", [1, 2, 3])

        self.assertEqual(result, {1})
        get_ticket_repo_as.assert_awaited_once_with(_ENGINEER)
        as_principal_repo.find_existing_ids.assert_awaited_once_with("UserRequest", [1, 2, 3])
        ticket_repo.find_existing_ids.assert_not_awaited()

    async def test_needs_no_prepare(self):
        # `prepare()` caches the sweep's service-account view — the identity
        # this operation must not use. Confirming before any sweep has run is
        # normal, not a programming error.
        get_ticket_repo, get_ticket_repo_as, _repo, as_principal_repo = _ticket_repo_factory()
        as_principal_repo.find_existing_ids = AsyncMock(return_value=set())
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])

        self.assertEqual(await source.confirm_visible(_ENGINEER, "UserRequest", [1]), set())
        get_ticket_repo.assert_not_awaited()

    async def test_a_repository_is_fetched_per_call(self):
        # Two people, two answers: caching the set built for the first would
        # hand the second somebody else's tickets.
        get_ticket_repo, get_ticket_repo_as, _repo, as_principal_repo = _ticket_repo_factory()
        as_principal_repo.find_existing_ids = AsyncMock(return_value={1})
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])

        await source.confirm_visible(_ENGINEER, "UserRequest", [1])
        await source.confirm_visible(Principal.service(), "UserRequest", [1])

        self.assertEqual(
            [call.args[0] for call in get_ticket_repo_as.await_args_list], [_ENGINEER, Principal.service()]
        )


class TestChunk(unittest.IsolatedAsyncioTestCase):
    async def test_builds_profile_and_body_chunks_with_service_names(self):
        get_ticket_repo, get_ticket_repo_as, ticket_repo, _ = _ticket_repo_factory()
        ticket_repo.find_modified_since = AsyncMock(return_value=[_ticket()])
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])
        await source.prepare()
        [record] = await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        chunks = await source.chunk("UserRequest", record, _CFG, max_chunk_tokens=100, log_entries_per_chunk=5)

        by_kind = {c.kind: c for c in chunks}
        self.assertIn("Printing", by_kind["profile"].text)
        self.assertIn("Hardware", by_kind["profile"].text)
        self.assertEqual(by_kind["body"].text, "Not printing.")

    async def test_public_log_entries_labeled_by_caller_name(self):
        get_ticket_repo, get_ticket_repo_as, ticket_repo, _ = _ticket_repo_factory()
        ticket = _ticket(
            public_log=[
                LogEntry(user_login="John Doe", message="I have a problem"),
                LogEntry(user_login="Jane Agent", message="Looking into it"),
            ]
        )
        ticket_repo.find_modified_since = AsyncMock(return_value=[ticket])
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])
        await source.prepare()
        [record] = await source.find_modified_since("UserRequest", None, page=1, page_size=100)

        chunks = await source.chunk("UserRequest", record, _CFG, max_chunk_tokens=100, log_entries_per_chunk=5)

        log_chunk = next(c for c in chunks if c.kind == "log:public")
        self.assertIn("caller: I have a problem", log_chunk.text)
        self.assertIn("agent: Looking into it", log_chunk.text)

    async def test_private_log_entries_labeled_by_caller_name(self):
        get_ticket_repo, get_ticket_repo_as, ticket_repo, _ = _ticket_repo_factory()
        ticket = _ticket(private_log=[LogEntry(user_login="Jane Agent", message="Ordered a replacement part")])
        ticket_repo.find_modified_since = AsyncMock(return_value=[ticket])
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])
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
        get_ticket_repo, get_ticket_repo_as, ticket_repo, _ = _ticket_repo_factory()
        ticket_repo.find_modified_since = AsyncMock(return_value=[ticket or _ticket()])
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])
        await source.prepare()
        [record] = await source.find_modified_since("UserRequest", None, page=1, page_size=100)
        return await source.chunk("UserRequest", record, cfg, max_chunk_tokens=100, log_entries_per_chunk=5)

    async def test_declared_fields_are_exactly_the_chunkable_ones(self):
        get_ticket_repo, get_ticket_repo_as, _repo, _ = _ticket_repo_factory()
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])
        await source.prepare()

        fields = source._semantic_fields(_ticket())

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

        with self.assertLogs("itop_ai_assistant.content_sources.tickets", level="WARNING"):
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


class TestPrepare(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_the_repo_from_the_factory(self):
        """No run, no principal: the sweep is infrastructure, and the index it
        builds is global on purpose. Not tested here as behavior — this class
        is only ever given the service set's `ticket_repo` (`registry.py`), which
        never touches `for_principal`, so there is nothing else it could do."""
        get_ticket_repo, get_ticket_repo_as, ticket_repo, _ = _ticket_repo_factory()
        source = TicketVectorSource(get_ticket_repo, get_ticket_repo_as, classes=["UserRequest"])

        await source.prepare()

        get_ticket_repo.assert_awaited_once_with()
        self.assertIs(source._ticket_repo, ticket_repo)


if __name__ == "__main__":
    unittest.main()
