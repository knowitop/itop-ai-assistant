"""Adapter between the semantic service-catalog model and the iTop REST API."""

from domain.catalog import CatalogItem
from itop_client import Itop

_SERVICE_CLASS = "Service"
_SUBCATEGORY_CLASS = "ServiceSubcategory"
_PROJECTION = ["id", "name", "description"]


class CatalogRepository:
    """Reads Services and ServiceSubcategories as semantic CatalogItems.

    Class and attribute names are fixed: unlike tickets, the service catalog
    classes are practically never customized in iTop datamodels. OQL filters
    are passed through as-is (they are config themselves).
    """

    def __init__(self, itop: Itop):
        self._itop = itop

    async def find_services(self, oql: str) -> list[CatalogItem]:
        return await self._find(_SERVICE_CLASS, oql)

    async def find_subcategories(self, oql: str) -> list[CatalogItem]:
        return await self._find(_SUBCATEGORY_CLASS, oql)

    async def get_service(self, service_id: str) -> CatalogItem | None:
        return await self._get(_SERVICE_CLASS, service_id)

    async def get_subcategory(self, subcategory_id: str) -> CatalogItem | None:
        return await self._get(_SUBCATEGORY_CLASS, subcategory_id)

    async def _find(self, obj_class: str, oql: str) -> list[CatalogItem]:
        rows = await self._itop.schema(obj_class).find(oql, projection=_PROJECTION)
        return [self._to_item(row) for row in rows]

    async def _get(self, obj_class: str, obj_id: str) -> CatalogItem | None:
        row = await self._itop.schema(obj_class).find_one({"id": obj_id}, projection=_PROJECTION)
        return self._to_item(row) if row else None

    @staticmethod
    def _to_item(row: dict) -> CatalogItem:
        return CatalogItem(
            id=str(row["id"]),
            name=row.get("name") or "",
            description=row.get("description") or "",
        )
