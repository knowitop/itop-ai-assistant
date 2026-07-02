"""Semantic ticket model, decoupled from iTop attribute names.

Raw iTop attributes are translated into this model by `itop.repository.
TicketRepository` according to the `ticket_mapping` config section — customer
datamodel customizations are handled there, never in processing code.
"""

from pydantic import BaseModel


class LogEntry(BaseModel):
    user_login: str
    message: str


class Ticket(BaseModel):
    obj_class: str  # iTop final class, e.g. "UserRequest" / "Incident"
    id: str
    ref: str | None = None
    title: str = ""
    description: str = ""  # raw HTML as stored in iTop
    status: str = ""
    service_id: str = "0"
    subcategory_id: str = "0"
    caller_name: str = ""
    org_id: str | None = None
    request_type: str | None = None
    public_log: list[LogEntry] = []

    @property
    def label(self) -> str:
        return f"{self.obj_class}::{self.id}"

    @property
    def has_service(self) -> bool:
        return _is_set(self.service_id)

    @property
    def has_subcategory(self) -> bool:
        return _is_set(self.subcategory_id)


def _is_set(external_key: str | None) -> bool:
    """iTop returns "0" for unset external keys."""
    try:
        return bool(int(external_key or 0))
    except ValueError:
        return False
