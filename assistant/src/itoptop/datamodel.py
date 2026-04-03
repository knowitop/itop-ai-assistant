# mypy: ignore-errors
# lxml's xpath() returns XPathObject — a broad union type that mypy can't narrow
# without per-call casts. This file is XML-parsing infrastructure; errors are suppressed.
import copy
import re
from typing import Dict, List, Optional, Tuple

from lxml import etree


class DataModel:
    def __init__(self, filename: str):
        """
        Parse iTop datamodel XML file.

        :param filename: Path to the datamodel XML file exported from iTop.
        """
        xml = open(filename, encoding="utf-8", errors="replace").read()
        xml = re.sub('xmlns(:xsi|)="[^"]+"', "", xml, count=1)
        xml = re.sub("xsi:", "", xml, count=0)
        self.root = etree.XML(bytes(bytearray(xml, encoding="utf-8")))

        schemas = [node for node in self.root.xpath("//class") if "id" in node.attrib]
        self.schemas: List[str] = [schema.attrib["id"] for schema in schemas]

        self._cache_external_fields: Dict[str, Dict] = {}
        self._cache_linked_sets: Dict[str, Dict] = {}

    def lookupExternalField(self, schema: str) -> Dict[str, Tuple]:
        """
        Return a dict of external field names → (external_key, lookup_class, lookup_field)
        for a given schema, including inherited fields from parent classes.
        """
        if schema in self._cache_external_fields:
            return self._cache_external_fields[schema]

        root = self.root
        schema_lookups: Dict[str, Tuple] = {}

        fields = list(set(root.xpath("//class[@id='%s']//field[@type='AttributeExternalField']/@id" % schema)))
        for field in fields:
            key = root.xpath("//class[@id='%s']//field[@id='%s']/extkey_attcode/text()" % (schema, field))[0]
            lookup_field = root.xpath("//class[@id='%s']//field[@id='%s']/target_attcode/text()" % (schema, field))[0]

            # The key can be defined in a parent class — walk up the hierarchy
            current_schema: Optional[str] = schema
            while current_schema:
                field_types = root.xpath("//class[@id='%s']//field[@id='%s']/@type" % (current_schema, key))
                if not field_types:
                    parent = root.xpath("//class[@id='%s']/parent/text()" % current_schema)
                    current_schema = parent[0] if parent else None
                else:
                    if field_types[0] == "AttributeHierarchicalKey":
                        lookup_class = current_schema
                    else:
                        lookup_class = root.xpath(
                            "//class[@id='%s']//field[@id='%s']/target_class/text()" % (current_schema, key)
                        )[0]
                    schema_lookups[field] = (key, lookup_class, lookup_field)
                    break

        # Inherit from parent class
        parent = root.xpath("//class[@id='%s']/parent/text()" % schema)
        if parent and parent[0] != "cmdbAbstractObject":
            inherited = copy.deepcopy(self.lookupExternalField(parent[0]))
            inherited.update(schema_lookups)
            schema_lookups = inherited

        self._cache_external_fields[schema] = schema_lookups
        return schema_lookups

    def lookupLinkedSet(self, schema: str) -> Dict[str, Tuple]:
        """
        Return a dict of linked set field names → (linked_class, ext_key_to_me, ext_key_to_remote)
        for N-N relationships.
        """
        if schema in self._cache_linked_sets:
            return self._cache_linked_sets[schema]

        root = self.root
        schema_lookups: Dict[str, Tuple] = {}

        fields = list(set(root.xpath("//class[@id='%s']//field[@type='AttributeLinkedSetIndirect']/@id" % schema)))
        for field in fields:
            linked_class = root.xpath("//class[@id='%s']//field[@id='%s']/linked_class/text()" % (schema, field))[0]
            ext_key_to_me = root.xpath("//class[@id='%s']//field[@id='%s']/ext_key_to_me/text()" % (schema, field))[0]
            ext_key_to_remote = root.xpath(
                "//class[@id='%s']//field[@id='%s']/ext_key_to_remote/text()" % (schema, field)
            )[0]
            schema_lookups[field] = (linked_class, ext_key_to_me, ext_key_to_remote)

        self._cache_linked_sets[schema] = schema_lookups
        return schema_lookups
