import unittest

from itop_ai_assistant.domain.identity import ObjectIdentity


class TestObjectIdentity(unittest.TestCase):
    def test_str_composes_class_and_id(self):
        self.assertEqual(str(ObjectIdentity(obj_class="UserRequest", obj_id="42")), "UserRequest::42")

    def test_parses_the_class_alias(self):
        ref = ObjectIdentity.model_validate({"class": "Incident", "id": "7"})
        self.assertEqual(ref.obj_class, "Incident")

    def test_populate_by_name_still_accepts_the_field_name(self):
        ref = ObjectIdentity(obj_class="Incident", obj_id="7")
        self.assertEqual(ref.obj_class, "Incident")


if __name__ == "__main__":
    unittest.main()
