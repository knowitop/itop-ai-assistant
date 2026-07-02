import logging

from config import TicketMappingConfig
from domain.ticket import LogEntry, Ticket
from itop_client import Itop

logger = logging.getLogger(__name__)


class TicketRepository:
    """Translates between the semantic Ticket model and raw iTop attributes.

    All knowledge of the customer's iTop datamodel (attribute names, absent
    fields per class) lives in the `ticket_mapping` config — processing code
    works with semantic field names only.
    """

    def __init__(self, itop: Itop, mapping: TicketMappingConfig):
        self._itop = itop
        self.mapping = mapping
        self._ai_person_name: str | None = None

    async def fetch(self, obj_class: str, ticket_id: str) -> Ticket | None:
        raw = await self._itop.schema(obj_class).find_one({"id": ticket_id})
        if raw is None:
            return None
        return self.to_ticket(obj_class, raw)

    def to_ticket(self, obj_class: str, raw: dict) -> Ticket:
        fields = self.mapping.for_class(obj_class)

        def attr(semantic: str):
            attr_code = fields.get(semantic)
            return raw.get(attr_code) if attr_code else None

        log_raw = attr("public_log") or {}
        entries = [LogEntry(user_login=e["user_login"], message=e["message"]) for e in (log_raw.get("entries") or [])]

        return Ticket(
            obj_class=obj_class,
            id=str(raw["id"]),
            ref=attr("ref"),
            title=attr("title") or "",
            description=attr("description") or "",
            status=attr("status") or "",
            service_id=str(attr("service_id") or "0"),
            subcategory_id=str(attr("subcategory_id") or "0"),
            caller_name=attr("caller_name") or "",
            org_id=attr("org_id"),
            request_type=attr("request_type"),
            public_log=entries,
        )

    async def set_fields(self, ticket: Ticket, fields: dict[str, str]) -> None:
        """Update ticket attributes in iTop; `fields` is keyed by semantic names."""
        mapped = self.mapping.for_class(ticket.obj_class)
        raw_fields = {}
        for semantic, value in fields.items():
            attr_code = mapped.get(semantic)
            if attr_code is None:
                logger.warning(f"{ticket.label}: field {semantic!r} is not mapped for {ticket.obj_class}, skipping")
                continue
            raw_fields[attr_code] = value
        if raw_fields:
            await self._itop.schema(ticket.obj_class).update({"id": ticket.id}, raw_fields)

    async def append_public_log(self, ticket: Ticket, message: str) -> None:
        await self._append_log(ticket, "public_log", message)

    async def append_private_log(self, ticket: Ticket, message: str) -> None:
        await self._append_log(ticket, "private_log", message)

    async def _append_log(self, ticket: Ticket, semantic_log: str, message: str) -> None:
        attr_code = self.mapping.for_class(ticket.obj_class).get(semantic_log)
        if attr_code is None:
            raise ValueError(f"{semantic_log!r} is not mapped for class {ticket.obj_class}")
        await self._itop.schema(ticket.obj_class).update(
            {"id": ticket.id},
            {attr_code: {"add_item": {"message": message, "format": "text"}}},
        )

    async def get_ai_person_name(self) -> str:
        """Friendly name of the AI service account. Cached for the process lifetime."""
        if self._ai_person_name is None:
            person = await self._itop.schema("Person").find_one({"id": ("=", ":current_contact_id")})
            self._ai_person_name = person["friendlyname"]
        return self._ai_person_name
