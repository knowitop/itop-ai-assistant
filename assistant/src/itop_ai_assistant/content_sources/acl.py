"""Which organizations give access to one object — the union a source hands
to the indexer as `VectorRecord.acl_org_ids`.

Shared by every content source because the question has one shape: the class
config names semantic fields (`VectorClassConfig.acl_org_fields`), the domain
object carries them, and the values are read off it and merged. Which fields
those may be is the source's own declaration (`VectorSource.org_fields`), and
a name outside it is refused when the config is saved — this is the second
line: a config written before a field was renamed warns and yields nothing
rather than failing the pass, exactly as a stale `ChunkPlan` field name does.

The union is deliberately flat. What is lost is which field granted the
access — see `dev-docs/tasks/TASK-076-multi-value-acl-prefilter/`.
"""

import logging
from collections.abc import Sequence

from itop_ai_assistant.repositories.valuemap import normalize_value

logger = logging.getLogger(__name__)


def org_ids(obj: object, fields: Sequence[str], *, source: str) -> tuple[str, ...]:
    """The values of `fields` on `obj`, as one tuple.

    A field may hold one value or several: `Ticket.org_id` is a scalar,
    `FaqArticle.customer_org_ids` a tuple, and both mean the same thing here.
    """
    values: list[str] = []
    for name in fields:
        if not hasattr(obj, name):
            logger.warning(f"{source} source: acl_org_fields names unknown field {name!r} — ignored")
            continue
        raw = getattr(obj, name)
        for item in raw if isinstance(raw, (list, tuple, set, frozenset)) else [raw]:
            value = normalize_value(item)
            if value is not None:
                values.append(value)
    return tuple(values)
