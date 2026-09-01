"""One iTop object as normalized semantic values — what generic code reads.

The repository resolves a family's mapping against a raw iTop row once and
hands back this; nothing downstream sees an attribute code, a link set or an
iTop "0". Access is by semantic name and by kind: `text()` on a text field,
`identifier()` on an id, and asking for the wrong one is an error rather than
a surprising value.

**An unmapped field is absent, not empty.** A deployment whose datamodel has
no `request_type` leaves it out of `values` entirely, so a typed model built
over the view falls back to its own default — the same rule multi-valued
fields have always followed. Reading such a field through an accessor still
answers: an unset text is `""`, an unset identifier is `None`. What the
accessors refuse is a name the family never declared, which is a mistake in
the code or in a saved config, not a fact about the object.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from itop_ai_assistant.domain.identity import ObjectIdentifiable, ObjectIdentity
from itop_ai_assistant.domain.schema import FieldKind, Role, Schema


class LogEntry(BaseModel):
    """One entry of an iTop case log.

    `is_requester` is set where the log is read, by the one party that knows
    who the requester is (`Role.REQUESTER`, mapped next to the log itself).
    Downstream — the chunker labelling a conversation — only ever sees the
    flag, and needs to know nothing about tickets.
    """

    user_login: str
    message: str
    is_requester: bool = False


@dataclass(frozen=True)
class ObjectView(ObjectIdentifiable):
    """One object of one family: its identity and its mapped values."""

    schema: Schema
    obj_class: str
    id: str
    #: Semantic name → normalized value, for the mapped fields that were read.
    values: Mapping[str, Any]

    @property
    def identity(self) -> ObjectIdentity:
        return ObjectIdentity(obj_class=self.obj_class, obj_id=self.id)

    def text(self, name: str) -> str:
        value = self._value(name, FieldKind.TEXT)
        return "" if value is None else str(value)

    def identifier(self, name: str) -> str | None:
        value = self._value(name, FieldKind.ID)
        return None if value is None else str(value)

    def identifiers(self, name: str) -> tuple[str, ...]:
        """A multi-valued id field's values, or the one value of a single one —
        an organization reads the same way whether the datamodel holds one or
        a link set of them (ADR-033)."""
        value = self._value(name, FieldKind.ID)
        if value is None:
            return ()
        if isinstance(value, Sequence) and not isinstance(value, str):
            return tuple(str(item) for item in value)
        return (str(value),)

    def state(self, name: str) -> str:
        value = self._value(name, FieldKind.ENUM)
        return "" if value is None else str(value)

    def state_of(self, role: Role) -> str:
        """The value of the field carrying a singular role, `""` if the family
        declares none — how generic code asks for a meaning instead of a name.
        """
        spec = self.schema.one(role)
        return self.state(spec.name) if spec else ""

    def moment_of(self, role: Role) -> datetime | None:
        """The same for a timestamp — `None` where the family has no such
        field, which is what stock iTop's `FAQ` class looks like."""
        spec = self.schema.one(role)
        return self.moment(spec.name) if spec else None

    def moment(self, name: str) -> datetime | None:
        value = self._value(name, FieldKind.DATETIME)
        return value if isinstance(value, datetime) else None

    def log(self, name: str) -> list[LogEntry]:
        value = self._value(name, FieldKind.LOG)
        return list(value) if value else []

    def _value(self, name: str, kind: FieldKind) -> Any:
        spec = self.schema.spec(name)
        if spec is None:
            raise KeyError(f"{self.schema.name!r} declares no field {name!r}")
        if spec.kind is not kind:
            raise TypeError(f"field {name!r} is a {spec.kind.value!r}, read as a {kind.value!r}")
        return self.values.get(name)
