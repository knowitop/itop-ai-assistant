"""Every object family the deployment knows, by name.

One list, and the only one: the mapping section is built from it, the
repositories are built from it, and a family absent from here has no way to be
configured or read. What a family declares beyond its fields — which fragments
the vector index can build out of it — belongs to the subsystem that consumes
it (`content_sources/`), not here.
"""

from collections.abc import Mapping

from itop_ai_assistant.domain.faq_schema import FAQ_SCHEMA
from itop_ai_assistant.domain.schema import Schema
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA

SCHEMAS: Mapping[str, Schema] = {schema.name: schema for schema in (TICKET_SCHEMA, FAQ_SCHEMA)}
