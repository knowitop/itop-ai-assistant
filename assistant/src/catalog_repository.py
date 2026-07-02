"""Adapter between the semantic service-catalog models and the iTop REST API."""

from domain.catalog import Service, ServiceSubcategory
from itop_client import Itop

_SERVICE_CLASS = "Service"
_SERVICE_PROJECTION = ["id", "name", "description"]
_SUBCATEGORY_CLASS = "ServiceSubcategory"
_SUBCATEGORY_PROJECTION = ["id", "name", "description", "service_id"]


class CatalogRepository:
    """Reads Services and ServiceSubcategories as semantic models.

    Class and attribute names are fixed: unlike tickets, the service catalog
    classes are practically never customized in iTop datamodels. OQL filters
    are passed through as-is (they are config themselves).
    """

    def __init__(self, itop: Itop):
        self._itop = itop

    async def find_services(self, oql: str) -> list[Service]:
        rows = await self._itop.schema(_SERVICE_CLASS).find(oql, projection=_SERVICE_PROJECTION)
        return [self._to_service(row) for row in rows]

    async def find_subcategories(self, oql: str) -> list[ServiceSubcategory]:
        rows = await self._itop.schema(_SUBCATEGORY_CLASS).find(oql, projection=_SUBCATEGORY_PROJECTION)
        return [self._to_subcategory(row) for row in rows]

    async def get_service(self, service_id: str) -> Service | None:
        row = await self._itop.schema(_SERVICE_CLASS).find_one({"id": service_id}, projection=_SERVICE_PROJECTION)
        return self._to_service(row) if row else None

    async def get_subcategory(self, subcategory_id: str) -> ServiceSubcategory | None:
        row = await self._itop.schema(_SUBCATEGORY_CLASS).find_one(
            {"id": subcategory_id}, projection=_SUBCATEGORY_PROJECTION
        )
        return self._to_subcategory(row) if row else None

    @staticmethod
    def _to_service(row: dict) -> Service:
        return Service(
            id=str(row["id"]),
            name=row.get("name") or "",
            description=row.get("description") or "",
        )

    @staticmethod
    def _to_subcategory(row: dict) -> ServiceSubcategory:
        return ServiceSubcategory(
            id=str(row["id"]),
            name=row.get("name") or "",
            description=row.get("description") or "",
            service_id=str(row["service_id"]),
        )
