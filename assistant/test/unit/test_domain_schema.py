"""The family schema: what it refuses to declare, and what its readers select."""

import unittest

from itop_ai_assistant.config import FaqFieldMap, TicketFieldMap
from itop_ai_assistant.domain.faq_schema import FAQ_SCHEMA
from itop_ai_assistant.domain.schema import FieldKind, FieldSpec, Role, Schema
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA


class TestFieldSpec(unittest.TestCase):
    """A role is a statement about the value, so a value read the wrong way
    cannot carry it — caught at import, not at the first object."""

    def test_a_role_requires_its_kind(self):
        with self.assertRaises(ValueError) as raised:
            FieldSpec("closed_at", FieldKind.DATETIME, "closed_at", roles=frozenset({Role.ORGANIZATION}))

        self.assertIn("requires kind 'id'", str(raised.exception))

    def test_a_timestamp_cannot_hold_several_values(self):
        with self.assertRaises(ValueError):
            FieldSpec("last_update", FieldKind.DATETIME, "last_update", multi=True)

    def test_a_case_log_is_appended_to_not_set(self):
        with self.assertRaises(ValueError):
            FieldSpec("public_log", FieldKind.LOG, "public_log", writable=True)


class TestSchema(unittest.TestCase):
    def test_a_field_cannot_be_declared_twice(self):
        with self.assertRaises(ValueError):
            Schema(
                name="probe",
                fields=(
                    FieldSpec("title", FieldKind.TEXT, "title"),
                    FieldSpec("title", FieldKind.TEXT, "name"),
                ),
            )

    def test_an_object_has_one_modification_time(self):
        with self.assertRaises(ValueError) as raised:
            Schema(
                name="probe",
                fields=(
                    FieldSpec("last_update", FieldKind.DATETIME, "last_update", roles=frozenset({Role.MODIFIED_AT})),
                    FieldSpec("touched_at", FieldKind.DATETIME, "touched_at", roles=frozenset({Role.MODIFIED_AT})),
                ),
            )

        self.assertIn("more than one field", str(raised.exception))

    def test_several_organizations_may_grant_access(self):
        # ADR-033 — unlike the singular roles, this one is a list by design.
        self.assertEqual(("org_id", "customer_org_ids"), FAQ_SCHEMA.names(Role.ORGANIZATION))

    def test_content_is_what_the_object_is_about_not_every_text_field(self):
        # `caller_name` is text and is deliberately not offered to the admin
        # as something to embed.
        self.assertEqual(FieldKind.TEXT, TICKET_SCHEMA.spec("caller_name").kind)
        self.assertNotIn("caller_name", TICKET_SCHEMA.names(Role.CONTENT))

    def test_multi_is_declared_not_inferred(self):
        self.assertEqual(frozenset({"customer_org_ids"}), FAQ_SCHEMA.multi_names())
        self.assertEqual(frozenset(), TICKET_SCHEMA.multi_names())

    def test_resolve_names_who_asked_for_an_unknown_field(self):
        with self.assertRaises(ValueError) as raised:
            TICKET_SCHEMA.resolve(("title", "no_such_field"), by="a caller")

        self.assertIn("a caller", str(raised.exception))
        self.assertIn("no_such_field", str(raised.exception))


class TestMappingSectionsCoverTheirSchema(unittest.TestCase):
    """The mapping section is what an administrator points at their own
    datamodel, so a field the schema declares and the section omits is a
    field nothing can ever map."""

    def test_every_declared_field_has_a_row(self):
        for schema, model in ((TICKET_SCHEMA, TicketFieldMap), (FAQ_SCHEMA, FaqFieldMap)):
            with self.subTest(family=schema.name):
                self.assertEqual({spec.name for spec in schema.fields}, set(model.model_fields))
