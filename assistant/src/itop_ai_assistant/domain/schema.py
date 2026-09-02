"""A family of iTop objects described as data: what its semantic fields are,
what each of them is, and where its value comes from.

One declaration per family (`domain/tickets_schema.py`, `domain/faq_schema.py`)
replaces the parallel tuples that used to name the same fields for each
mechanism separately — and could disagree with each other, because nothing
made them agree.

**What a field says about itself, and what a consumer says about it.** The
line matters, because everything on the wrong side of it drifts. `kind` is
the field's nature: it decides how a raw iTop value is read, and it is the
same answer wherever the field is read. `roles` name what the field *is* in
the object — its modification time, its lifecycle state, an organization that
can grant access to it, part of what the object is about. A consumer then
asks for a meaning ("give me the modification time"); it does not get to
record its own intentions here. Which payload keys a vector index carries,
which of them get a Qdrant index, which fields feed which chunk fragment —
all of that is the consumer's own declaration, kept where it is consumed and
resolved through `Schema.resolve()`, so a name that no longer exists fails
loudly instead of quietly resolving to nothing.

`source` is the iTop attribute code the value is read from — the default for
a stock datamodel, which a deployment overrides in its mapping config section.
`None` means the attribute does not exist by default, and the field is empty
until a deployment maps it.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


class FieldKind(StrEnum):
    """What a field is, which is also how its raw iTop value is read.

    A closed list on purpose: a kind has to be understood by whoever consumes
    the value, so a kind nobody can read is a field nobody can use. Adding one
    is a code change in the readers, not a config entry.
    """

    #: Prose a human wrote or iTop rendered — read as a string, empty when unset.
    TEXT = "text"
    #: An identifier: an object key, an external key, a reference. "0" and ""
    #: are iTop's ways of saying "unset" and both read as no value.
    ID = "id"
    #: One value out of a set the datamodel fixes (a status, a request type).
    ENUM = "enum"
    #: An iTop timestamp.
    DATETIME = "datetime"
    #: A case log — an append-only sequence of entries, not a scalar.
    LOG = "log"


class Role(StrEnum):
    """What a field means in the object.

    Not what anyone does with it: a role is readable off the object alone, and
    stays true whether or not the vector index, the intake module or anything
    else exists. That is what makes it safe to keep here, next to the field,
    rather than in a list belonging to one of its readers.
    """

    #: Carries part of what the object is *about* — the text a person would
    #: read to understand it. Distinct from `FieldKind.TEXT`, which is only a
    #: normalization rule: a caller's display name is text and is not what the
    #: ticket is about.
    CONTENT = "content"
    #: When the object last changed.
    MODIFIED_AT = "modified_at"
    #: When the object came into being.
    CREATED_AT = "created_at"
    #: Where the object stands in its lifecycle.
    LIFECYCLE_STATE = "lifecycle_state"
    #: Names the person the object was raised for. What tells an entry of a
    #: case log apart from an engineer's reply, which is why the repository
    #: can mark the entries and nothing downstream needs to know what a
    #: ticket is.
    REQUESTER = "requester"
    #: Names an organization that can give access to the object. A candidate,
    #: not a verdict: whether it actually grants access is a deployment's
    #: statement about its own datamodel (`VectorClassConfig.acl_org_fields`).
    ORGANIZATION = "organization"


#: The kind each role requires. A role is a statement about the value, so a
#: value read the wrong way cannot carry it: a modification time that is not a
#: datetime, or an organization that is not an identifier, is a declaration
#: that would fail at the first object rather than at import.
_ROLE_KIND = {
    Role.CONTENT: FieldKind.TEXT,
    Role.MODIFIED_AT: FieldKind.DATETIME,
    Role.CREATED_AT: FieldKind.DATETIME,
    Role.LIFECYCLE_STATE: FieldKind.ENUM,
    Role.REQUESTER: FieldKind.TEXT,
    Role.ORGANIZATION: FieldKind.ID,
}

#: Roles an object has at most one field for. `ORGANIZATION` is deliberately
#: not among them — an object can be reachable through several organizations
#: (ADR-033) — and neither is `CONTENT`.
_SINGULAR_ROLES = frozenset({Role.MODIFIED_AT, Role.CREATED_AT, Role.LIFECYCLE_STATE, Role.REQUESTER})

#: Kinds for which "several values" means nothing: a case log is already a
#: sequence, and a link set of timestamps is not something iTop expresses.
_SINGLE_VALUED_KINDS = frozenset({FieldKind.DATETIME, FieldKind.LOG})


@dataclass(frozen=True)
class FieldSpec:
    """One semantic field of one family."""

    name: str
    kind: FieldKind
    #: iTop attribute code this reads by default, `None` when the stock
    #: datamodel has no such attribute. A list-valued field takes the
    #: `<link set>:<id attribute>` form (`repositories/valuemap.py`).
    source: str | None
    #: One mapping value, several iTop values. Declared here rather than read
    #: off a domain model's annotation, so a family with no typed model of its
    #: own can still say it.
    multi: bool = False
    #: Whether iTop lets this attribute be set. False for anything iTop
    #: computes (a friendly name, a reference, a timestamp) and for every
    #: field of a family nothing writes to. Case logs are appended to, never
    #: set, so a `LOG` field is never writable.
    writable: bool = False
    roles: frozenset[Role] = frozenset()
    #: What the field is, for the administrator mapping it onto their own
    #: datamodel. Empty when the name explains itself
    #: ([[ADR-025-ui-hints-travel-in-the-schema]]).
    description: str = ""
    #: True when an administrator declared this field rather than the code.
    #: The same `FieldSpec` either way — that is the point (ADR-034) — but a
    #: consumer that has to tell them apart can: no code reads such a field by
    #: name, so its value only reaches anyone by riding into the index.
    from_config: bool = False

    def __post_init__(self) -> None:
        for role in sorted(self.roles):
            if self.kind is not _ROLE_KIND[role]:
                raise ValueError(
                    f"field {self.name!r}: role {role.value!r} requires kind "
                    f"{_ROLE_KIND[role].value!r}, got {self.kind.value!r}"
                )
        if self.multi and self.kind in _SINGLE_VALUED_KINDS:
            raise ValueError(f"field {self.name!r}: a {self.kind.value!r} field cannot hold several values")
        if self.writable and self.kind is FieldKind.LOG:
            raise ValueError(f"field {self.name!r}: a case log is appended to, not set")

    def has(self, role: Role) -> bool:
        return role in self.roles


@dataclass(frozen=True)
class Schema:
    """Every semantic field of one family, and the selections its readers make.

    Validated at construction, which for the two families declared in code
    means at import: a declaration that contradicts itself fails the process
    rather than one sweep pass. The same constructor answers for a field an
    administrator declared, so the rules hold for both.
    """

    #: The family's name — "tickets", "faq". What the vector subsystem names
    #: its collection after, and what an error message calls the family.
    name: str
    fields: tuple[FieldSpec, ...]
    _by_name: dict[str, FieldSpec] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_name: dict[str, FieldSpec] = {}
        for spec in self.fields:
            if spec.name in by_name:
                raise ValueError(f"schema {self.name!r}: field {spec.name!r} is declared twice")
            by_name[spec.name] = spec
        object.__setattr__(self, "_by_name", by_name)
        for role in sorted(_SINGULAR_ROLES):
            named = self.names(role)
            if len(named) > 1:
                raise ValueError(
                    f"schema {self.name!r}: role {role.value!r} is carried by more than one field: {list(named)}"
                )

    def spec(self, name: str) -> FieldSpec | None:
        return self._by_name.get(name)

    def names(self, role: Role) -> tuple[str, ...]:
        """Fields carrying `role`, in declaration order."""
        return tuple(spec.name for spec in self.fields if role in spec.roles)

    def one(self, role: Role) -> FieldSpec | None:
        """The single field carrying a singular role, or None if the family has
        none — a legitimate answer, not an error: stock iTop's `FAQ` class has
        no modification date at all."""
        if role not in _SINGULAR_ROLES:
            raise ValueError(f"role {role.value!r} may be carried by several fields — use names()")
        found = self.names(role)
        return self._by_name[found[0]] if found else None

    def sources(self) -> dict[str, str | None]:
        """Default attribute code per field — what a deployment's mapping
        section starts from and overrides only where its datamodel differs.
        Kept here rather than copied into the config's defaults, so a saved
        mapping is what a deployment changed, not a copy of the code."""
        return {spec.name: spec.source for spec in self.fields}

    def multi_names(self) -> frozenset[str]:
        """Fields whose one mapping value yields several iTop values."""
        return frozenset(spec.name for spec in self.fields if spec.multi)

    def extended(self, specs: Iterable[FieldSpec]) -> "Schema":
        """This family plus the fields an administrator declared.

        A declared name that collides with one of the family's own is dropped
        with a warning rather than refused: saving such a name is answered
        with a 422 (`config.py::MappingsConfig`), and this is the last line —
        a section that reached here some other way must not take the whole
        family down with it ([[ADR-026-install-edits-cannot-brick-startup]]).
        """
        extra = []
        for spec in specs:
            if spec.name in self._by_name:
                logger.warning(f"schema {self.name!r}: {spec.name!r} is already a field of this family — ignoring")
                continue
            extra.append(spec)
        return self if not extra else Schema(name=self.name, fields=(*self.fields, *extra))

    def resolve(self, names: Iterable[str], *, by: str) -> tuple[str, ...]:
        """`names`, checked to be fields of this family.

        What a consumer keeping its own list of field names calls to make that
        list unable to drift away from the schema silently: the two are tied
        together at the point of use, and a name that no field answers to is
        named in the failure along with who asked for it.
        """
        wanted = tuple(names)
        unknown = [name for name in wanted if name not in self._by_name]
        if unknown:
            raise ValueError(
                f"{by}: {unknown} are not fields of the {self.name!r} schema — known: {sorted(self._by_name)}"
            )
        return wanted
