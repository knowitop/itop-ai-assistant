import logging
from datetime import UTC, datetime

from config import TicketMappingConfig
from domain.ticket import LogEntry, Ticket
from itop_client import Itop
from text_utils import bind_oql

logger = logging.getLogger(__name__)

ITOP_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_dt(value) -> datetime | None:
    """Parse an iTop timestamp, tolerating garbage (None on failure).

    iTop returns naive strings in the *server's local time*. We tag them UTC
    purely as a label — timestamptz columns require aware datetimes — and only
    ever compare them with other iTop timestamps, so the offset is irrelevant.
    Do not "fix" this by converting to real UTC: there is no reliable way to
    know the iTop server's zone from here, and consistency is all we need.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, ITOP_DATETIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


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
        # Request only the attributes the mapping reads — fetching everything
        # ("*+") drags in link sets and the private log for no reason.
        fields = self.mapping.for_class(obj_class)
        attrs = [attr for semantic, attr in fields.items() if attr and semantic != "private_log"]
        raw = await self._itop.schema(obj_class).find_one({"id": ticket_id}, projection=["id", *attrs])
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
            solution=attr("solution") or "",
            last_update=_parse_dt(attr("last_update")),
            created_at=_parse_dt(attr("created_at")),
        )

    async def find_modified_since(
        self, obj_class: str, since: datetime | None, *, page: int, page_size: int
    ) -> list[Ticket]:
        """One page of tickets modified at/after `since` (None = full scan).

        Deliberately no status predicate: a ticket that left the indexable
        statuses must still be seen so its chunks can be deleted. iTop OQL has
        no ORDER BY, so pages come in internal order — callers must consume
        all pages before trusting a cursor built from the results.
        """
        fields = self.mapping.for_class(obj_class)
        last_update_attr = fields.get("last_update")
        if last_update_attr is None:
            raise ValueError(f"'last_update' is not mapped for class {obj_class}")
        if since is None:
            oql = f"SELECT {obj_class}"
        else:
            oql = bind_oql(
                f"SELECT {obj_class} WHERE {last_update_attr} >= :this->since",
                {"since": since.strftime(ITOP_DATETIME_FORMAT)},
            )
        attrs = [attr for semantic, attr in fields.items() if attr and semantic != "private_log"]
        rows = await self._itop.schema(obj_class).find(
            oql, projection=["id", *attrs], limit=str(page_size), page=str(page)
        )
        return [self.to_ticket(obj_class, row) for row in rows]

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        """Which of the given ids still exist in iTop (reconciliation probe)."""
        if not ids:
            return set()
        id_list = ",".join(str(int(i)) for i in ids)
        rows = await self._itop.schema(obj_class).find(f"SELECT {obj_class} WHERE id IN ({id_list})", projection=["id"])
        return {int(row["id"]) for row in rows}

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
