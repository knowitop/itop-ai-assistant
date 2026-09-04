"""The ticket family as the vector subsystem sees it.

A declaration, not an implementation: what a source does with it is
`content_sources/generic.py`. The chunking vocabulary is derived — a field is
offered because it carries what the ticket is about (`Role.CONTENT`), and the
hand-written list is what once let this vocabulary call a field `service`
while the model called it `service_name`.
"""

from itop_ai_assistant.content_sources.generic import Fragment, ObjectType
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA

# The collection family this writes to.
FAMILY = TICKET_SCHEMA.name

OBJECT_TYPE = ObjectType(
    schema=TICKET_SCHEMA,
    fragments=(
        Fragment(kind="profile", visibility="public"),
        Fragment(kind="body", visibility="public"),
        Fragment(kind="solution", visibility="public"),
        # The two log fragments are opt-in: whether internal notes get
        # embedded at all is the administrator's call, and `log:private` is
        # the only fragment here that is not caller-facing. Which log feeds
        # which is fixed here rather than configurable — letting the private
        # log feed a public fragment would make the visibility above a lie.
        Fragment(kind="log:public", visibility="public", log_field="public_log", optional=True),
        Fragment(kind="log:private", visibility="internal", log_field="private_log", optional=True),
    ),
    filters=("service_id",),
    indexed_filter_keys=("status",),
)
