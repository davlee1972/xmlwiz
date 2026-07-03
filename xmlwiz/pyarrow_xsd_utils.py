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
    xpathType,
    xpathValue,
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
            for child in self.children[name].children:
                self.children[name].remove(child)
        del self.children[name]


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
                if child_elem.type.name:
                    # add element type name to recursion counter in case child elements come up more than twice (by default).
                    recursion_check_list.append(child_elem.type.name)

                    if recursion_check_list.count(child_elem.type.name) > max_recursion:
                        continue
                child_counter += 1

                convert_xsd_elem(
                    child_elem,
                    parent_xpath_elem,
                    xpath,
                    max_recursion,
                    recursion_check_list,
                )

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


def convert_xpath_tree_to_pyarrow_schema(
    xpath_root, xpath_list=None, flat_attributes=False, flat_elements=False
):

    def reset_fields(xpath_elem: XmlNode):
        xpath_elem.field_name = xpath_elem.name
        xpath_elem.field_node_type = xpath_elem.node_type
        xpath_elem.field_pyarrow_type = xpath_elem.pyarrow_type
        xpath_elem.field_parent = xpath_elem.parent
        xpath_elem.field_children = xpath_elem.children
        for child_elem in xpath_elem.children.values():
            reset_fields(child_elem)

    reset_fields(xpath_root)

    def flatten_elements(xpath_elem: XmlNode):
        if (
            not xpath_elem.name.endswith("@attributes")
            and len(xpath_elem.field_children) == 1
        ):
            child, child_elem = next(iter(xpath_elem.field_children.items()))
            # move all child child items up a level
            xpath_elem.field_children = child_elem.field_children
            xpath_elem.field_node_type = child_elem.field_node_type
            # change parent to this xpath_elem
            for child_elem2 in xpath_elem.field_children.values():
                child_elem2.field_parent = xpath_elem
            # clear out child
            child_elem.field_name = None
            child_elem.field_node_type = None
            child_elem.field_pyarrow_type = None
            child_elem.field_parent = None
            child_elem.field_children = None

        for child_elem in xpath_elem.field_children.values():
            flatten_elements(child_elem)

    # flatten elements
    if flat_elements:
        flatten_elements(xpath_root)

    def flatten_attributes(xpath_elem: XmlNode):
        if xpath_elem.name.endswith("@attributes"):
            # add all xpath_elem children to xpath_elem parent
            # change parent for all xpath_elem childre to xpath_elem parent
            # remove xpath_elem as a child from xpath_elem parent
            # set xpath_elem name, parent and children to None for skipping
            old_children = xpath_elem.field_parent.field_children
            xpath_elem.field_parent.field_children = {}
            for child, child_elem in old_children.items():
                if child == xpath_elem.field_name:
                    for child2, child_elem2 in xpath_elem.field_children.items():
                        child_elem2.field_parent = xpath_elem.field_parent
                        child_elem2.field_name = (
                            xpath_elem.field_name.removesuffix("attributes")
                            + child_elem2.field_name
                        )
                        xpath_elem.field_parent.field_children[
                            child_elem2.field_name
                        ] = child_elem2
                else:
                    xpath_elem.field_parent.field_children[child] = child_elem
            xpath_elem.field_name = None
            xpath_elem.field_node_type = None
            xpath_elem.field_pyarrow_type = None
            xpath_elem.field_parent = None
            xpath_elem.field_children = None
        else:
            for child_elem in xpath_elem.field_children.values():
                flatten_attributes(child_elem)

    # flatten attributes
    if flat_attributes:
        flatten_attributes(xpath_root)

    # convert xpath element to pyarrow type
    def set_field_pyarrow_type(xpath_elem: XmlNode):

        if (
            isinstance(xpath_elem.field_node_type, tuple)
            and xpath_elem.field_node_type[0] == ElementType.LIST
        ):
            xpath_elem.field_pyarrow_type = pa.list_(xpath_elem.field_pyarrow_type)
            return (
                xpath_elem.field_name,
                xpath_elem.field_pyarrow_type,
                xpath_elem.nullable,
            )

        elif xpath_elem.field_node_type in [ElementType.DICT, ElementType.LIST_OF_DICT]:
            struct_fields = []
            for child_elem in xpath_elem.field_children.values():
                struct_fields.append(set_field_pyarrow_type(child_elem))

            struct_type = pa.struct(struct_fields)

            if xpath_elem.field_node_type == ElementType.DICT:
                xpath_elem.field_pyarrow_type = struct_type
                return (
                    xpath_elem.field_name,
                    xpath_elem.field_pyarrow_type,
                    xpath_elem.nullable,
                )
            elif xpath_elem.field_node_type == ElementType.LIST_OF_DICT:
                xpath_elem.field_pyarrow_type = pa.list_(struct_type)
                return (
                    xpath_elem.field_name,
                    xpath_elem.field_pyarrow_type,
                    xpath_elem.nullable,
                )
        else:
            return (
                xpath_elem.field_name,
                xpath_elem.field_pyarrow_type,
                xpath_elem.nullable,
            )

    set_field_pyarrow_type(xpath_root)

    if xpath_list:
        elem_found = find_field_elem(xpath_root, xpath_list)
        return elem_found.field_pyarrow_type
    else:
        return xpath_root.field_pyarrow_type
