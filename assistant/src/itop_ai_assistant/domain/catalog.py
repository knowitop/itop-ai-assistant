"""Semantic service-catalog models, decoupled from iTop attribute names."""

from pydantic import BaseModel

from itop_ai_assistant.domain.identity import ObjectIdentifiable, ObjectIdentity


class Service(BaseModel, ObjectIdentifiable):
    id: str
    name: str = ""
    description: str = ""

    @property
    def identity(self) -> ObjectIdentity:
        return ObjectIdentity(obj_class="Service", id=self.id)


class ServiceSubcategory(BaseModel, ObjectIdentifiable):
    id: str
    name: str = ""
    description: str = ""
    # Mandatory external key in iTop: a subcategory always belongs to a service
    service_id: str

    @property
    def identity(self) -> ObjectIdentity:
        return ObjectIdentity(obj_class="ServiceSubcategory", id=self.id)
