"""List-valued mapping: what iTop returns → what the index stores."""

import unittest

from pydantic import BaseModel

from itop_ai_assistant.repositories.valuemap import attribute, extract, list_fields, read_lists

_LINKSET = "customers_list:customer_id"


class TestListFields(unittest.TestCase):
    """A field is a list because the model says so — not because the config
    says so, which is what keeps the two from disagreeing."""

    def test_only_tuple_typed_fields_count(self):
        class Probe(BaseModel):
            org_id: str | None = None
            customer_org_ids: tuple[str, ...] = ()
            tags: list[str] = []

        self.assertEqual(frozenset({"customer_org_ids"}), list_fields(Probe))


class TestAttribute(unittest.TestCase):
    def test_a_linkset_is_asked_for_by_its_own_name(self):
        # iTop returns the links with their attributes, so the half after the
        # colon needs nothing added to output_fields.
        self.assertEqual("customers_list", attribute(_LINKSET))

    def test_a_plain_attribute_is_itself(self):
        self.assertEqual("org_id", attribute("org_id"))


class TestExtract(unittest.TestCase):
    def test_reads_ids_out_of_a_linkset(self):
        raw = {"customers_list": [{"customer_id": "7", "customer_name": "Acme"}, {"customer_id": "3"}]}

        self.assertEqual(("3", "7"), extract(raw, _LINKSET))

    def test_a_plain_attribute_yields_one_value(self):
        self.assertEqual(("3",), extract({"org_id": "3"}, "org_id"))

    def test_drops_unset_external_keys_and_blanks(self):
        raw = {"customers_list": [{"customer_id": ""}, {"customer_id": None}, {"customer_id": "5"}]}

        self.assertEqual(("5",), extract(raw, _LINKSET))
        self.assertEqual((), extract({"org_id": "0"}, "org_id"))

    def test_duplicates_collapse(self):
        raw = {"customers_list": [{"customer_id": "7"}, {"customer_id": "7"}]}

        self.assertEqual(("7",), extract(raw, _LINKSET))

    def test_an_absent_or_empty_value_yields_nothing(self):
        self.assertEqual((), extract({}, _LINKSET))
        self.assertEqual((), extract({"customers_list": []}, _LINKSET))
        self.assertEqual((), extract({}, "org_id"))

    def test_a_linkset_keyed_by_id_reads_the_same(self):
        raw = {"customers_list": {"11": {"customer_id": "7"}, "12": {"customer_id": "3"}}}

        self.assertEqual(("3", "7"), extract(raw, _LINKSET))

    def test_a_link_without_the_named_attribute_is_skipped(self):
        raw = {"customers_list": [{"org_id": "7"}, {"customer_id": "3"}]}

        self.assertEqual(("3",), extract(raw, _LINKSET))

    def test_a_linkset_mapped_without_its_id_attribute_warns_and_yields_nothing(self):
        # Stringifying the links would fill the index with garbage, and
        # nothing here can guess which attribute of a link was meant.
        raw = {"customers_list": [{"customer_id": "7"}]}

        with self.assertLogs("itop_ai_assistant.repositories.valuemap", level="WARNING"):
            self.assertEqual((), extract(raw, "customers_list"))

    def test_numbers_come_back_as_strings(self):
        self.assertEqual(("7",), extract({"org_id": 7}, "org_id"))


class TestReadLists(unittest.TestCase):
    def test_an_unmapped_field_is_absent_rather_than_empty(self):
        # Absent, so the model's own default answers for it.
        values = read_lists({"org_id": "3"}, {"customer_org_ids": None}, ["customer_org_ids"])

        self.assertEqual({}, values)

    def test_a_mapped_field_is_keyed_as_the_model_names_it(self):
        raw = {"customers_list": [{"customer_id": "3"}]}

        values = read_lists(raw, {"customer_org_ids": _LINKSET}, ["customer_org_ids"])

        self.assertEqual({"customer_org_ids": ("3",)}, values)
