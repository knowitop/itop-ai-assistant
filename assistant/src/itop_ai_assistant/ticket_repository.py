import logging
from datetime import datetime

from itop_ai_assistant.config import TicketMappingConfig
from itop_ai_assistant.domain.ticket import LogEntry, Ticket
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.text_utils import ITOP_DATETIME_FORMAT, bind_oql, parse_itop_dt

logger = logging.getLogger(__name__)


def _parse_log_entries(log_raw: dict | None) -> list[LogEntry]:
    return [LogEntry(user_login=e["user_login"], message=e["message"]) for e in ((log_raw or {}).get("entries") or [])]


class TicketRepository:
    """Translates between the semantic Ticket model and raw iTop attributes.

    All knowledge of the customer's iTop datamodel (attribute names, absent
    fields per class) lives in the `ticket_mapping` config — processing code
    works with semantic field names only.
    """

    def __init__(self, itop: Itop, mapping: TicketMappingConfig):
        self._itop = itop
        self.mapping = mapping

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

        entries = _parse_log_entries(attr("public_log"))
        private_entries = _parse_log_entries(attr("private_log"))

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
            private_log=private_entries,
            solution=attr("solution") or "",
            last_update=parse_itop_dt(attr("last_update")),
            start_date=parse_itop_dt(attr("start_date")),
        )

    async def find_modified_since(
        self,
        obj_class: str,
        since: datetime | None,  # TODO: это не должно быть None, раз метод называется find_modified_since
        *,
        page: int,
        page_size: int,
        include_private_log: bool = False,  # TODO: это порнография. Нужен нормальный механизм включения/исключия/ленивой_загрузки полей объектов.
    ) -> list[Ticket]:
        """One page of tickets modified at/after `since` (None = full scan).

        Deliberately no status predicate: a ticket that left the indexable
        statuses must still be seen so its chunks can be deleted. iTop OQL has
        no ORDER BY, so pages come in internal order — callers must consume
        all pages before trusting a cursor built from the results.

        `include_private_log` opts into the field `fetch()` always excludes —
        only the vector sweep needs it, intake never does.
        """
        fields = self.mapping.for_class(obj_class)
        last_update_attr = fields.get("last_update")
        if last_update_attr is None:
            raise ValueError(f"'last_update' is not mapped for class {obj_class}")
        if since is None:
            query = f"SELECT {obj_class}"
        else:
            # TODO: почему мы тут пишем OQL? Нет в библиотеке метода, куда условия фильтра попдают как параметры?
            #  Вот так должно работать по идее: query = {last_update_attr: ('>=', since.strftime(ITOP_DATETIME_FORMAT))}
            query = bind_oql(
                f"SELECT {obj_class} WHERE {last_update_attr} >= :this->since",
                {"since": since.strftime(ITOP_DATETIME_FORMAT)},
            )
        excluded = set() if include_private_log else {"private_log"}
        attrs = [attr for semantic, attr in fields.items() if attr and semantic not in excluded]
        rows = await self._itop.schema(obj_class).find(
            query, projection=["id", *attrs], limit=str(page_size), page=str(page)
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
