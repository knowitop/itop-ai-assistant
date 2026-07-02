"""Semantic service-catalog model, decoupled from iTop attribute names."""

from pydantic import BaseModel


class CatalogItem(BaseModel):
    """A selectable catalog option: a Service or a ServiceSubcategory."""

    id: str
    name: str = ""
    description: str = ""
