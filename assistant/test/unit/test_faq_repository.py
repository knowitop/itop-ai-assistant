import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.config import FaqFieldMap, FaqMappingConfig
from itop_ai_assistant.domain.faq import FaqArticle
from itop_ai_assistant.repositories.faq import FaqRepository

_RAW_ARTICLE = {
    "id": "42",
    "title": "How to reset your password",
    "summary": "Quick steps to reset a forgotten password",
    "category_name": "Accounts",
    "error_code": "",
    "key_words": "password, reset, login",
    "description": "<p>Go to the login page and click...</p>",
}


def _make_repo(mapping: FaqMappingConfig | None = None) -> tuple[FaqRepository, MagicMock]:
    schema = MagicMock()
    schema.find = AsyncMock()
    itop = MagicMock()
    itop.schema = MagicMock(return_value=schema)
    return FaqRepository(itop, mapping or FaqMappingConfig()), schema


class TestToArticle(unittest.TestCase):
    def test_maps_default_attributes(self):
        repo, _ = _make_repo()

        article = repo.to_article(_RAW_ARTICLE)

        self.assertIsInstance(article, FaqArticle)
        self.assertEqual(article.id, "42")
        self.assertEqual(article.title, "How to reset your password")
        self.assertEqual(article.summary, "Quick steps to reset a forgotten password")
        self.assertEqual(article.category_name, "Accounts")
        self.assertEqual(article.key_words, "password, reset, login")
        self.assertEqual(article.description, "<p>Go to the login page and click...</p>")
        self.assertEqual(article.status, "")  # no status attribute in stock iTop, unmapped by default
        self.assertIsNone(article.org_id)  # no org-scoped ACL in stock iTop either, unmapped by default
        self.assertIsNone(article.last_update)  # no date attribute in stock iTop either, unmapped by default

    def test_status_can_be_mapped_where_a_deployment_has_one(self):
        mapping = FaqMappingConfig(fields=FaqFieldMap(status="my_status"))
        repo, _ = _make_repo(mapping)
        raw = {**_RAW_ARTICLE, "my_status": "published"}

        article = repo.to_article(raw)

        self.assertEqual(article.status, "published")

    def test_org_id_can_be_mapped_where_a_deployment_has_one(self):
        mapping = FaqMappingConfig(fields=FaqFieldMap(org_id="my_org"))
        repo, _ = _make_repo(mapping)
        raw = {**_RAW_ARTICLE, "my_org": "7"}

        article = repo.to_article(raw)

        self.assertEqual(article.org_id, "7")

    def test_last_update_can_be_mapped_where_a_deployment_has_one(self):
        mapping = FaqMappingConfig(fields=FaqFieldMap(last_update="my_date"))
        repo, _ = _make_repo(mapping)
        raw = {**_RAW_ARTICLE, "my_date": "2026-07-10 12:00:00"}

        article = repo.to_article(raw)

        self.assertEqual(article.last_update, datetime(2026, 7, 10, 12, 0, tzinfo=UTC))

    def test_custom_field_mapping(self):
        mapping = FaqMappingConfig(fields=FaqFieldMap(title="short_title"))
        repo, _ = _make_repo(mapping)
        raw = {**_RAW_ARTICLE, "short_title": "Custom!"}

        article = repo.to_article(raw)

        self.assertEqual(article.title, "Custom!")
        self.assertEqual(article.category_name, "Accounts")

    def test_start_date_unmapped_by_default(self):
        repo, _ = _make_repo()

        article = repo.to_article(_RAW_ARTICLE)

        self.assertIsNone(article.start_date)


class TestFindModifiedSince(unittest.IsolatedAsyncioTestCase):
    async def test_default_mapping_always_does_a_full_scan(self):
        # Stock iTop's FAQ carries no date attribute — `since` has nothing to
        # filter by, so it is ignored rather than raising.
        repo, schema = _make_repo()
        schema.find.return_value = [_RAW_ARTICLE]

        articles = await repo.find_modified_since(datetime(2026, 7, 10, 12, 0, tzinfo=UTC), page=2, page_size=50)

        self.assertEqual(schema.find.await_args.args[0], {})
        self.assertEqual(schema.find.await_args.kwargs["limit"], "50")
        self.assertEqual(schema.find.await_args.kwargs["page"], "2")
        self.assertEqual(articles[0].id, "42")

    async def test_none_since_is_full_scan(self):
        repo, schema = _make_repo()
        schema.find.return_value = []

        await repo.find_modified_since(None, page=1, page_size=100)

        self.assertEqual(schema.find.await_args.args[0], {})

    async def test_mapped_last_update_filters_and_has_no_status_predicate(self):
        mapping = FaqMappingConfig(fields=FaqFieldMap(last_update="my_date"))
        repo, schema = _make_repo(mapping)
        schema.find.return_value = []

        await repo.find_modified_since(datetime(2026, 7, 10, 12, 0, tzinfo=UTC), page=1, page_size=100)

        query = schema.find.await_args.args[0]
        self.assertEqual(query, {"my_date": (">=", "2026-07-10 12:00:00")})
        self.assertNotIn("status", query)


class TestFindExistingIds(unittest.IsolatedAsyncioTestCase):
    async def test_queries_ids_and_returns_found(self):
        repo, schema = _make_repo()
        schema.find.return_value = [{"id": "1"}, {"id": "3"}]

        existing = await repo.find_existing_ids([1, 2, 3])

        self.assertEqual(existing, {1, 3})
        self.assertEqual(schema.find.await_args.args[0], "SELECT FAQ WHERE id IN (1,2,3)")
        self.assertEqual(schema.find.await_args.kwargs["projection"], ["id"])

    async def test_empty_ids_no_call(self):
        repo, schema = _make_repo()

        self.assertEqual(await repo.find_existing_ids([]), set())
        schema.find.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
