"""The object view: what generic code may ask an object, and what it may not."""

import unittest
from datetime import UTC, datetime

from itop_ai_assistant.domain.faq_schema import FAQ_SCHEMA
from itop_ai_assistant.domain.object_view import LogEntry, ObjectView
from itop_ai_assistant.domain.schema import Role
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA


def _ticket(**values) -> ObjectView:
    return ObjectView(schema=TICKET_SCHEMA, obj_class="UserRequest", id="42", values=values)


class TestReadingAField(unittest.TestCase):
    def test_an_absent_field_reads_as_its_kinds_empty_value(self):
        view = _ticket()

        self.assertEqual("", view.text("title"))
        self.assertIsNone(view.identifier("service_id"))
        self.assertEqual((), view.identifiers("org_id"))
        self.assertIsNone(view.moment("last_update"))
        self.assertEqual([], view.log("public_log"))

    def test_a_name_the_family_never_declared_is_a_mistake_not_an_empty_value(self):
        # The failure this replaces: a renamed field quietly reading as "".
        with self.assertRaises(KeyError):
            _ticket().text("no_such_field")

    def test_reading_a_field_as_the_wrong_kind_is_refused(self):
        with self.assertRaises(TypeError):
            _ticket(last_update=datetime.now(UTC)).text("last_update")

    def test_one_organization_and_a_list_of_them_read_the_same_way(self):
        one = _ticket(org_id="7")
        many = ObjectView(schema=FAQ_SCHEMA, obj_class="FAQ", id="1", values={"customer_org_ids": ("3", "7")})

        self.assertEqual(("7",), one.identifiers("org_id"))
        self.assertEqual(("3", "7"), many.identifiers("customer_org_ids"))


class TestReadingByRole(unittest.TestCase):
    """How generic code asks for a meaning instead of a name."""

    def test_a_role_resolves_to_the_field_carrying_it(self):
        view = _ticket(status="resolved", last_update=datetime(2026, 7, 10, tzinfo=UTC))

        self.assertEqual("resolved", view.state_of(Role.LIFECYCLE_STATE))
        self.assertEqual(datetime(2026, 7, 10, tzinfo=UTC), view.moment_of(Role.MODIFIED_AT))

    def test_a_log_keeps_its_entries(self):
        view = _ticket(public_log=[LogEntry(user_login="John Doe", message="Help!")])

        self.assertEqual(["John Doe"], [entry.user_login for entry in view.log("public_log")])


if __name__ == "__main__":
    unittest.main()
