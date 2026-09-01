"""The FAQ family as the vector subsystem sees it — see
`content_sources/tickets.py` for the same declaration applied to tickets.

Simpler than tickets: one class, no conversation to index, nothing written
back. Stock iTop's `FAQ` carries neither a lifecycle status nor any date
attribute, so `vector.families.faq.classes.FAQ.index_values` is `[]` (every
article stays in the index) and every sweep pass reads every article. Neither
organization field is mapped either, so the R4 pre-filter (ADR-003) lets every
article through to `confirm_visible`; a build that publishes articles to a list
of customer organizations maps that link set onto `customer_org_ids` in
`faq_mapping` and names the field in `acl_org_fields`, with no code change.
"""

from itop_ai_assistant.content_sources.generic import Fragment, ObjectType
from itop_ai_assistant.domain.faq_schema import FAQ_SCHEMA

# The collection family this writes to.
FAMILY = FAQ_SCHEMA.name

OBJECT_TYPE = ObjectType(
    schema=FAQ_SCHEMA,
    fragments=(
        Fragment(kind="profile", visibility="public"),
        Fragment(kind="body", visibility="public"),
    ),
)
