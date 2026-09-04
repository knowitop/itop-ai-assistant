"""The one vector source, over both of today's families.

What used to be two nearly identical classes is one, so the behaviour is
tested once — through tickets, the family with logs and a pre-filter — and the
FAQ appears where it proves the generality: a second family, nothing declared
in code beyond its own fields, no conversation to index.
"""

import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.content_sources.faq import OBJECT_TYPE as FAQ
from itop_ai_assistant.content_sources.generic import Fragment, GenericVectorSource, ObjectType, _conversation
from itop_ai_assistant.content_sources.tickets import OBJECT_TYPE as TICKETS
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.faq_schema import FAQ_SCHEMA
from itop_ai_assistant.domain.object_view import LogEntry, ObjectView
from itop_ai_assistant.domain.schema import FieldKind, FieldSpec, Role
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA
from itop_ai_assistant.vector import ChunkPlan, FamilyConfig
from itop_ai_assistant.vector.config import VectorClassConfig

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
_ENGINEER = Principal.delegated("tok", login="ivanov", name="Ivan Ivanov")


def _family(obj_class: str, acl_org_fields: list[str] | None = None) -> FamilyConfig:
    return FamilyConfig(classes={obj_class: VectorClassConfig(acl_org_fields=acl_org_fields or [])})


def _plan(*, fields: dict[str, list[str]] | None = None, enabled: set[str] | None = None) -> ChunkPlan:
    return ChunkPlan(fields=fields or {}, enabled=frozenset(enabled or ()))


_TICKET_CFG = _plan(
    fields={"profile": ["title", "service_name", "subcategory_name"], "body": ["description"]},
    enabled={"log:public"},
)
_FAQ_CFG = _plan(
    fields={"profile": ["title", "summary", "category_name", "error_code", "key_words"], "body": ["description"]}
)


def _ticket(**overrides) -> ObjectView:
    values = {
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
    values.update(overrides)
    return ObjectView(schema=TICKET_SCHEMA, obj_class="UserRequest", id="1", values=values)


def _article(**overrides) -> ObjectView:
    values = {
        "title": "How to reset your password",
        "summary": "Quick steps to reset a forgotten password",
        "category_name": "Accounts",
        "error_code": "",
        "key_words": "password, reset, login",
        "description": "Go to the login page and click reset.",
        "status": "published",
        "org_id": "org1",
        "last_update": _NOW,
        "start_date": _NOW,
    }
    values.update(overrides)
    return ObjectView(schema=FAQ_SCHEMA, obj_class="FAQ", id="1", values=values)


def _repos() -> tuple[AsyncMock, AsyncMock, MagicMock, MagicMock]:
    """Both accessors a source is built from (TASK-032), and the repositories
    behind them.

    Two distinct objects on purpose: sweeping reads as the service account,
    confirming a search candidate reads as whoever asked, and a test that
    could not tell them apart would pass either way.
    """
    repo = MagicMock()
    as_principal_repo = MagicMock()
    return AsyncMock(return_value=repo), AsyncMock(return_value=as_principal_repo), repo, as_principal_repo


async def _swept(
    object_type: ObjectType,
    obj: ObjectView,
    *,
    acl_org_fields: list[str] | None = None,
) -> tuple[GenericVectorSource, list, MagicMock]:
    get_repo, get_repo_as, repo, _ = _repos()
    repo.find_modified_since = AsyncMock(return_value=[obj])
    source = GenericVectorSource(object_type, get_repo, get_repo_as, family_cfg=_family(obj.obj_class, acl_org_fields))
    await source.prepare()
    records = await source.find_modified_since(obj.obj_class, None, page=1, page_size=100)
    return source, records, repo


class TestFindModifiedSince(unittest.IsolatedAsyncioTestCase):
    async def test_roles_decide_what_lands_on_the_record(self):
        _source, records, _repo = await _swept(TICKETS, _ticket())

        record = records[0]
        self.assertEqual(1, record.obj_id)
        self.assertEqual("resolved", record.index_value)  # Role.LIFECYCLE_STATE
        self.assertEqual(_NOW, record.updated_at)  # Role.MODIFIED_AT
        self.assertEqual(_NOW, record.created_at)  # Role.CREATED_AT
        self.assertEqual({"service_id": "5"}, record.filters)
        self.assertEqual((), record.acl_org_ids)  # no acl_org_fields configured for the class
        self.assertEqual("1", record.payload.id)

    async def test_a_family_declaring_no_pre_filter_carries_none(self):
        _source, records, _repo = await _swept(FAQ, _article())

        self.assertIsNone(records[0].filters)
        self.assertEqual("published", records[0].index_value)

    async def test_an_absent_pre_filter_value_is_no_key_rather_than_an_empty_one(self):
        _source, records, _repo = await _swept(TICKETS, _ticket(service_id=None))

        self.assertIsNone(records[0].filters)

    async def test_a_family_with_no_modification_date_says_so_with_none(self):
        _source, records, _repo = await _swept(FAQ, _article(last_update=None, status=None))

        self.assertIsNone(records[0].updated_at)
        self.assertEqual("", records[0].index_value)

    async def test_nothing_is_excluded_so_an_internal_log_is_read(self):
        # The sweep is the one reader of the private log; whether it is
        # embedded at all is decided per fragment, not by leaving it unread.
        _source, _records, repo = await _swept(TICKETS, _ticket())

        repo.find_modified_since.assert_awaited_once_with("UserRequest", None, page=1, page_size=100)


class TestAclOrgIds(unittest.IsolatedAsyncioTestCase):
    async def test_the_configured_fields_are_what_grants_access(self):
        _source, records, _repo = await _swept(TICKETS, _ticket(), acl_org_fields=["org_id"])

        self.assertEqual(("org1",), records[0].acl_org_ids)

    async def test_a_field_that_grants_no_access_warns_and_yields_nothing(self):
        # Second line behind the 422 the config save answers with: a name the
        # schema has outgrown must not fail the pass.
        with self.assertLogs("itop_ai_assistant.content_sources.acl", level="WARNING"):
            _source, records, _repo = await _swept(TICKETS, _ticket(), acl_org_fields=["nonesuch"])

        self.assertEqual((), records[0].acl_org_ids)


class TestTheTwoIdentities(unittest.IsolatedAsyncioTestCase):
    """TASK-032: the sweep's probe and the search's gate are separate
    operations with separate identities."""

    async def test_the_sweep_probe_goes_through_the_prepared_repository(self):
        get_repo, get_repo_as, repo, _ = _repos()
        repo.find_existing_ids = AsyncMock(return_value={1, 2})
        source = GenericVectorSource(TICKETS, get_repo, get_repo_as, family_cfg=_family("UserRequest"))
        await source.prepare()

        self.assertEqual({1, 2}, await source.find_existing_ids("UserRequest", [1, 2, 3]))
        repo.find_existing_ids.assert_awaited_once_with("UserRequest", [1, 2, 3])

    async def test_confirming_asks_the_repository_of_the_given_principal(self):
        get_repo, get_repo_as, repo, as_principal_repo = _repos()
        as_principal_repo.find_existing_ids = AsyncMock(return_value={1})
        repo.find_existing_ids = AsyncMock(return_value={1, 2, 3})
        source = GenericVectorSource(TICKETS, get_repo, get_repo_as, family_cfg=_family("UserRequest"))

        self.assertEqual({1}, await source.confirm_visible(_ENGINEER, "UserRequest", [1, 2, 3]))
        get_repo_as.assert_awaited_once_with(_ENGINEER)
        as_principal_repo.find_existing_ids.assert_awaited_once_with("UserRequest", [1, 2, 3])
        repo.find_existing_ids.assert_not_awaited()

    async def test_confirming_needs_no_prepare(self):
        # `prepare()` caches the sweep's service-account view — the identity
        # this operation must not use. Confirming before any sweep has run is
        # normal, not a programming error.
        get_repo, get_repo_as, _repo, as_principal_repo = _repos()
        as_principal_repo.find_existing_ids = AsyncMock(return_value=set())
        source = GenericVectorSource(TICKETS, get_repo, get_repo_as, family_cfg=_family("UserRequest"))

        self.assertEqual(set(), await source.confirm_visible(_ENGINEER, "UserRequest", [1]))
        get_repo.assert_not_awaited()

    async def test_a_repository_is_fetched_per_confirmation(self):
        # Two people, two answers: caching the set built for the first would
        # hand the second somebody else's objects.
        get_repo, get_repo_as, _repo, as_principal_repo = _repos()
        as_principal_repo.find_existing_ids = AsyncMock(return_value={1})
        source = GenericVectorSource(TICKETS, get_repo, get_repo_as, family_cfg=_family("UserRequest"))

        await source.confirm_visible(_ENGINEER, "UserRequest", [1])
        await source.confirm_visible(Principal.service(), "UserRequest", [1])

        self.assertEqual([call.args[0] for call in get_repo_as.await_args_list], [_ENGINEER, Principal.service()])

    async def test_prepare_resolves_the_sweep_repository(self):
        get_repo, get_repo_as, repo, _ = _repos()
        source = GenericVectorSource(TICKETS, get_repo, get_repo_as, family_cfg=_family("UserRequest"))

        await source.prepare()

        get_repo.assert_awaited_once_with()
        self.assertIs(repo, source._repo)


class TestChunk(unittest.IsolatedAsyncioTestCase):
    async def _chunk(self, object_type: ObjectType, obj: ObjectView, plan: ChunkPlan):
        source, [record], _repo = await _swept(object_type, obj)
        return await source.chunk(obj.obj_class, record, plan, max_chunk_tokens=100, log_entries_per_chunk=5)

    async def test_a_fragment_is_composed_of_the_configured_fields(self):
        chunks = await self._chunk(TICKETS, _ticket(), _TICKET_CFG)

        by_kind = {c.kind: c for c in chunks}
        self.assertIn("Printing", by_kind["profile"].text)
        self.assertIn("Hardware", by_kind["profile"].text)
        self.assertEqual("Not printing.", by_kind["body"].text)

    async def test_the_second_family_composes_the_same_way(self):
        chunks = await self._chunk(FAQ, _article(), _FAQ_CFG)

        by_kind = {c.kind: c for c in chunks}
        self.assertIn("How to reset your password", by_kind["profile"].text)
        self.assertIn("password, reset, login", by_kind["profile"].text)
        self.assertEqual("Go to the login page and click reset.", by_kind["body"].text)
        self.assertEqual("public", by_kind["body"].visibility)

    async def test_a_log_fragment_is_labelled_from_the_mark_on_each_entry(self):
        # Who counts as the requester was decided where the log was read; the
        # chunker knows nothing about tickets.
        ticket = _ticket(
            public_log=[
                LogEntry(user_login="John Doe", message="I have a problem", is_requester=True),
                LogEntry(user_login="Jane Agent", message="Looking into it"),
            ]
        )

        chunks = await self._chunk(TICKETS, ticket, _TICKET_CFG)

        log_chunk = next(c for c in chunks if c.kind == "log:public")
        self.assertIn("caller: I have a problem", log_chunk.text)
        self.assertIn("agent: Looking into it", log_chunk.text)

    async def test_declared_visibility_reaches_the_chunk(self):
        ticket = _ticket(private_log=[LogEntry(user_login="Jane Agent", message="ordered a part")])
        plan = _plan(fields={"body": ["description"]}, enabled={"log:private"})

        chunks = await self._chunk(TICKETS, ticket, plan)

        self.assertEqual({"log:private": "internal", "body": "public"}, {c.kind: c.visibility for c in chunks})

    async def test_every_declared_fragment_can_be_produced(self):
        ticket = _ticket(
            solution="Replaced the cartridge.",
            public_log=[LogEntry(user_login="John Doe", message="hi", is_requester=True)],
            private_log=[LogEntry(user_login="Jane Agent", message="ordered a part")],
        )
        plan = _plan(
            fields={f.kind: list(TICKETS.schema.names(Role.CONTENT)) for f in TICKETS.fragments if not f.optional},
            enabled={f.kind for f in TICKETS.fragments if f.optional},
        )

        chunks = await self._chunk(TICKETS, ticket, plan)

        self.assertEqual({f.kind for f in TICKETS.fragments}, {c.kind for c in chunks})

    async def test_an_optional_fragment_absent_from_the_config_is_off(self):
        ticket = _ticket(private_log=[LogEntry(user_login="Jane Agent", message="ordered a part")])

        chunks = await self._chunk(TICKETS, ticket, _plan(fields={"body": ["description"]}))

        self.assertEqual({"body"}, {c.kind for c in chunks})

    async def test_a_required_fragment_without_fields_produces_nothing(self):
        self.assertEqual([], await self._chunk(TICKETS, _ticket(), _plan(fields={"body": []})))

    async def test_an_unknown_field_warns_and_is_treated_as_empty(self):
        plan = _plan(fields={"body": ["description", "no_such_field"]})

        with self.assertLogs("itop_ai_assistant.content_sources.generic", level="WARNING"):
            chunks = await self._chunk(TICKETS, _ticket(), plan)

        self.assertEqual(["Not printing."], [c.text for c in chunks])

    async def test_an_unknown_fragment_kind_in_the_config_is_ignored(self):
        plan = _plan(fields={"no_such_fragment": ["description"]})

        self.assertEqual([], await self._chunk(TICKETS, _ticket(), plan))


class TestDeclaredFields(unittest.IsolatedAsyncioTestCase):
    """A field an administrator added is a field of the family — composable
    into a fragment, able to grant access, and carried into the payload,
    which is the only way its value reaches anyone at all."""

    def _family_with(self, name: str, kind: FieldKind, roles: frozenset[Role] = frozenset(), multi: bool = False):
        """The FAQ family as a deployment that declared one extra field has it."""
        return FAQ_SCHEMA.extended([FieldSpec(name, kind, None, multi=multi, roles=roles, from_config=True)])

    async def _swept_with(self, schema, values: dict, acl_org_fields: list[str] | None = None):
        get_repo, get_repo_as, repo, _ = _repos()
        view = ObjectView(schema=schema, obj_class="FAQ", id="1", values=values)
        repo.find_modified_since = AsyncMock(return_value=[view])
        source = GenericVectorSource(
            FAQ, get_repo, get_repo_as, family_cfg=_family("FAQ", acl_org_fields), schema=schema
        )
        await source.prepare()
        return source, await source.find_modified_since("FAQ", None, page=1, page_size=100)

    async def test_an_identifier_rides_into_the_payload(self):
        schema = self._family_with("vendor_id", FieldKind.ID)

        _source, records = await self._swept_with(schema, {"vendor_id": "42"})

        self.assertEqual({"vendor_id": "42"}, records[0].filters)

    async def test_prose_does_not(self):
        # The index stores embeddings, ids and filter metadata — never text.
        schema = self._family_with("vendor_note", FieldKind.TEXT)

        _source, records = await self._swept_with(schema, {"vendor_note": "a long note"})

        self.assertIsNone(records[0].filters)

    async def test_it_can_grant_access_like_a_built_in_one(self):
        schema = self._family_with("vendor_id", FieldKind.ID, frozenset({Role.ORGANIZATION}))

        source, records = await self._swept_with(schema, {"vendor_id": "42"}, acl_org_fields=["vendor_id"])

        self.assertEqual(("42",), records[0].acl_org_ids)
        self.assertIn("vendor_id", source.org_fields)

    async def test_a_list_valued_one_grants_access_through_every_value_it_holds(self):
        # A link set of customer organizations and a single `org_id` mean the
        # same thing here, and nothing the code declares holds several values.
        schema = self._family_with("customer_orgs", FieldKind.ID, frozenset({Role.ORGANIZATION}), multi=True)

        _source, records = await self._swept_with(
            schema, {"org_id": "org1", "customer_orgs": ("7", "3")}, acl_org_fields=["org_id", "customer_orgs"]
        )

        self.assertEqual(("org1", "7", "3"), records[0].acl_org_ids)

    async def test_it_can_feed_a_fragment(self):
        schema = self._family_with("vendor_note", FieldKind.TEXT, frozenset({Role.CONTENT}))

        source, [record] = await self._swept_with(schema, {"vendor_note": "Acme said so"})
        chunks = await source.chunk(
            "FAQ",
            record,
            _plan(fields={"body": ["vendor_note"]}),
            max_chunk_tokens=100,
            log_entries_per_chunk=5,
        )

        self.assertIn("vendor_note", source.fields)
        self.assertEqual(["Acme said so"], [c.text for c in chunks])

    async def test_a_family_with_nothing_declared_carries_what_it_always_did(self):
        _source, records, _repo = await _swept(TICKETS, _ticket())

        self.assertEqual({"service_id": "5"}, records[0].filters)


class TestConversation(unittest.TestCase):
    def test_the_mark_on_the_entry_is_the_whole_of_the_labelling(self):
        entries = [
            LogEntry(user_login="John Doe", message="hi", is_requester=True),
            LogEntry(user_login="Support Bot", message="hello"),
        ]

        self.assertEqual(["caller: hi", "agent: hello"], _conversation(entries))


class TestDeclarations(unittest.TestCase):
    """The vocabulary served to the admin UI (ADR-018) is derived, so it
    cannot describe something the source will not do."""

    def test_the_chunkable_vocabulary_is_what_the_family_says_is_its_content(self):
        source = GenericVectorSource(TICKETS, AsyncMock(), AsyncMock(), family_cfg=_family("UserRequest"))

        self.assertEqual(TICKET_SCHEMA.names(Role.CONTENT), source.fields)
        # Text, but a person's name — not offered as something to embed.
        self.assertNotIn("caller_name", source.fields)

    def test_a_pre_filter_has_to_name_an_identifier_of_the_family(self):
        with self.assertRaises(ValueError):
            ObjectType(schema=TICKET_SCHEMA, fragments=(), filters=("title",))
        with self.assertRaises(ValueError):
            ObjectType(schema=TICKET_SCHEMA, fragments=(), filters=("no_such_field",))

    def test_a_log_fragment_has_to_name_a_case_log_of_the_family(self):
        with self.assertRaises(ValueError):
            ObjectType(
                schema=TICKET_SCHEMA,
                fragments=(Fragment(kind="log:x", visibility="public", log_field="title", optional=True),),
            )

    def test_a_conversation_is_always_the_administrators_choice(self):
        # Whether a conversation gets embedded at all is a privacy decision,
        # so a log fragment cannot be declared as always-on.
        with self.assertRaises(ValueError):
            Fragment(kind="log:x", visibility="public", log_field="public_log")


if __name__ == "__main__":
    unittest.main()
