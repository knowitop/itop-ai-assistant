import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.config import ChunkFragmentConfig, VectorClassConfig
from itop_ai_assistant.content_sources.faq import FIELDS, FRAGMENTS, FaqVectorSource
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.faq import FaqArticle

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
_ENGINEER = Principal.delegated("tok", login="ivanov", name="Ivan Ivanov")


def _cfg(fragments: dict[str, ChunkFragmentConfig]) -> VectorClassConfig:
    return VectorClassConfig(chunks=fragments)


_CFG = _cfg(
    {
        "profile": ChunkFragmentConfig(fields=["title", "summary", "category_name", "error_code", "key_words"]),
        "body": ChunkFragmentConfig(fields=["description"]),
    }
)


def _article(**overrides) -> FaqArticle:
    fields = {
        "id": "1",
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
    fields.update(overrides)
    return FaqArticle(**fields)


def _faq_repo_factory() -> tuple[AsyncMock, AsyncMock, MagicMock, MagicMock]:
    """Both accessors a source is built from — see the same factory in
    `test_vector_sources_tickets.py` for why they are distinct objects."""
    faq_repo = MagicMock()
    as_principal_repo = MagicMock()
    get_faq_repo = AsyncMock(return_value=faq_repo)
    get_faq_repo_as = AsyncMock(return_value=as_principal_repo)
    return get_faq_repo, get_faq_repo_as, faq_repo, as_principal_repo


class TestFindModifiedSince(unittest.IsolatedAsyncioTestCase):
    async def test_maps_article_fields_onto_vector_record(self):
        get_faq_repo, get_faq_repo_as, faq_repo, _ = _faq_repo_factory()
        faq_repo.find_modified_since = AsyncMock(return_value=[_article()])
        source = FaqVectorSource(get_faq_repo, get_faq_repo_as, classes=["FAQ"])
        await source.prepare()

        records = await source.find_modified_since("FAQ", None, page=1, page_size=100)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.obj_id, 1)
        self.assertEqual(record.index_value, "published")
        self.assertEqual(record.org_id, "org1")
        self.assertIsNone(record.filters)
        self.assertEqual(record.payload.id, "1")

    async def test_delegates_to_the_repository(self):
        get_faq_repo, get_faq_repo_as, faq_repo, _ = _faq_repo_factory()
        faq_repo.find_modified_since = AsyncMock(return_value=[_article()])
        source = FaqVectorSource(get_faq_repo, get_faq_repo_as, classes=["FAQ"])
        await source.prepare()

        await source.find_modified_since("FAQ", None, page=1, page_size=100)

        faq_repo.find_modified_since.assert_awaited_once_with(None, page=1, page_size=100)


class TestFindExistingIds(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_to_the_repository(self):
        get_faq_repo, get_faq_repo_as, faq_repo, _ = _faq_repo_factory()
        faq_repo.find_existing_ids = AsyncMock(return_value={1, 2})
        source = FaqVectorSource(get_faq_repo, get_faq_repo_as, classes=["FAQ"])
        await source.prepare()

        result = await source.find_existing_ids("FAQ", [1, 2, 3])

        self.assertEqual(result, {1, 2})
        faq_repo.find_existing_ids.assert_awaited_once_with([1, 2, 3])


class TestConfirmVisible(unittest.IsolatedAsyncioTestCase):
    async def test_asks_the_repository_of_the_given_principal_dropping_obj_class(self):
        # TASK-032. `obj_class` goes nowhere here: this source owns one class,
        # and `FaqRepository` queries `FAQ` by name — same adaptation as
        # `find_existing_ids` above, under the caller's identity instead of
        # the sweep's.
        get_faq_repo, get_faq_repo_as, faq_repo, as_principal_repo = _faq_repo_factory()
        as_principal_repo.find_existing_ids = AsyncMock(return_value={1})
        faq_repo.find_existing_ids = AsyncMock(return_value={1, 2, 3})
        source = FaqVectorSource(get_faq_repo, get_faq_repo_as, classes=["FAQ"])

        result = await source.confirm_visible(_ENGINEER, "FAQ", [1, 2, 3])

        self.assertEqual(result, {1})
        get_faq_repo_as.assert_awaited_once_with(_ENGINEER)
        as_principal_repo.find_existing_ids.assert_awaited_once_with([1, 2, 3])
        faq_repo.find_existing_ids.assert_not_awaited()


class TestChunk(unittest.IsolatedAsyncioTestCase):
    async def test_builds_profile_and_body_chunks(self):
        get_faq_repo, get_faq_repo_as, faq_repo, _ = _faq_repo_factory()
        faq_repo.find_modified_since = AsyncMock(return_value=[_article()])
        source = FaqVectorSource(get_faq_repo, get_faq_repo_as, classes=["FAQ"])
        await source.prepare()
        [record] = await source.find_modified_since("FAQ", None, page=1, page_size=100)

        chunks = await source.chunk("FAQ", record, _CFG, max_chunk_tokens=100, log_entries_per_chunk=5)

        by_kind = {c.kind: c for c in chunks}
        self.assertIn("How to reset your password", by_kind["profile"].text)
        self.assertIn("Accounts", by_kind["profile"].text)
        self.assertIn("password, reset, login", by_kind["profile"].text)
        self.assertEqual(by_kind["body"].text, "Go to the login page and click reset.")
        self.assertEqual(by_kind["profile"].visibility, "public")
        self.assertEqual(by_kind["body"].visibility, "public")


class TestDeclaration(unittest.IsolatedAsyncioTestCase):
    """The vocabulary served to the admin UI (ADR-018) must describe what the
    source can actually do — a stale declaration would put fields and
    fragments in the editor that quietly produce nothing."""

    async def _chunk(self, cfg: VectorClassConfig, article: FaqArticle | None = None):
        get_faq_repo, get_faq_repo_as, faq_repo, _ = _faq_repo_factory()
        faq_repo.find_modified_since = AsyncMock(return_value=[article or _article()])
        source = FaqVectorSource(get_faq_repo, get_faq_repo_as, classes=["FAQ"])
        await source.prepare()
        [record] = await source.find_modified_since("FAQ", None, page=1, page_size=100)
        return await source.chunk("FAQ", record, cfg, max_chunk_tokens=100, log_entries_per_chunk=5)

    async def test_declared_fields_are_exactly_the_chunkable_ones(self):
        get_faq_repo, get_faq_repo_as, faq_repo, _ = _faq_repo_factory()
        source = FaqVectorSource(get_faq_repo, get_faq_repo_as, classes=["FAQ"])
        await source.prepare()

        fields = source._semantic_fields(_article())

        self.assertEqual(set(FIELDS), set(fields))

    async def test_every_declared_fragment_can_be_produced(self):
        cfg = _cfg({spec.kind: ChunkFragmentConfig(fields=list(FIELDS)) for spec in FRAGMENTS})

        chunks = await self._chunk(cfg)

        self.assertEqual({c.kind for c in chunks}, {spec.kind for spec in FRAGMENTS})

    async def test_fragment_without_fields_produces_nothing(self):
        self.assertEqual(await self._chunk(_cfg({"body": ChunkFragmentConfig(fields=[])})), [])

    async def test_unknown_field_warns_and_is_treated_as_empty(self):
        cfg = _cfg({"body": ChunkFragmentConfig(fields=["description", "no_such_field"])})

        with self.assertLogs("itop_ai_assistant.content_sources.faq", level="WARNING"):
            chunks = await self._chunk(cfg)

        self.assertEqual([c.text for c in chunks], ["Go to the login page and click reset."])

    async def test_unknown_fragment_kind_in_config_is_ignored(self):
        cfg = _cfg({"no_such_fragment": ChunkFragmentConfig(fields=["description"])})

        self.assertEqual(await self._chunk(cfg), [])


class TestPrepare(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_the_repo_from_the_factory(self):
        """No run, no principal: the sweep is infrastructure, and the index it
        builds is global on purpose. Not tested here as behavior — this class
        is only ever given the service set's `faq_repo` (`registry.py`), which
        never touches `for_principal`, so there is nothing else it could do."""
        get_faq_repo, get_faq_repo_as, faq_repo, _ = _faq_repo_factory()
        source = FaqVectorSource(get_faq_repo, get_faq_repo_as, classes=["FAQ"])

        await source.prepare()

        get_faq_repo.assert_awaited_once_with()
        self.assertIs(source._faq_repo, faq_repo)


if __name__ == "__main__":
    unittest.main()
