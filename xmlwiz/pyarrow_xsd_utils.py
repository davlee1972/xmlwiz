#
# MIT License
#
# Copyright (c) 2026 David Lee
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to3 deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import isodate
from decimal import Decimal
import pyarrow as pa
import pyarrow.compute as pc

from xmlwiz.mappings import (
    ElementType,
    XSD_TO_PYARROW,
    XSD_TO_ELEMENT_DECODE,
)


@dataclass(slots=True)
class XmlNode:
    name: str
    level: int
    xpath: list
    node_type: int
    parent: XmlNode | None = field(repr=False)
    pyarrow_type: pa.DataType
    nullable: bool
    casting_exp: pc.Expression | None
    validation_exp: pc.Expression | None

    children: dict[str, XmlNode] = field(default_factory=dict, repr=False)

    field_name: str | None = None
    field_node_type: int | None = None
    field_pyarrow_type: pa.DataType | None = (field(default=None, repr=False),)
    field_parent: XmlNode | None = (field(default=None, repr=False),)
    field_children: dict[str, XmlNode] = field(default_factory=dict, repr=False)

    data_vector: list[str | None] | None = None
    data_offset: list[int | None] = None
    data_counter: int | None = None

    def add_child(
        self,
        name,
        node_type,
        pyarrow_type,
        nullable,
        casting_exp,
        validation_exp,
    ) -> XmlNode:
        new_child = XmlNode(
            name,
            self.level + 1,
            self.xpath + [name],
            node_type,
            self,
            pyarrow_type,
            nullable,
            casting_exp,
            validation_exp,
        )

        self.children[name] = new_child
        return new_child

    def remove_child(self, name):
        if self.children[name].children:
            for child_name in list(self.children[name].children):
                self.children[name].remove_child(child_name)
        del self.children[name]

    def find_elem(self, xpath_list):
        if self.xpath == xpath_list:
            return self
        elif self.children:
            for child_elem in self.children.values():
                elem_found = child_elem.find_elem(xpath_list)
                if elem_found:
                    return elem_found

    def find_field_elem(self, xpath_list):
        if self.xpath == xpath_list:
            return self
        elif self.field_children:
            for child_elem in self.field_children.values():
                elem_found = child_elem.find_field_elem(xpath_list)
                if elem_found:
                    return elem_found

    def get_data(self):
        data = {}
        data[tuple(self.xpath)] = (self.name, self.field_name, self.data_vector)
        if self.children:
            for child_elem in self.children.values():
                child_data = child_elem.get_data()
                data.update(child_data)
        return data

    def reset_and_trim_fields(self, xpath_list=None):
        self.field_name = self.name
        self.field_node_type = self.node_type
        self.field_pyarrow_type = self.pyarrow_type
        self.field_parent = self.parent
        self.field_children = self.children

        for child_elem in list(self.children.values()):
            child_elem.reset_and_trim_fields(xpath_list)

        if xpath_list:
            # attributes trimming is handled by parent
            try:
                if self.xpath[-1].endswith("@attributes"):
                    return
            except:
                pass

            try:
                if self.field_parent.xpath[-1].endswith("@attributes"):
                    return
            except:
                pass

            # we do not have a match between xpath_list and self.xpath
            if not all(a == b for a, b in zip(xpath_list, self.xpath)):
                self.parent.remove_child(self.name)

    def flatten_elements(self):
        if not self.name.endswith("@attributes") and len(self.field_children) == 1:
            child, child_elem = next(iter(self.field_children.items()))
            # move all child child items up a level
            self.field_children = child_elem.field_children
            self.field_node_type = child_elem.field_node_type
            # change parent to this self
            for child_elem2 in self.field_children.values():
                child_elem2.field_parent = self
            # clear out child
            child_elem.field_name = child_elem.field_node_type = (
                child_elem.field_pyarrow_type
            ) = child_elem.field_parent = child_elem.field_children = None

        for child_elem in self.field_children.values():
            child_elem.flatten_elements()

    def flatten_attributes(self):
        if self.name.endswith("@attributes"):
            # add all self children to self parent
            # change parent for all self childre to self parent
            # remove self as a child from self parent
            # set self name, parent and children to None for skipping
            old_children = self.field_parent.field_children
            self.field_parent.field_children = {}
            for child, child_elem in old_children.items():
                if child == self.field_name:
                    for child2, child_elem2 in self.field_children.items():
                        child_elem2.field_parent = self.field_parent
                        child_elem2.field_name = (
                            self.field_name.removesuffix("attributes")
                            + child_elem2.field_name
                        )
                        self.field_parent.field_children[child_elem2.field_name] = (
                            child_elem2
                        )
                else:
                    self.field_parent.field_children[child] = child_elem
            self.field_name = self.field_node_type = self.field_pyarrow_type = (
                self.field_parent
            ) = self.field_children = None
        else:
            for child_elem in self.field_children.values():
                child_elem.flatten_attributes()

    # convert xpath element to pyarrow type
    def set_pyarrow_type(self):

        if (
            isinstance(self.field_node_type, tuple)
            and self.field_node_type[0] == ElementType.LIST
        ):
            self.field_pyarrow_type = pa.list_(self.field_pyarrow_type)
            return (
                self.field_name,
                self.field_pyarrow_type,
                self.nullable,
            )

        elif self.field_node_type in [ElementType.DICT, ElementType.LIST_OF_DICT]:
            struct_fields = []
            for child_elem in self.field_children.values():
                struct_fields.append(child_elem.set_pyarrow_type())

            struct_type = pa.struct(struct_fields)

            if self.field_node_type == ElementType.DICT:
                self.field_pyarrow_type = struct_type
                return (
                    self.field_name,
                    self.field_pyarrow_type,
                    self.nullable,
                )
            elif self.field_node_type == ElementType.LIST_OF_DICT:
                self.field_pyarrow_type = pa.list_(struct_type)
                return (
                    self.field_name,
                    self.field_pyarrow_type,
                    self.nullable,
                )
        else:
            return (
                self.field_name,
                self.field_pyarrow_type,
                self.nullable,
            )


def xml_to_python(elem_text, element_type):
    # handles decoding element text to python data

    if isinstance(element_type, tuple) and element_type[0] == ElementType.LIST:
        elem_list = elem_text.split(" ")
        elem_list = [
            element_element_type(elem_item, element_type[1]) for elem_item in elem_list
        ]
        return elem_list
    elif element_type == ElementType.DECIMAL:
        return Decimal(elem_text)
    elif element_type == ElementType.DURATION:
        dur = isodate.parse_duration(elem_text)
        microseconds = int(dur.total_seconds() * 1_000_000)
        return microseconds
    elif element_type == ElementType.DATE:
        return datetime.fromisoformat(elem_text).date()
    elif element_type == ElementType.TIMESTAMP:
        return datetime.fromisoformat(elem_text)
    elif element_type == ElementType.TIME:
        return datetime.strptime(elem_text, "%H:%M:%S %z").time()
    elif element_type == ElementType.GEGORIAN:
        date_parts = elem_text.split("-")
        date_len = len(date_parts)
        """
            <gYearMonthType>2026-06</gYear MonthType> <gYearType>2026</gYearType>
            <gMonthDayType>--06-23</gMonthDayType>
            <gDayType>---23</gDayType>
            <gMonthType>--86</gMonthType>
        """
        if date_len == 1:
            return {"yyyy": int(date_parts[0])}
        elif date_len == 2:
            return {"yyyy": int(date_parts[0]), "mm": int(date_parts[1])}
        elif date_len == 3:
            return {"mm": int(date_parts[2])}
        elif date_len == 4:
            if date_parts[2]:
                return {"mm": int(date_parts[2]), "dd": int(date_parts[3])}
            else:
                return {"dd": int(date_parts[3])}
        return datetime.strptime(elem_text, "%H:%M:%S %z").time()
    else:
        return elem_text


def apply_facet(facet_name, vector, value):
    # Signed Integers
    if facet_name == "maxExclusive":
        return pc.less(vector, value)
    elif facet_name == "maxInclusive":
        return pc.less_equal, (vector, value)
    elif facet_name == "minExclusive":
        return pc.greater(vector, value)
    elif facet_name == "minInclusive":
        return pc.greater_equal, (vector, value)
    elif facet_name == "whitespace" and value == "collapse":
        return pc.replace_substring_regex(
            pc.utf8_trim_whitespace(vector), pattern=r"\s+", replacement=""
        )
    elif facet_name == "whitespace" and value == "replace":
        return pc.replace_substring_regex(vector, pattern=r"\s", replacement="")


def pyarrow_numeric(xsd_type):
    facets = {}
    if xsd_type.is_restriction():
        local_name = xsd_type.base_type.local_name
        for facet_name, facet_obj in xsd_type.facets.items():
            facets[facet_name] = facet_obj.value
    else:
        local_name = xsd_type.local_name
        base_type = xsd_type

    if local_name in [
        "decimal",
        "positiveInteger",
        "nonNegativeInteger",
        "negativeInteger",
        "nonPositiveInteger",
    ]:
        totalDigits = facets.get("totalDigits", None)
        fractionDigits = facets.get("totalDigits", totalDigits)
        max_value = facets.get("maxInclusive", facets.get("maxExclusive", None))
        min_value = facets.get("minInclusive", facets.get("minExclusive", None))

    if local_name == "decimal":
        if totalDigits:
            if totalDigits <= 9:
                return pa.decimal32(totalDigits, fractionDigits)
            elif totalDigits <= 18:
                return pa.decimal64(totalDigits, fractionDigits)
            elif totalDigits <= 38:
                return pa.decimal128(totalDigits, fractionDigits)
            elif totalDigits <= 76:
                return pa.decima1256(totalDigits, fractionDigits)
        else:
            return pa.decimal128(38, 10)
    elif local_name in ["positiveInteger", "nonNegativeInteger"]:
        max_value = facets.get("maxInclusive", facets.get("maxExclusive", None))
        if max_value:
            if max_value <= (1 << 8) - 1:
                return pa.uint8()
            elif max_value <= (1 << 16) - 1:
                return pa.uint16()
            elif max_value <= (1 << 32):
                return pa.uint32()
            elif max_value <= (1 << 64):
                return pa.uint64()
            elif max_value <= (1 << 127) - 1:
                return pa.decimal128(38, 0)
            elif max_value <= (1 << 255) - 1:
                return pa.decimal256(76, 0)
        elif totalDigits:
            if totalDigits < 3:
                return pa.uint8()
            elif totalDigits < 5:
                return pa.uint16()
            elif totalDigits < 10:
                return pa.uint32()
            elif totalDigits < 20:
                return pa.uint64()
            elif totalDigits <= 38:
                return pa.decimal128(totalDigits, 0)
            elif totalDigits <= 76:
                return pa.decimal256(totalDigits, 0)
        else:
            return pa.uint64()
    elif local_name in ["negativeInteger", "nonPositiveInteger"]:
        min_value = facets.get("minInclusive", facets.get("minExclusive", None))
        if min_value:
            if min_value >= -(1 << (8 - 1)):
                return pa.int8()
            elif min_value >= -(1 << (16 - 1)):
                return pa.int16()
            elif min_value >= -(1 << (32 - 1)):
                return pa.int32()
            elif min_value >= -(1 << (64 - 1)):
                return pa.int64()
        elif totalDigits:
            if totalDigits < 3:
                return pa.int8()
            elif totalDigits < 5:
                return pa.int16()
            elif totalDigits < 10:
                return pa.int32()
            elif totalDigits < 19:
                return pa.int64()
            elif totalDigits <= 38:
                return pa.decimal128(totalDigits, 0)
            elif totalDigits <= 76:
                return pa.decimal256(totalDigits, 0)
        else:
            return pa.uint64()
    else:
        return pa.int64()


def map_xsd_type_to_arrow(xsd_type):
    # returns
    # element_type - python Logic to transform element text to python type
    # pyarrow_type pyarrow type to transform from element text or python type
    # validation_rule - pyarrow compute expression to validate vectors
    if xsd_type.is_restriction():
        local_name = xsd_type.base_type.local_name
        base_type = xsd_type.base_type
    else:
        local_name = xsd_type.local_name
        base_type = xsd_type

    if base_type.is_list():
        local_name = base_type.item_type.local_name
        element_type = XSD_TO_ELEMENT_DECODE.get(local_name, ElementType.OTHER)
        pyarrow_type = XSD_TO_PYARROW.get(local_name, pa.string())
        if pyarrow_type == "numeric":
            pyarrow_type = pyarrow_numeric(xsd_type)
        pyarrow_validation = None
        return (
            (ElementType.LIST, element_type),
            pa.list_(pyarrow_type),
            pyarrow_validation,
        )
    else:
        element_type = XSD_TO_ELEMENT_DECODE.get(local_name, ElementType.OTHER)
        pyarrow_type = XSD_TO_PYARROW.get(local_name, pa.string())
        if pyarrow_type == "numeric":
            pyarrow_type = pyarrow_numeric(xsd_type)
        pyarrow_validation = None
        return element_type, pyarrow_type, pyarrow_validation


def convert_xsd_elem(elem, xpath_elem, xpath, max_recursion, recursion_check_list):

    xpath += [elem.local_name]
    level = len(xpath)
    nullable = elem.min_occurs == 0

    pyarrow_casting = None

    if elem.type.is_simple():
        element_type, pyarrow_type, pyarrow_validation = map_xsd_type_to_arrow(
            elem.type
        )
        xpath_elem.add_child(
            elem.local_name,
            element_type,
            pyarrow_type,
            nullable,
            pyarrow_casting,
            pyarrow_validation,
        )
    else:
        # 1. Process Attributes
        attr_fields = {}
        if hasattr(elem.type, "attributes"):
            attributes_nullable = True
            for attr in elem.type.attributes.values():
                attr_xpath = xpath + [elem.local_name + "@attributes", attr.name]
                element_type, pyarrow_type, pyarrow_validation = map_xsd_type_to_arrow(
                    attr.type
                )
                attr_nullable = attr.use == "optional"
                if not attr_nullable:
                    attributes_nullable = False

                attr_fields[attr.name] = (
                    element_type,
                    pyarrow_type,
                    attr_nullable,
                    pyarrow_casting,
                    pyarrow_validation,
                )

        # 2. Process Simple Content
        if elem.type.has_simple_content():
            element_type, pyarrow_type, pyarrow_validation = map_xsd_type_to_arrow(
                elem.type
            )
            if elem.max_occurs is None or elem.max_occurs > 1:
                parent_xpath_elem = xpath_elem.add_child(
                    elem.local_name,
                    (ElementType.LIST, element_type),
                    pyarrow_type,
                    nullable,
                    pyarrow_casting,
                    pyarrow_validation,
                )
            else:
                parent_xpath_elem = xpath_elem.add_child(
                    elem.local_name,
                    element_type,
                    pyarrow_type,
                    nullable,
                    pyarrow_casting,
                    pyarrow_validation,
                )

        # 3. Process Mixed and Complex Content
        elif elem.type.has_complex_content() or elem.type.has_mixed_content():
            if elem.max_occurs is None or elem.max_occurs > 1:
                parent_xpath_elem = xpath_elem.add_child(
                    elem.local_name,
                    ElementType.LIST_OF_DICT,
                    None,
                    nullable,
                    None,
                    None,
                )
            else:
                parent_xpath_elem = xpath_elem.add_child(
                    elem.local_name,
                    ElementType.DICT,
                    None,
                    nullable,
                    None,
                    None,
                )

            if elem.type.has_mixed_content():
                element_type, pyarrow_type, pyarrow_validation = map_xsd_type_to_arrow(
                    elem.type
                )
                parent_xpath_elem.add_child(
                    elem.local_name,
                    element_type,
                    pyarrow_type,
                    nullable,
                    pyarrow_casting,
                    pyarrow_validation,
                )

        if attr_fields:
            attr_group_name = elem.local_name + "@attributes"
            attr_group = parent_xpath_elem.add_child(
                attr_group_name,
                ElementType.DICT,
                None,
                attributes_nullable,
                None,
                None,
            )
            for attr_name, attr_values in attr_fields.items():
                attr_group.add_child(
                    attr_name,
                    *attr_values,
                )

        if elem.type.has_complex_content() or elem.type.has_mixed_content():
            child_counter = 0
            for child_elem in elem.type.content.iter_elements():
                old_recursion_check_list = recursion_check_list
                if child_elem.type.is_complex() and child_elem.type.name:
                    # add element type name to recursion counter in case child elements come up more than twice (by default).
                    recursion_check_list = recursion_check_list + [child_elem.type.name]
                    if recursion_check_list.count(child_elem.type.name) > max_recursion:
                        recursion_check_list = old_recursion_check_list
                        continue
                child_counter += 1

                convert_xsd_elem(
                    child_elem,
                    parent_xpath_elem,
                    xpath,
                    max_recursion,
                    recursion_check_list,
                )

                recursion_check_list = old_recursion_check_list

            if elem.type.has_complex_content() and child_counter == 0:
                xpath_elem.remove_child(elem.local_name)


def convert_xsd_to_xpath_tree(xsd_schema, max_recursion=2):

    xpath_root = XmlNode("root", 0, [], ElementType.DICT, None, None, True, None, None)

    for elem in xsd_schema.elements.values():
        if not elem.is_global():
            continue
        convert_xsd_elem(
            elem,
            xpath_root,
            xpath=[],
            max_recursion=max_recursion,
            recursion_check_list=[],
        )

    # Each xml file should only have one root element
    # However, we may have to merge different root elements across files.
    # Make all root elements nullable to enable root schema merging.
    if len(xpath_root.children) > 1:
        for child_elem in xpath_root.children.values():
            child_elem.nullable = True

    return xpath_root


def find_elem(xpath_elem: XmlNode, xpath_list):
    if xpath_elem.xpath == xpath_list:
        return xpath_elem
    elif xpath_elem.children:
        for child_elem in xpath_elem.children.values():
            elem_found = find_elem(child_elem, xpath_list)
            if elem_found:
                return elem_found


def find_field_elem(xpath_elem: XmlNode, xpath_list):
    if xpath_elem.xpath == xpath_list:
        return xpath_elem
    elif xpath_elem.field_children:
        for child_elem in xpath_elem.field_children.values():
            elem_found = find_field_elem(child_elem, xpath_list)
            if elem_found:
                return elem_found


def get_data_elem(xpath_elem: XmlNode):
    data = {}
    data[tuple(xpath_elem.xpath)] = xpath_elem.data_vector
    if xpath_elem.field_children:
        for child_elem in xpath_elem.field_children.values():
            child_data = get_data_elem(child_elem)
            data.update(child_data)
    return data


def convert_xpath_tree_to_pyarrow_schema(
    xpath_root, xpath_list=None, flat_attributes=False, flat_elements=False
):

    xpath_root.reset_and_trim_fields(xpath_list)

    # flatten elements
    if flat_elements:
        xpath_root.flatten_elements()

    # flatten attributes
    if flat_attributes:
        xpath_root.flatten_attributes()

    xpath_root.set_pyarrow_type()

    return xpath_root.field_pyarrow_type
