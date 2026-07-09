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
from decimal import Decimal
import pyarrow as pa
import pyarrow.compute as pc

from xmlwiz.mappings import (
    ComputeType,
    XSD_TO_PYARROW,
    XSD_TO_COMPUTE_DECODE,
)


@dataclass(slots=True)
class XmlElement:
    name: str
    xpaths: list
    is_simple: bool
    is_list: bool
    is_dict: bool
    pyarrow_type: pa.DataType | None
    nullable: bool | None
    casting_exp: list[pc.Expression] | None
    validation_exp: list[pc.Expression] | None

    parent: XmlElement | None = field(repr=False)
    children: dict[str, XmlElement] = field(default_factory=dict, repr=False)

    field_name: str | None = None
    field_pyarrow_type: pa.DataType | None = field(default=None, repr=False)
    field_skip: bool = False

    data_vector: list[str] = field(default_factory=list)
    data_offsets: list[int | None] = field(default_factory=lambda: [0])
    data_counter: int = 0
    data_pyarrow: pa.Array = None

    def add_child(
        self,
        name,
        is_simple,
        is_list,
        is_dict,
        pyarrow_type,
        nullable,
        casting_exp,
        validation_exp,
    ) -> XmlElement:

        new_child = XmlElement(
            name,
            self.xpaths + [name],
            is_simple,
            is_list,
            is_dict,
            pyarrow_type,
            nullable,
            casting_exp,
            validation_exp,
            self,
        )

        self.children[name] = new_child
        return new_child

    def remove_child(self, name):
        if self.children[name].children:
            for child_name in list(self.children[name].children):
                self.children[name].remove_child(child_name)
        del self.children[name]

    def iter_elem(self):
        yield self
        for child_elem in self.children.values():
            yield from child_elem.iter_elem()

    def find_elem(self, xml_path):

        if isinstance(xml_path, str):
            xpaths = xml_path.split("/")
            xpaths = xpaths[1:]
        else:
            xpaths = xml_path

        for xpath_elem in self.iter_elem():
            if xpath_elem.xpaths == xpaths:
                return xpath_elem

    def get_data_vector(self):
        data = {}
        for xpath_elem in self.iter_elem():
            data[tuple(xpath_elem.xpaths)] = xpath_elem.data_vector
        return data


    def clear_data(self):
        for elem in self.iter_elem():
            elem.data_vector = []
            elem.data_offsets = [0]
            elem.data_counter = 0
            elem.data_pyarrow = None

    def reset_fields(self):
        for elem in self.iter_elem():
            elem.field_name = elem.name
            elem.field_pyarrow_type = elem.pyarrow_type
            elem.field_skip = False        

    def trim_elements(self, xpaths):
        # attributes and tail trimming is handled by removing parent element
        try:
            if self.xpaths[-1].endswith("@attributes") or self.xpaths[-1].endswith(
                "@tail"
            ):
                return
        except:
            pass

        try:
            if self.parent.xpaths[-1].endswith("@attributes") or self.parent.xpaths[
                -1
            ].endswith("@tail"):
                return
        except:
            pass

        # we do not have a match between xpaths and self.xpaths
        if not all(a == b for a, b in zip(xpaths, self.xpaths)):
            self.parent.remove_child(self.name)

    def flatten_elements(self):
        for xpath_elem in self.iter_elem():
            if not xpath_elem.name.endswith("@attributes") and len(xpath_elem.children) == 1:
                child, child_elem = next(iter(xpath_elem.children.items()))
                if child.endswith("@attributes"):
                    return
                xpath_elem.field_skip = True
                if not xpath_elem.nullable and child_elem.nullable:
                    xpath_elem.nullable = True

    def flatten_attributes(self):
        for xpath_elem in self.iter_elem():
            if xpath_elem.name.endswith("@attributes"):
                xpath_elem.field_skip = True
                # change name of all children
                for child_elem in xpath_elem.children.values():
                    child_elem.field_name = xpath_elem.name.removesuffix("attributes") + child_elem.name



    # convert xpath element to pyarrow type
    def set_field_pyarrow_type(self):
        # process data types in reverse. parent data types may be structs of child data types which in turn may also be structs.
        for xpath_elem in reversed(list(self.iter_elem())):
            if xpath_elem.field_skip:
                if not xpath_elem.name.endswith("@attributes"):
                    child_elem = next(iter(xpath_elem.children.values()))
                    xpath_elem.field_pyarrow_type = child_elem.field_pyarrow_type
                continue

            if xpath_elem.is_dict:
                struct_fields = [
                    pa.field(v.field_name, v.field_pyarrow_type, nullable=v.nullable)
                    for v in xpath_elem.children.values()
                    if v.field_pyarrow_type
                ]

                # add in flattened attributes
                attributes = xpath_elem.name + "@attributes"
                if attributes in xpath_elem.children and xpath_elem.children[attributes].field_skip:
                    attributes_elem = xpath_elem.children[attributes]
                    attr_struct_fields = [
                        pa.field(v.field_name, v.field_pyarrow_type, nullable=v.nullable)
                        for v in attributes_elem.children.values()
                        if v.field_pyarrow_type
                    ]
                    if attr_struct_fields:
                        attr_struct_fields.extend(struct_fields)
                        struct_fields = attr_struct_fields

                xpath_elem.field_pyarrow_type = pa.struct(struct_fields)

            if xpath_elem.is_list:
                xpath_elem.field_pyarrow_type = pa.list_(xpath_elem.field_pyarrow_type)


def pyarrow_numeric(xsd_type):
    facets = {}
    if xsd_type.is_restriction():
        local_name = xsd_type.base_type.local_name
        for facet_name, facet_obj in xsd_type.facets.items():
            facets[facet_name] = facet_obj.value
    else:
        local_name = xsd_type.local_name
        base_type = xsd_type

    if local_name in (
        "decimal",
        "positiveInteger",
        "nonNegativeInteger",
        "negativeInteger",
        "nonPositiveInteger",
    ):
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
    elif local_name in ("positiveInteger", "nonNegativeInteger"):
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
    elif local_name in ("negativeInteger", "nonPositiveInteger"):
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
            return pa.int64()
    else:
        return pa.int64()


def map_xsd_simple_type_to_arrow(xsd_type):
    # returns
    # pyarrow_type pyarrow type to transform from element text or python type
    # casting exp - pyarrow compute expressions to cast string to pyarrow types
    # validation exp - pyarrow compute expressions to validate vectors

    casting_exp = []
    validation_exp = []

    if xsd_type.is_restriction():
        local_name = xsd_type.base_type.local_name
        base_type = xsd_type.base_type
    else:
        local_name = xsd_type.local_name
        base_type = xsd_type

    if base_type.is_list():
        local_name = base_type.item_type.local_name
        casting_exp.append(ComputeType.LIST)

    compute_type = XSD_TO_COMPUTE_DECODE.get(local_name, None)
    if compute_type:
        casting_exp.append(compute_type)

    pyarrow_type = XSD_TO_PYARROW.get(local_name, pa.string())
    if pyarrow_type == "numeric":
        pyarrow_type = pyarrow_numeric(xsd_type)

    return (
        pyarrow_type,
        casting_exp,
        validation_exp,
    )


def convert_xsd_elem(elem, xpath_elem, max_recursion, recursion_check_list):

    nullable = elem.min_occurs == 0

    if elem.max_occurs is None or elem.max_occurs > 1:
        is_list = True
    else:
        is_list = False

    if elem.type.is_simple():
        pyarrow_type, casting_exp, validation_exp = map_xsd_simple_type_to_arrow(
            elem.type
        )

        xpath_elem.add_child(
            elem.local_name,
            True,
            is_list,
            False,
            pyarrow_type,
            nullable,
            casting_exp,
            validation_exp,
        )

    else:
        # 1. Process Attributes
        attr_fields = {}
        if hasattr(elem.type, "attributes"):
            attributes_nullable = False
            for attr in elem.type.attributes.values():
                pyarrow_type, casting_exp, validation_exp = (
                    map_xsd_simple_type_to_arrow(attr.type)
                )
                attr_nullable = attr.use == "optional"
                if attr_nullable:
                    attributes_nullable = True

                attr_fields[attr.name] = (
                    pyarrow_type,
                    attr_nullable,
                    casting_exp,
                    validation_exp,
                )

        # 2. Process Simple Content
        if elem.type.has_simple_content():
            pyarrow_type, casting_exp, validation_exp = map_xsd_simple_type_to_arrow(
                elem.type
            )
            if attr_fields:
                # add simple list of dict for attributes and elements
                parent_xpath_elem = xpath_elem.add_child(
                    elem.local_name,
                    True,
                    is_list,
                    True,
                    None,
                    nullable,
                    None,
                    None,
                )

                # add @attributes dict
                attr_group_name = elem.local_name + "@attributes"
                attr_group = parent_xpath_elem.add_child(
                    attr_group_name,
                    False,
                    False,
                    True,
                    None,
                    attributes_nullable,
                    None,
                    None,
                )
                # add attributes items as simple types
                for attr_name, attr_values in attr_fields.items():
                    attr_group.add_child(
                        attr_name,
                        True,
                        False,
                        False,
                        *attr_values,
                    )

                # add simple type for element
                parent_xpath_elem.add_child(
                    elem.local_name,
                    True,
                    False,
                    False,
                    pyarrow_type,
                    nullable,
                    casting_exp,
                    validation_exp,
                )

            else:
                # simple type
                parent_xpath_elem = xpath_elem.add_child(
                    elem.local_name,
                    True,
                    is_list,
                    False,
                    pyarrow_type,
                    nullable,
                    casting_exp,
                    validation_exp,
                )

        # 3. Process Mixed and Complex Content
        elif elem.type.has_complex_content() or elem.type.has_mixed_content():
            # complex dict
            parent_xpath_elem = xpath_elem.add_child(
                elem.local_name,
                False,
                is_list,
                True,
                None,
                nullable,
                None,
                None,
            )

            if attr_fields:
                # add @attributes dict
                attr_group_name = elem.local_name + "@attributes"
                attr_group = parent_xpath_elem.add_child(
                    attr_group_name,
                    False,
                    False,
                    True,
                    None,
                    attributes_nullable,
                    None,
                    None,
                )
                # add attributes items as simple types
                for attr_name, attr_values in attr_fields.items():
                    attr_group.add_child(
                        attr_name,
                        True,
                        False,
                        False,
                        *attr_values,
                    )

            if elem.type.has_mixed_content():
                pyarrow_type, casting_exp, validation_exp = (
                    map_xsd_simple_type_to_arrow(elem.type)
                )
                # add simple type for element value
                parent_xpath_elem.add_child(
                    elem.local_name,
                    True,
                    False,
                    False,
                    pyarrow_type,
                    nullable,
                    casting_exp,
                    validation_exp,
                )

            if elem.type.has_complex_content() or elem.type.has_mixed_content():
                # add complex child elements
                child_counter = 0
                for child_elem in elem.type.content.iter_elements():
                    old_recursion_check_list = recursion_check_list
                    if child_elem.type.is_complex() and child_elem.type.name:
                        # add element type name to recursion counter in case child elements come up more than twice (by default).
                        recursion_check_list = recursion_check_list + [
                            child_elem.type.name
                        ]
                        if (
                            recursion_check_list.count(child_elem.type.name)
                            > max_recursion
                        ):
                            recursion_check_list = old_recursion_check_list
                            continue
                    child_counter += 1

                    convert_xsd_elem(
                        child_elem,
                        parent_xpath_elem,
                        max_recursion,
                        recursion_check_list,
                    )

                    recursion_check_list = old_recursion_check_list

                # add a simple type tail for every child element
                if elem.type.has_mixed_content():
                    parent_xpath_elem.add_child(
                        child_elem.local_name + "@tail",
                        True,
                        False,
                        False,
                        pa.string(),
                        nullable,
                        None,
                        None,
                    )

                # Edge case if all children fail recursion. Remove the parent which is empty.
                if elem.type.has_complex_content() and child_counter == 0:
                    xpath_elem.remove_child(elem.local_name)


def convert_xsd_to_xpath_tree(xsd_schema, max_recursion=2):

    xpath_root = XmlElement("root", [], False, False, True, None, True, None, None, None)

    for elem in xsd_schema.elements.values():
        if not elem.is_global():
            continue

        convert_xsd_elem(
            elem,
            xpath_root,
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


def convert_xpath_tree_to_schema_type(
    xpath_root, xpaths=None
):
    xpath_root.set_field_pyarrow_type()

    if xpaths:
        schema_type = xpath_root.find_elem(xpaths).field_pyarrow_type
    else:
        schema_type = xpath_root.field_pyarrow_type

    while pa.types.is_list(schema_type):
        schema_type = schema_type.value_type
    
    return schema_type
