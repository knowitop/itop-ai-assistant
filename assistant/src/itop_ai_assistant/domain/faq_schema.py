"""The FAQ family declared as data — see `domain/tickets_schema.py` for the
same declaration applied to tickets.

Read-only: no field is `writable`, which is the whole of "the sweep only ever
reads FAQ content". Several fields have no default attribute at all, because
stock iTop's `FAQ` class has neither a lifecycle status, nor an org-scoped
ACL, nor any date attribute — a deployment whose FAQ does carry one maps it.
"""

from itop_ai_assistant.domain.schema import FieldKind, FieldSpec, Role, Schema

FAQ_SCHEMA = Schema(
    name="faq",
    fields=(
        FieldSpec("title", FieldKind.TEXT, "title", roles=frozenset({Role.CONTENT})),
        FieldSpec(
            "summary",
            FieldKind.TEXT,
            "summary",
            roles=frozenset({Role.CONTENT}),
            description="Short abstract of the article, if the class has one",
        ),
        FieldSpec(
            "category_name",
            FieldKind.TEXT,
            "category_name",
            roles=frozenset({Role.CONTENT}),
            description="Display name of the FAQ category",
        ),
        FieldSpec(
            "error_code",
            FieldKind.TEXT,
            "error_code",
            roles=frozenset({Role.CONTENT}),
            description="Error code the article is about",
        ),
        FieldSpec(
            "key_words",
            FieldKind.TEXT,
            "key_words",
            roles=frozenset({Role.CONTENT}),
            description="Search keywords of the article",
        ),
        FieldSpec("description", FieldKind.TEXT, "description", roles=frozenset({Role.CONTENT})),
        # Unmapped by default: FAQ has no lifecycle status in stock iTop, so
        # `vector.families.faq.classes.FAQ.index_values` is [] — every article
        # stays in the index — rather than a list of statuses to filter by.
        FieldSpec("status", FieldKind.ENUM, None, roles=frozenset({Role.LIFECYCLE_STATE})),
        # Unmapped in stock iTop as well, so the R4 pre-filter lets every
        # article through to `confirm_visible` (ADR-033). A build that scopes
        # articles by organization maps it and names it in `acl_org_fields`;
        # one that publishes an article to a *list* of customer organizations
        # declares a list-valued field of its own for that link set (ADR-034).
        FieldSpec("org_id", FieldKind.ID, None, roles=frozenset({Role.ORGANIZATION})),
        # Stock iTop's FAQ class carries no date attribute at all. With
        # `last_update` unmapped every sweep pass re-reads every article
        # (cheap thanks to the hash-guard, `vector/use_cases/indexer.py`).
        FieldSpec("last_update", FieldKind.DATETIME, None, roles=frozenset({Role.MODIFIED_AT})),
        FieldSpec("start_date", FieldKind.DATETIME, None, roles=frozenset({Role.CREATED_AT})),
    ),
)
