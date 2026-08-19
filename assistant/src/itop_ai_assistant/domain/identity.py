"""A unique, class-qualified identity for one iTop object — everything the
platform needs to log, journal or start a run over it, without loading the
rest of it.

The webhook body and the manual `/api/modules/<module>/process` request parse
straight into `ObjectIdentity` — the `class` alias exists because that is the
JSON key iTop and this API use, and `class` is a Python keyword, not because
this type carries any of iTop's own conditions (contrast `repositories/ticket.py`'s
"0 means unset").
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class ObjectIdentity(BaseModel):
    obj_class: str = Field(alias="class")
    obj_id: str = Field(alias="id")

    model_config = {"populate_by_name": True}

    def __str__(self) -> str:
        return f"{self.obj_class}::{self.obj_id}"


class ObjectIdentifiable(ABC):
    """Something with the identity of one iTop object alongside its own data.

    Every domain model that names one implements this — callers hold the
    `ObjectIdentity` returned here instead of recomposing `Class::Id` by hand,
    and stringify it only where a plain `str` is genuinely required (a Redis
    key, a journal field).
    """

    @property
    @abstractmethod
    def identity(self) -> ObjectIdentity: ...
