"""Semantic ticket model, decoupled from iTop attribute names.

Built over an `ObjectView` by `repositories/ticket.py::to_ticket`: what the
fields are and how they read is `domain/tickets_schema.py`, where the values
come from is the `mappings` config section, and neither is decided here.
This class exists because `intake` reads these names as identifiers and wants
mypy to check them — a family no module reads by name needs no such class.
"""

from datetime import datetime

from pydantic import BaseModel

from itop_ai_assistant.domain.identity import ObjectIdentifiable, ObjectIdentity
from itop_ai_assistant.domain.object_view import LogEntry

__all__ = ["LogEntry", "Ticket"]


class Ticket(BaseModel, ObjectIdentifiable):
    obj_class: str  # iTop final class, e.g. "UserRequest" / "Incident"
    id: str
    ref: str | None = None
    title: str = ""
    description: str = ""  # raw HTML as stored in iTop
    status: str = ""
    service_id: str | None = None
    service_name: str = ""
    subcategory_id: str | None = None
    subcategory_name: str = ""
    caller_name: str = ""
    org_id: str | None = None
    request_type: str | None = None
    public_log: list[LogEntry] = []
    private_log: list[LogEntry] = []
    solution: str = ""  # raw HTML, filled on resolved/closed tickets
    # iTop timestamps carry a nominal UTC tzinfo — see ticket_repository._parse_dt
    last_update: datetime | None = None
    start_date: datetime | None = None  # named as in iTop — creation time, semantic mapping key stays "created_at"

    @property
    def identity(self) -> ObjectIdentity:
        return ObjectIdentity(obj_class=self.obj_class, obj_id=self.id)
