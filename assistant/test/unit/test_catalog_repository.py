import unittest
from unittest.mock import AsyncMock, MagicMock

from catalog_repository import CatalogRepository
from domain.catalog import CatalogItem

_RAW_SERVICE = {"id": "5", "name": "IT Support", "description": "General IT"}


def _make_repo() -> tuple[CatalogRepository, MagicMock, MagicMock]:
    schema = MagicMock()
    schema.find = AsyncMock(return_value=[_RAW_SERVICE])
    schema.find_one = AsyncMock(return_value=_RAW_SERVICE)
    itop = MagicMock()
    itop.schema = MagicMock(return_value=schema)
    return CatalogRepository(itop), itop, schema


class TestCatalogRepository(unittest.IsolatedAsyncioTestCase):
    async def test_find_services_queries_service_class_with_projection(self):
        repo, itop, schema = _make_repo()

        items = await repo.find_services("SELECT Service")

        itop.schema.assert_called_once_with("Service")
        schema.find.assert_awaited_once_with("SELECT Service", projection=["id", "name", "description"])
        self.assertEqual(items, [CatalogItem(id="5", name="IT Support", description="General IT")])

    async def test_find_subcategories_queries_subcategory_class(self):
        repo, itop, _ = _make_repo()

        await repo.find_subcategories("SELECT ServiceSubcategory")

        itop.schema.assert_called_once_with("ServiceSubcategory")

    async def test_get_service_returns_item(self):
        repo, _, schema = _make_repo()

        item = await repo.get_service("5")

        schema.find_one.assert_awaited_once_with({"id": "5"}, projection=["id", "name", "description"])
        self.assertEqual(item, CatalogItem(id="5", name="IT Support", description="General IT"))

    async def test_get_service_returns_none_when_missing(self):
        repo, _, schema = _make_repo()
        schema.find_one.return_value = None

        self.assertIsNone(await repo.get_service("999"))

    async def test_missing_description_becomes_empty(self):
        repo, _, schema = _make_repo()
        schema.find_one.return_value = {"id": "5", "name": "IT", "description": None}

        item = await repo.get_service("5")

        self.assertEqual(item.description, "")


if __name__ == "__main__":
    unittest.main()
