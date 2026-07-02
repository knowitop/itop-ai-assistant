"""Semantic service-catalog models, decoupled from iTop attribute names."""

from pydantic import BaseModel


class Service(BaseModel):
    id: str
    name: str = ""
    description: str = ""


class ServiceSubcategory(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    # Mandatory external key in iTop: a subcategory always belongs to a service
    service_id: str
