"""Semantic ticket model, decoupled from iTop attribute names.

Raw iTop attributes are translated into this model by
`repositories/ticket.py::TicketRepository` according to the `ticket_mapping`
config section — customer datamodel customizations are handled there, never in
processing code.
"""

from datetime import datetime

from pydantic import BaseModel

from itop_ai_assistant.domain.identity import ObjectIdentifiable, ObjectIdentity


class LogEntry(BaseModel):
    user_login: str
    message: str


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

    @property
    def has_service(self) -> bool:
        return self.service_id is not None

    @property
    def has_subcategory(self) -> bool:
        return self.subcategory_id is not None
