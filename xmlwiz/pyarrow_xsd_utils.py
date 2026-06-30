#
# MIT License
#
# Copyright (c) 2026 David Lee
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
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

from datetime import datetime
import isodate
from decimal import Decimal
import pyarrow as pa

from xmlwiz.mappings import ElementTypeEnum, XSD_TO_PYARROW, XSD_TO_ELEMENT_DECODE


def get_base_type(parent_type):
    if hasattr(parent_type, "base_type") and parent_type.base_type:
        return get_base_type(parent_type.base_type)
    else:
        return parent_type


def map_xsd_type_to_arrow(xsd_type):

    xsd_local_type = get_base_type(xsd_type)

    if xsd_local_type.is_list():
        decode = XSD_TO_ELEMENT_DECODE.get(xsd_local_type.item_type.local_name, ElementTypeEnum.STRING)
        return (ElementTypeEnum.LIST, decode), pa.list_(
            XSD_TO_PYARROW.get(xsd_local_type.item_type.local_name, pa.string())
        )

    decode = XSD_TO_ELEMENT_DECODE.get(xsd_local_type.local_name, ElementTypeEnum.STRING)
    return decode, XSD_TO_PYARROW.get(
        xsd_local_type.local_name, pa.string()
    )  # Fallback to string for unknown primitives


def convert_xsd_type(elem, action_index, xpath, elem_type_list):
    # Handle simple types (scalars)

    nullable = elem.min_occurs == 0

    xpath_key = tuple(xpath + [elem.local_name])
    level = len(xpath) + 1

    if level not in action_index:
        action_index[level] = {}

    if elem.type.is_simple():
        decode, pyarrow_type = map_xsd_type_to_arrow(elem.type)
        action_index[level][xpath_key] = (decode, pyarrow_type, nullable)
        return pyarrow_type, nullable
    else:
        # Handle complex types (structs)
        fields = []

        # prevents recursion using a depth of 2
        if elem_type_list.count(elem.type.name) >= 2:
            return None, None

        # 1. Process Attributes
        # Access the attribute group associated with the complex type
        if hasattr(elem.type, "attributes"):
            attr_fields = []
            for attr in elem.type.attributes.values():
                attr_xpath_key = tuple(xpath + [elem.local_name] + [attr.name] )
                decode, pyarrow_type = map_xsd_type_to_arrow(attr.type)
                if nullable:
                    attr_nullable = nullable
                else:
                    attr_nullable = attr.use != "required"
                action_index[level][attr_xpath_key] = (decode, pyarrow_type, attr_nullable)
                fields.append(pa.field(elem.local_name + "@" + attr.name, pyarrow_type, nullable=attr_nullable))

        # 2. Process Child Elements
        if hasattr(elem.type, "content") and hasattr(
            elem.type.content, "iter_elements"
        ):

            if elem.type.name:
                elem_type_list.append(elem.type.name)
            for child_elem in elem.type.content.iter_elements():
                child_type, child_nullable = convert_xsd_type(child_elem, action_index, xpath + [elem.local_name], elem_type_list)
                if child_type:
                    fields.append(pa.field(child_elem.local_name, child_type, nullable=child_nullable))

        if not fields:
            return None, None

        if elem.max_occurs is None or elem.max_occurs > 1:
            action_index[level][xpath_key] = (ElementTypeEnum.LIST, pa.list_(pa.struct(fields)), nullable)
            return pa.list_(pa.struct(fields)), nullable

        action_index[level][xpath_key] = (ElementTypeEnum.DICT, pa.struct(fields), nullable)
        return pa.struct(fields), nullable


def convert_xsd_to_pyarrow(xsd_schema, xpath):
    schema_fields = []
    action_index = {}
    for elem in xsd_schema.elements.values():

        if not elem.is_global():
            continue

        field_type, field_nullable = convert_xsd_type(elem, action_index, xpath=[], elem_type_list=[])
        schema_fields.append(pa.field(elem.local_name, field_type, nullable=field_nullable))

    pyarrow_schema = pa.schema(schema_fields)

    # Each xml file should only have one root element
    # However, we may have to merge different root elements across files.
    # Make all root elements nullable to enable root schema merging.
    if len(pyarrow_schema.names) > 1:
        pyarrow_schema = pa.schema(
            [column.with_nullable(True) for column in pyarrow_schema]
        )

    return pyarrow_schema, action_index


def element_decode(elem_text, element_type):
    # handles decoding element types to python types compatible with pyarrow types

    if isinstance(element_type, tuple) and element_type[0] == ElementTypeEnum.LIST:
        elem_list = elem_text.split(" ")
        elem_list = [element_decode(elem_item, element_type[1]) for elem_item in elem_list]
        return elem_list
    elif element_type == ElementTypeEnum.DECIMAL:
        return Decimal(elem_text)
    elif element_type == ElementTypeEnum.DURATION:
        dur = isodate.parse_duration(elem_text)
        microseconds = int(dur.total_seconds() * 1_000_000)
        return microseconds
    elif element_type == ElementTypeEnum.DATE:
        return datetime.fromisoformat(elem_text).date()
    elif element_type == ElementTypeEnum.TIMESTAMP:
        return datetime.fromisoformat(elem_text)
    elif element_type == ElementTypeEnum.TIME:
        return datetime.strptime(elem_text, "%H:%M:%S%z").time()
    elif element_type == ElementTypeEnum.GEGORIAN:
        date_parts = elem_text.split("-")
        date_len = len(date_parts)
        """
            <gYearMonthType>2026-06</gYearMonthType>
            <gYearType>2026</gYearType>
            <gMonthDayType>--06-23</gMonthDayType>
            <gDayType>---23</gDayType>
            <gMonthType>--06</gMonthType>
        """
        if date_len == 1:
            return {"yyyy": int(date_parts[0])}
        elif date_len == 2:
            return {"yyyy": int(date_parts[0]), 'mm': int(date_parts[1])}
        elif date_len == 3:
            return {"mm": int(date_parts[2])}
        elif date_len == 4:
            if date_parts[2]:
                return {"mm": int(date_parts[2]), 'dd': int(date_parts[3])}
            else:
                return {"dd": int(date_parts[3])}
        return datetime.strptime(elem_text, "%H:%M:%S%z").time()
    else:
        return elem_text