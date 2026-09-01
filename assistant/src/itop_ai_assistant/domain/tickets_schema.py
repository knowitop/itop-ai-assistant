"""The ticket family declared as data — what a ticket's semantic fields are
and which stock iTop attribute each reads by default.

The mapping section `ticket_mapping` overrides the attribute codes for a
customized datamodel; the field list, the kinds and the roles are code,
because each of them has a reader that has to understand it.
"""

from itop_ai_assistant.domain.schema import FieldKind, FieldSpec, Role, Schema

TICKET_SCHEMA = Schema(
    name="tickets",
    fields=(
        FieldSpec("ref", FieldKind.ID, "ref"),
        FieldSpec("title", FieldKind.TEXT, "title", writable=True, roles=frozenset({Role.CONTENT})),
        FieldSpec("description", FieldKind.TEXT, "description", writable=True, roles=frozenset({Role.CONTENT})),
        FieldSpec("status", FieldKind.ENUM, "status", writable=True, roles=frozenset({Role.LIFECYCLE_STATE})),
        FieldSpec("service_id", FieldKind.ID, "service_id", writable=True),
        FieldSpec("subcategory_id", FieldKind.ID, "servicesubcategory_id", writable=True),
        FieldSpec(
            "service_name",
            FieldKind.TEXT,
            "service_id_friendlyname",
            roles=frozenset({Role.CONTENT}),
            description="Display name of the service, not its id",
        ),
        FieldSpec(
            "subcategory_name",
            FieldKind.TEXT,
            "servicesubcategory_id_friendlyname",
            roles=frozenset({Role.CONTENT}),
            description="Display name of the subcategory, not its id",
        ),
        # Text, but not what the ticket is about: it names a person, and the
        # log labelling compares it with an entry's author. Hence no
        # `CONTENT` — an administrator cannot compose the caller's name into
        # an embedded fragment, which is a privacy decision, not an oversight.
        FieldSpec("caller_name", FieldKind.TEXT, "caller_id_friendlyname", description="Display name of the caller"),
        FieldSpec("org_id", FieldKind.ID, "org_id", writable=True, roles=frozenset({Role.ORGANIZATION})),
        FieldSpec(
            "request_type",
            FieldKind.ENUM,
            "request_type",
            writable=True,
            description="Service request or incident; absent on some classes",
        ),
        FieldSpec("public_log", FieldKind.LOG, "public_log", description="Conversation with the caller"),
        FieldSpec("private_log", FieldKind.LOG, "private_log", description="Notes between engineers"),
        FieldSpec("solution", FieldKind.TEXT, "solution", writable=True, roles=frozenset({Role.CONTENT})),
        FieldSpec("last_update", FieldKind.DATETIME, "last_update", roles=frozenset({Role.MODIFIED_AT})),
        FieldSpec("start_date", FieldKind.DATETIME, "start_date", roles=frozenset({Role.CREATED_AT})),
    ),
)
