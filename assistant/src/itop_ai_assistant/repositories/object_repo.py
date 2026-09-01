"""One repository for every family of iTop objects.

What used to be written once per family — a projection, a hand-written
translation of raw attributes into a model, a reconciliation probe, a writer —
is here once, driven by the family's `Schema` and the deployment's mapping
section. A new family costs a declaration, not a class.

All knowledge of the customer's iTop datamodel (attribute names, absent fields
per class) stays in the mapping; processing code works with semantic field
names only. **How** a value is read is the field's `kind`, so nothing here
decides per field what to do with a raw string — that decision was where
`to_ticket` quietly turned an unmapped attribute into an empty one.

Also where the installation's writes are counted (REQ-009 R3). Here rather
than in the module that meant them, for the reason the dry run is enforced
here and not there: a rule every new module has to remember is a rule the
first forgetful module breaks, and it breaks silently — as "that customer
somehow asks no questions". What the counters name is therefore the write that
happened; which counter a log append belongs to is the caller's word, because
"a question to the requester" and "a note between engineers" are distinctions
the ticket family makes and this class does not.

In the dry run the write is dropped below this point (`Itop.read_only`), so
what is counted then is the intent. Deliberately: an installation running a
week in dry run must not look like a dead one, and the document carries the
mode alongside the counters.
"""

import logging
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import datetime
from typing import Any, Protocol

from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.identity import ObjectIdentity
from itop_ai_assistant.domain.object_view import LogEntry, ObjectView
from itop_ai_assistant.domain.schema import FieldKind, FieldSpec, Role, Schema
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.repositories.valuemap import attribute, extract, normalize_value
from itop_ai_assistant.state.counters import Counter, DailyCounters
from itop_ai_assistant.util.text import ITOP_DATETIME_FORMAT, parse_itop_dt

logger = logging.getLogger(__name__)


class ClassMapping(Protocol):
    """The mapping section of one family, as this repository reads it.

    One method, because per-class overrides are all a repository ever asks a
    mapping for — a family with a single class answers the same table for
    every argument.
    """

    def for_class(self, obj_class: str) -> dict[str, str | None]: ...


def _parse_log(raw: Any, requester: str = "") -> list[LogEntry]:
    """A case log's entries, each already marked as the requester's or not.

    Marked here because here is the only place that knows both: the log and
    the field naming the requester are attributes of the same object, read in
    the same row. Downstream a log entry is a line with a flag on it, and
    nothing has to know what a ticket is to label a conversation.
    """
    entries = (raw or {}).get("entries") or [] if isinstance(raw, Mapping) else []
    return [
        LogEntry(
            user_login=e["user_login"],
            message=e["message"],
            is_requester=bool(requester) and e["user_login"] == requester,
        )
        for e in entries
    ]


#: How each kind reads a raw iTop value. The whole of "what to do with what
#: iTop returned" — a field gets no say beyond declaring what it is. A case log
#: is read through here too, but `to_view` calls it directly, with the
#: requester to mark the entries against.
_READERS: Mapping[FieldKind, Callable[[Any], Any]] = {
    FieldKind.TEXT: lambda raw: raw or "",
    FieldKind.ID: normalize_value,
    FieldKind.ENUM: lambda raw: raw or "",
    FieldKind.DATETIME: parse_itop_dt,
    FieldKind.LOG: _parse_log,
}


class ObjectRepository:
    """Translates between one family's semantic fields and raw iTop attributes."""

    def __init__(self, itop: Itop, schema: Schema, mapping: ClassMapping, counters: DailyCounters):
        self._itop = itop
        self.schema = schema
        self.mapping = mapping
        self._counters = counters

    def attributes(self, obj_class: str) -> dict[str, str | None]:
        """Semantic field → iTop attribute code for this class, `None` where
        the deployment says the attribute does not exist."""
        return self.mapping.for_class(obj_class)

    def unmapped(self, obj_class: str, names: Collection[str]) -> tuple[str, ...]:
        """Which of `names` this deployment does not map for this class.

        What a module asks before it starts reading fields it cannot do
        without: an unmapped field reads as empty, and an empty caller name
        mislabels a whole conversation rather than failing.
        """
        attrs = self.attributes(obj_class)
        return tuple(name for name in names if not attrs.get(name))

    async def read(self, obj_class: str, obj_id: str, *, exclude: Collection[str] = ()) -> ObjectView | None:
        """One object by id, or None if it is gone.

        Asks iTop only for the attributes the mapping reads — fetching
        everything ("*+") drags in link sets and case logs for no reason —
        minus whatever `exclude` names.
        """
        raw = await self._itop.schema(obj_class).find_one(
            {"id": obj_id}, projection=["id", *self._projection(obj_class, exclude=exclude)]
        )
        return None if raw is None else self.to_view(obj_class, raw)

    def to_view(self, obj_class: str, raw: dict) -> ObjectView:
        """One raw iTop row as normalized semantic values.

        A field the row does not carry — unmapped, or left out of the
        projection — is absent from the view rather than empty, so a typed
        model built over it falls back to its own default.
        """
        attrs = self.attributes(obj_class)
        requester = self._requester(attrs, raw)
        values: dict[str, Any] = {}
        for spec in self.schema.fields:
            attr_code = attrs.get(spec.name)
            if not attr_code:
                continue
            source = attribute(attr_code) if spec.multi else attr_code
            if source not in raw:
                continue
            if spec.multi:
                values[spec.name] = extract(raw, attr_code)
            elif spec.kind is FieldKind.LOG:
                values[spec.name] = _parse_log(raw[source], requester)
            else:
                values[spec.name] = _READERS[spec.kind](raw[source])
        return ObjectView(schema=self.schema, obj_class=obj_class, id=str(raw["id"]), values=values)

    def _requester(self, attrs: Mapping[str, str | None], raw: Mapping[str, Any]) -> str:
        """Who this object was raised for, as the case log names its author.

        Read before the fields, not as one of them: a log entry is marked
        while it is parsed, and declaration order must not decide whether the
        mark lands.
        """
        spec = self.schema.one(Role.REQUESTER)
        attr_code = attrs.get(spec.name) if spec else None
        return str(raw.get(attr_code) or "") if attr_code else ""

    def _projection(self, obj_class: str, *, exclude: Collection[str] = ()) -> list[str]:
        """Attributes a read of this class asks iTop for — one projection per
        class, not one per call site.

        A list-valued field is mapped as `<link set>:<id attribute>`, and it is
        the link set alone that iTop is asked for (`repositories/valuemap.py`).
        """
        multi = self.schema.multi_names()
        return list(
            dict.fromkeys(
                attribute(attr) if semantic in multi else attr
                for semantic, attr in self.attributes(obj_class).items()
                if attr and semantic not in exclude
            )
        )

    async def find_modified_since(
        self,
        obj_class: str,
        since: datetime | None,
        *,
        page: int,
        page_size: int,
        exclude: Collection[str] = (),
    ) -> list[ObjectView]:
        """One page of objects modified at/after `since` (None = full scan).

        Deliberately no relevance predicate: an object that left the indexable
        scope must still be seen so its chunks can be deleted. iTop OQL has no
        ORDER BY, so pages come in internal order — callers must consume all
        pages before trusting a cursor built from the results.

        A family whose modification date is not mapped always does a full scan
        and says so: stock iTop's `FAQ` class carries no date attribute at all,
        and refusing here would make an optional field mandatory for every
        family. The hash-guard (`vector/use_cases/indexer.py`) keeps that
        cheap — unchanged chunks are neither re-embedded nor rewritten.

        `exclude` leaves fields out of the projection — the private log is not
        something intake has any business fetching.
        """
        cursor = self._cursor_attribute(obj_class, first_page=page == 1)
        query = {} if cursor is None or since is None else {cursor: (">=", since.strftime(ITOP_DATETIME_FORMAT))}
        rows = await self._itop.schema(obj_class).find(
            query,
            projection=["id", *self._projection(obj_class, exclude=exclude)],
            limit=str(page_size),
            page=str(page),
        )
        return [self.to_view(obj_class, row) for row in rows]

    def _cursor_attribute(self, obj_class: str, *, first_page: bool) -> str | None:
        """The attribute a cursor compares against, `None` where the family
        has no modification date at all.

        Said once per walk, not once per page: pages of one walk start at 1,
        and a hundred identical lines is how a warning stops being read. The
        repository itself keeps no memory of having said it — it is stateless,
        a client and a mapping (`.claude/rules/itop.md`).
        """
        spec = self.schema.one(Role.MODIFIED_AT)
        attr_code = self.attributes(obj_class).get(spec.name) if spec else None
        if attr_code is None and first_page:
            logger.warning(
                f"{self.schema.name}: no modification date mapped for {obj_class} — every pass reads every object"
            )
        return attr_code

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        """Which of the given ids still exist in iTop (reconciliation probe)."""
        if not ids:
            return set()
        id_list = ",".join(str(int(i)) for i in ids)
        rows = await self._itop.schema(obj_class).find(f"SELECT {obj_class} WHERE id IN ({id_list})", projection=["id"])
        return {int(row["id"]) for row in rows}

    async def set_fields(self, obj: ObjectIdentity, fields: dict[str, str]) -> None:
        """Update attributes in iTop; `fields` is keyed by semantic names.

        A field the family declares read-only raises: iTop computes it, so the
        write would be a code mistake and would fail there instead. A field
        this deployment does not map only warns — that is a datamodel it does
        not have, not a bug.
        """
        attrs = self.attributes(obj.obj_class)
        raw_fields = {}
        for semantic, value in fields.items():
            spec = self._writable(semantic)
            attr_code = attrs.get(spec.name)
            if attr_code is None:
                logger.warning(f"{obj}: field {semantic!r} is not mapped for {obj.obj_class}, skipping")
                continue
            raw_fields[attr_code] = value
        if raw_fields:
            await self._itop.schema(obj.obj_class).update({"id": obj.obj_id}, raw_fields)
            await self._counters.bump(Counter.ITOP_FIELD_UPDATE)

    def _writable(self, semantic: str) -> FieldSpec:
        spec = self.schema.spec(semantic)
        if spec is None:
            raise ValueError(f"{self.schema.name!r} declares no field {semantic!r}")
        if not spec.writable:
            raise ValueError(f"{self.schema.name}.{semantic} is read-only — iTop does not let it be set")
        return spec

    async def append_log(self, obj: ObjectIdentity, name: str, message: str, *, counter: Counter) -> None:
        """Append one entry to a case log. Case logs are append-only in iTop —
        `add_item`, never a rewrite."""
        spec = self.schema.spec(name)
        if spec is None or spec.kind is not FieldKind.LOG:
            raise ValueError(f"{self.schema.name}.{name} is not a case log")
        attr_code = self.attributes(obj.obj_class).get(name)
        if attr_code is None:
            raise ValueError(f"{name!r} is not mapped for class {obj.obj_class}")
        await self._itop.schema(obj.obj_class).update(
            {"id": obj.obj_id},
            {attr_code: {"add_item": {"message": message, "format": "text"}}},
        )
        await self._counters.bump(counter)


# The shape of "a way to fetch a fresh repository for one family" — declared
# once here so a caller that needs one imports this instead of redeclaring the
# same callable. Named `*Provider`, not `*Factory`: it never constructs one, it
# fetches one already built by `ItopRepositories` (the actual factory) and
# projects it out of the `RepositorySet`.
type ObjectRepositoryProvider = Callable[[], Awaitable[ObjectRepository]]

# The same, for a repository bound to a given principal. A separate type rather
# than an optional argument on the one above (TASK-032): a holder of one of
# these can only ever ask "as this person", a holder of the other can only ever
# ask as the service account, and neither can be handed where the other is
# expected.
type ObjectRepositoryForPrincipal = Callable[[Principal], Awaitable[ObjectRepository]]
