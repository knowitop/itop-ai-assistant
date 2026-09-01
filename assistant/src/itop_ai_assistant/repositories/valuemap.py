"""List-valued semantic fields: one mapping value, several iTop values.

Most semantic fields name one attribute and hold one value. A few are lists by
nature — the organizations an FAQ article is published to arrive as an n-n link
set — and those are declared as such in the family schema (`FieldSpec.multi`,
`domain/schema.py`). The **declaration** is what says a field is a list; the
mapping only says where the values come from.

Two forms of mapping value, both plain strings in the same `fields` table as
every other attribute code:

- `org_id` — an ordinary attribute or external key. One value, wrapped into a
  one-element tuple (or an empty one when unset).
- `customers_list:customer_id` — an n-n link set and the attribute *of a link*
  that carries the id of interest. Every link contributes a value.

A path through a related object (`ticket_id->org_id`) is deliberately not
expressible: iTop's `output_fields` has no such syntax, so it would be a second
request per page rather than another form here.

Normalization lives here rather than in each caller: what iTop returns for an
unset external key ("0"), what an empty link set looks like, and the order of
links are the same question wherever such a field is read.
"""

import logging
from collections.abc import Collection, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

_LINKSET = ":"


def read_lists(
    raw: Mapping[str, Any], fields: Mapping[str, str | None], names: Collection[str]
) -> dict[str, tuple[str, ...]]:
    """Every mapped list-valued field of one raw row, keyed as the model names
    them — ready to splat into the domain object. An unmapped field is absent
    rather than empty, so the model's own default answers for it."""
    return {name: extract(raw, spec) for name in names if (spec := fields.get(name))}


def attribute(spec: str) -> str:
    """The attribute code to ask iTop for — a link set by its own name.

    iTop returns a link set with the links' attributes included, so the part
    after the colon needs nothing added to `output_fields`.
    """
    return spec.split(_LINKSET, 1)[0]


def extract(raw: Mapping[str, Any], spec: str) -> tuple[str, ...]:
    """The values this mapping spec resolves to on one raw iTop row, sorted
    and unique.

    Sorted because the caller compares it: iTop returns links in no particular
    order, and an order that moves between reads would make an unchanged object
    look changed (`vector/domain.py::ChunkMetadata.meta_hash`).
    """
    attr, _, id_field = spec.partition(_LINKSET)
    value = raw.get(attr)
    values = _linkset_values(value, id_field, attr) if id_field else _scalar_values(value, attr)
    return tuple(sorted(set(values)))


def _scalar_values(raw: Any, attr: str) -> list[str]:
    if isinstance(raw, (Mapping, list, tuple)):
        # A link set mapped without the `:<id attribute>` half. Nothing here
        # can guess which attribute of a link was meant, and stringifying the
        # links would fill the index with garbage.
        logger.warning(f"mapping: {attr!r} came back as a link set — write it as '{attr}:<id attribute>'")
        return []
    value = normalize_value(raw)
    return [] if value is None else [value]


def _linkset_values(raw_links: Any, id_field: str, attr: str) -> list[str]:
    """Ids out of one link set. iTop renders it as a list of links; a build
    that keys them by id instead is read the same way."""
    if isinstance(raw_links, Mapping):
        links: Any = raw_links.values()
    elif isinstance(raw_links, Sequence) and not isinstance(raw_links, str):
        links = raw_links
    elif raw_links is None:
        return []
    else:
        logger.warning(f"mapping: {attr!r} is mapped as a link set but iTop returned a plain value")
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
