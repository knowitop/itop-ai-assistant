import logging
from collections.abc import Awaitable, Callable, Collection
from datetime import datetime

from itop_ai_assistant.config import TicketMappingConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.ticket import LogEntry, Ticket
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.repositories.valuemap import attribute, read_lists
from itop_ai_assistant.state.counters import Counter, DailyCounters
from itop_ai_assistant.util.text import ITOP_DATETIME_FORMAT, parse_itop_dt

logger = logging.getLogger(__name__)

# Semantic fields of a ticket that hold a list of values, per the schema.
_LIST_FIELDS = TICKET_SCHEMA.multi_names()


def _parse_log_entries(log_raw: dict | None) -> list[LogEntry]:
    return [LogEntry(user_login=e["user_login"], message=e["message"]) for e in ((log_raw or {}).get("entries") or [])]


def _external_key(raw: str | None) -> str | None:
    """iTop returns "0" for an unset external key."""
    try:
        return str(raw) if int(raw or 0) else None
    except ValueError:
        return None


class TicketRepository:
    """Translates between the semantic Ticket model and raw iTop attributes.

    All knowledge of the customer's iTop datamodel (attribute names, absent
    fields per class) lives in the `ticket_mapping` config — processing code
    works with semantic field names only.

    Also where the installation's writes are counted (REQ-009 R3). Here rather
    than in the module that meant them, for the reason the dry run is enforced
    here and not there: a rule every new module has to remember is a rule the
    first forgetful module breaks, and it breaks silently — as "that customer
    somehow asks no questions". What the counters name is therefore the write
    that happened, not what it meant; the reading of a public comment as "a
    question the intake module asked" belongs to the document builder, in one
    place.

    In the dry run the write is dropped below this point (`Itop.read_only`),
    so what is counted then is the intent. Deliberately: an installation
    running a week in dry run must not look like a dead one, and the document
    carries the mode alongside the counters.
    """

    def __init__(self, itop: Itop, mapping: TicketMappingConfig, counters: DailyCounters):
        self._itop = itop
        self.mapping = mapping
        self._counters = counters

    async def fetch(self, obj_class: str, ticket_id: str) -> Ticket | None:
        # Request only the attributes the mapping reads — fetching everything
        # ("*+") drags in link sets and the private log for no reason.
        attrs = self._projection(obj_class, excluded={"private_log"})
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
            service_id=_external_key(attr("service_id")),
            subcategory_id=_external_key(attr("subcategory_id")),
            service_name=attr("service_name") or "",
            subcategory_name=attr("subcategory_name") or "",
            caller_name=attr("caller_name") or "",
            org_id=attr("org_id"),
            request_type=attr("request_type"),
            public_log=entries,
            private_log=private_entries,
            solution=attr("solution") or "",
            last_update=parse_itop_dt(attr("last_update")),
            start_date=parse_itop_dt(attr("start_date")),
            **read_lists(raw, fields, _LIST_FIELDS),
        )

    def _projection(self, obj_class: str, *, excluded: Collection[str] = ()) -> list[str]:
        """Attributes a read of this class asks iTop for — one projection per
        class, not one per call site.

        A list-valued field is mapped as `<link set>:<id attribute>`, and it is
        the link set alone that iTop is asked for (`repositories/valuemap.py`).
        """
        fields = self.mapping.for_class(obj_class)
        return list(
            dict.fromkeys(
                attribute(attr) if semantic in _LIST_FIELDS else attr
                for semantic, attr in fields.items()
                if attr and semantic not in excluded
            )
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
        query = {} if since is None else {last_update_attr: (">=", since.strftime(ITOP_DATETIME_FORMAT))}
        attrs = self._projection(obj_class, excluded=set() if include_private_log else {"private_log"})
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
                logger.warning(f"{ticket.identity}: field {semantic!r} is not mapped for {ticket.obj_class}, skipping")
                continue
            raw_fields[attr_code] = value
        if raw_fields:
            await self._itop.schema(ticket.obj_class).update({"id": ticket.id}, raw_fields)
            await self._counters.bump(Counter.ITOP_FIELD_UPDATE)

    async def append_public_log(self, ticket: Ticket, message: str) -> None:
        await self._append_log(ticket, "public_log", message)
        await self._counters.bump(Counter.ITOP_PUBLIC_COMMENT)

    async def append_private_log(self, ticket: Ticket, message: str) -> None:
        await self._append_log(ticket, "private_log", message)
        await self._counters.bump(Counter.ITOP_PRIVATE_NOTE)

    async def _append_log(self, ticket: Ticket, semantic_log: str, message: str) -> None:
        attr_code = self.mapping.for_class(ticket.obj_class).get(semantic_log)
        if attr_code is None:
            raise ValueError(f"{semantic_log!r} is not mapped for class {ticket.obj_class}")
        await self._itop.schema(ticket.obj_class).update(
            {"id": ticket.id},
            {attr_code: {"add_item": {"message": message, "format": "text"}}},
        )


# The shape of "a way to fetch a fresh `TicketRepository`" — declared once
# here so a caller that needs one imports this instead of redeclaring the
# same `Callable[[], Awaitable[TicketRepository]]`. Named `*Provider`, not
# `*Factory`: it never constructs one, it fetches one already built by
# `ItopRepositories` (the actual factory) and projects it out of the
# `RepositorySet`.
type TicketRepositoryProvider = Callable[[], Awaitable[TicketRepository]]

# The same, for a repository bound to a given principal. A separate type
# rather than an optional argument on the one above (TASK-032): a holder of
# one of these can only ever ask "as this person", a holder of the other can
# only ever ask as the service account, and neither can be handed where the
# other is expected.
type TicketRepositoryForPrincipal = Callable[[Principal], Awaitable[TicketRepository]]
