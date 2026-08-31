"""Multi-valued mapping: what iTop returns → what the index stores."""

import unittest

from itop_ai_assistant.repositories.valuemap import AttrValue, LinksetValue, extract, projection


class TestProjection(unittest.TestCase):
    def test_asks_for_each_attribute_once(self):
        specs = [AttrValue(attr="org_id"), LinksetValue(attr="customers_list", id_field="customer_id")]

        self.assertEqual(["org_id", "customers_list"], projection(specs))

    def test_a_repeated_attribute_is_asked_for_once(self):
        specs = [AttrValue(attr="org_id"), AttrValue(attr="org_id")]

        self.assertEqual(["org_id"], projection(specs))

    def test_a_linkset_needs_nothing_beyond_its_own_name(self):
        # iTop returns the whole link with its attributes, so `id_field` is
        # read out of what came back rather than requested separately.
        self.assertEqual(["customers_list"], projection([LinksetValue(attr="customers_list", id_field="customer_id")]))


class TestExtract(unittest.TestCase):
    def test_reads_ids_out_of_a_linkset(self):
        raw = {"customers_list": [{"customer_id": "7", "customer_name": "Acme"}, {"customer_id": "3"}]}

        values = extract(raw, [LinksetValue(attr="customers_list", id_field="customer_id")])

        self.assertEqual(("3", "7"), values)

    def test_merges_every_spec_sorted_and_deduplicated(self):
        raw = {"org_id": "3", "customers_list": [{"customer_id": "7"}, {"customer_id": "3"}]}

        values = extract(raw, [AttrValue(attr="org_id"), LinksetValue(attr="customers_list", id_field="customer_id")])

        self.assertEqual(("3", "7"), values)

    def test_drops_unset_external_keys_and_blanks(self):
        raw = {"org_id": "0", "customers_list": [{"customer_id": ""}, {"customer_id": None}, {"customer_id": "5"}]}

        values = extract(raw, [AttrValue(attr="org_id"), LinksetValue(attr="customers_list", id_field="customer_id")])

        self.assertEqual(("5",), values)

    def test_an_absent_or_empty_attribute_yields_nothing(self):
        specs = [AttrValue(attr="org_id"), LinksetValue(attr="customers_list", id_field="customer_id")]

        self.assertEqual((), extract({}, specs))
        self.assertEqual((), extract({"customers_list": []}, specs))

    def test_a_linkset_keyed_by_id_reads_the_same(self):
        raw = {"customers_list": {"11": {"customer_id": "7"}, "12": {"customer_id": "3"}}}

        values = extract(raw, [LinksetValue(attr="customers_list", id_field="customer_id")])

        self.assertEqual(("3", "7"), values)

    def test_a_link_without_the_named_field_is_skipped(self):
        raw = {"customers_list": [{"org_id": "7"}, {"customer_id": "3"}]}

        values = extract(raw, [LinksetValue(attr="customers_list", id_field="customer_id")])

        self.assertEqual(("3",), values)

    def test_numbers_come_back_as_strings(self):
        self.assertEqual(("7",), extract({"org_id": 7}, [AttrValue(attr="org_id")]))
