"""Multi-valued semantic field → iTop, as a value the config can hold.

A semantic field mapped through `TicketFieldMap`/`FaqFieldMap` names exactly
one attribute and yields one value. Some facts an installation needs are not
shaped like that: the organizations an FAQ article is published to arrive as
an n-n link set, and a class may carry more than one attribute that means the
same thing. `fields_multi` in the mapping sections is that case — a semantic
name bound to a *list* of `ValueSpec`, whose values are read together and
merged into one tuple.

Two forms, and deliberately no third: `attr` for a plain attribute or an
external key, `linkset` for an n-n link set whose links carry the id under
`id_field`. A path through a related object (`ticket_id->org_id`) is not
expressible — iTop's `output_fields` has no such syntax, so it would be a
second request per page, not another spec.

Normalization lives here rather than in each caller: what iTop returns for an
unset external key ("0"), what an empty link set looks like, and the order of
links are all the same question wherever a multi-valued field is read.
"""

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class AttrValue(BaseModel):
    """One iTop attribute read as-is — a scalar or an external key."""

    kind: Literal["attr"] = "attr"
    attr: str


class LinksetValue(BaseModel):
    """An n-n link set: `attr` is the link set on the class, `id_field` the
    attribute of a *link* that carries the id of interest (for
    `FAQ.customers_list` in a build that has it: `customer_id`)."""

    kind: Literal["linkset"] = "linkset"
    attr: str
    id_field: str


type ValueSpec = Annotated[AttrValue | LinksetValue, Field(discriminator="kind")]


def projection(specs: Sequence[ValueSpec]) -> list[str]:
    """Attributes these specs need in `output_fields`, deduplicated.

    A link set is requested by its own name — iTop returns the whole link with
    its attributes, so `id_field` needs nothing added here.
    """
    attrs: list[str] = []
    for spec in specs:
        if spec.attr not in attrs:
            attrs.append(spec.attr)
    return attrs


def extract(raw: Mapping[str, Any], specs: Sequence[ValueSpec]) -> tuple[str, ...]:
    """The union of every spec's values on one raw iTop row, sorted and unique.

    Sorted because the caller compares it: iTop returns links in no
    particular order, and an order that moves between reads would make an
    unchanged object look changed (`ChunkMetadata.meta_hash`).
    """
    values: set[str] = set()
    for spec in specs:
        if isinstance(spec, LinksetValue):
            values.update(_linkset_values(raw.get(spec.attr), spec.id_field))
        else:
            value = normalize_value(raw.get(spec.attr))
            if value is not None:
                values.add(value)
    return tuple(sorted(values))


def _linkset_values(raw_links: Any, id_field: str) -> list[str]:
    """Ids out of one link set. iTop renders it as a list of links; a build
    that keys them by id instead is read the same way."""
    if isinstance(raw_links, Mapping):
        links: Any = raw_links.values()
    elif isinstance(raw_links, Sequence) and not isinstance(raw_links, str):
        links = raw_links
    else:
        return []
    found = []
    for link in links:
        if not isinstance(link, Mapping):
            continue
        value = normalize_value(link.get(id_field))
        if value is not None:
            found.append(value)
    return found


def normalize_value(raw: Any) -> str | None:
    """One value as the index stores it, or None when there is none.

    Public because the same reading applies wherever a value is bound for
    filtering, not only when it is read off a raw iTop row
    (`content_sources/acl.py`).

    "0" is iTop's unset external key (same reading as
    `repositories/ticket.py::_external_key`), and an empty string is an unset
    anything.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    return None if text in ("", "0") else text
