"""Which organizations give access to one object — the union a source hands
to the indexer as `VectorRecord.acl_org_ids`.

Shared by every content source because the question has one shape: the class
config names semantic fields (`VectorClassConfig.acl_org_fields`), the object
carries them, and the values are read off it and merged. Which fields those
may be is the family's own declaration (`Role.ORGANIZATION`), and a name
outside it is refused when the config is saved — this is the second line: a
config written before a field was renamed warns and yields nothing rather than
failing the pass, exactly as a stale `ChunkPlan` field name does.

The union is deliberately flat. What is lost is which field granted the
access — see `dev-docs/tasks/TASK-076-multi-value-acl-prefilter/`.
"""

import logging
from collections.abc import Sequence

from itop_ai_assistant.domain.object_view import ObjectView
from itop_ai_assistant.domain.schema import Role

logger = logging.getLogger(__name__)


def org_ids(obj: ObjectView, fields: Sequence[str], *, source: str) -> tuple[str, ...]:
    """The values of `fields` on `obj`, as one tuple.

    A field may hold one value or several: a ticket's `org_id` is a scalar,
    an article's `customer_org_ids` a link set, and both mean the same thing
    here. A field this deployment does not map contributes nothing, which is
    not an error — stock iTop scopes no FAQ article by organization.
    """
    values: list[str] = []
    for name in fields:
        spec = obj.schema.spec(name)
        if spec is None or not spec.has(Role.ORGANIZATION):
            logger.warning(f"{source} source: acl_org_fields names {name!r}, which grants no access here — ignored")
            continue
        values.extend(obj.identifiers(name))
    return tuple(values)
