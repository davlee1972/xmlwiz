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

from datetime import datetime
import isodate
from decimal import Decimal
import pyarrow as pa
import pyarrow.compute as pc

from xmlwiz.mappings import (
    XpathTypeEnum,
    XpathValueEnum,
    ElementTypeEnum,
    XSD_TO_PYARROW,
    XSD_TO_ELEMENT_DECODE,
)


def element_decode_type(elem_text, element_type):
    # handles decoding element types to python types compatible with pyarrow types

    if isinstance(element_type, tuple) and element_type[0] == ElementTypeEnum.LIST:
        elem_list = elem_text.split(" ")
        elem_list = [
            element_element_type(elem_item, element_type[1]) for elem_item in elem_list
        ]
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
        return datetime.strptime(elem_text, "%H:%M:%S %z").time()
    elif element_type == ElementTypeEnum.GEGORIAN:
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
        local_name = base_type.item_item.local_name
        element_type = XSD_TO_ELEMENT_DECODE.get(local_name, ElementTypeEnum.STRING)
        pyarrow_type = XSD_TO_PYARROW.get(local_name, pa.string())
        if pyarrow_type == "numeric":
            pyarrow_type = pyarrow_numeric(xsd_type)
        pyarrow_validation = None
        return (
            (ElementTypeEnum.LIST, element_type),
            pa.list_(pyarrow_type),
            pyarrow_validation,
        )
    else:
        element_type = XSD_TO_ELEMENT_DECODE.get(local_name, ElementTypeEnum.OTHER)
        pyarrow_type = XSD_TO_PYARROW.get(local_name, pa.string())
        if pyarrow_type == "numeric":
            pyarrow_type = pyarrow_numeric(xsd_type)
        pyarrow_validation = None
        return element_type, pyarrow_type, pyarrow_validation


def convert_xsd_elem(elem, xpath_index, xpath, max_recursion, recursion_check_list):
    # Handle simple types (scalars)

    nullable = elem.min_occurs == 0

    xpath_key = tuple(xpath + [elem.local_name])
    level = len(xpath) + 1

    if level not in xpath_index:
        xpath_index[level] = {}

    if elem.type.is_simple():
        element_type, pyarrow_type, pyarrow_validation = map_xsd_type_to_arrow(
            elem.type
        )

        xpath_index[level][xpath_key] = (
            elem.local_name,
            element_type,
            pyarrow_type,
            nullable,
            None,
            None,
            None,
            pyarrow_validation,
            [None, None, None, None],
        )
    else:
        # prevents recursion. default is 2 Levels.
        if recursion_check_list.count(elem.type.name) >= max_recursion:
            return

        # 1. Process Attributes
        # Access the attribute group associated with the complex type
        if hasattr(elem.type, "attributes"):
            attr_fields = {}
            attributes_nullable = True
            for attr in elem.type.attributes.values():
                attr_xpath_key = tuple(
                    xpath + [elem.local_name, "@attributes", attr.name]
                )
                element_type, pyarrow_type, pyarrow_validation = map_xsd_type_to_arrow(
                    attr.type
                )
                attr_nullable = attr.use == "optional"
                if not attr_nullable:
                    attributes_nullable = False
                attr_fields[attr_xpath_key] = (
                    attr.name,
                    element_type,
                    pyarrow_type,
                    attr_nullable,
                    None,
                    None,
                    None,
                    pyarrow_validation,
                    [None, None, None, None],
                )

            if attr_fields:
                if level + 1 not in xpath_index:
                    xpath_index[level + 1] = {}
                if level + 2 not in xpath_index:
                    xpath_index[level + 2] = {}

                xpath_index[level + 1][
                    tuple(xpath + [elem.local_name, "@attributes"])
                ] = (
                    "@attributes",
                    ElementTypeEnum.DICT,
                    None,
                    attributes_nullable,
                    None,
                    None,
                    None,
                    None,
                    [None, None, None, None],
                )

                xpath_index[level + 2].update(attr_fields)

        # 2. Process Content
        if elem.max_occurs is None or elem.max_occurs > 1:
            xpath_index[level][xpath_key] = (
                elem.local_name,
                ElementTypeEnum.LIST_OF_DICT,
                None,
                nullable,
                None,
                None,
                None,
                None,
                [None, None, None, None],
            )
        else:
            xpath_index[level][xpath_key] = (
                elem.local_name,
                ElementTypeEnum.DICT,
                None,
                nullable,
                None,
                None,
                None,
                None,
                [None, None, None, None],
            )

        # 3. Process Simple Content
        if elem.type.has_simple_content():
            element_type, pyarrow_type, pyarrow_validation = map_xsd_type_to_arrow(
                elem.type
            )
            xpath_index[level + 1][
                tuple(xpath + [elem.local_name, elem.local_name])
            ] = (
                elem.local_name,
                element_type,
                pyarrow_type,
                nullable,
                None,
                None,
                None,
                pyarrow_validation,
                [None, None, None, None],
            )

        # 3. Process Mixed and Complex Content
        elif elem.type.has_complex_content() or elem.type.has_mixed_content():
            if elem.type.has_mixed_content():
                element_type, pyarrow_type, pyarrow_validation = map_xsd_type_to_arrow(
                    elem.type
                )
                xpath_index[level + 1][
                    tuple(xpath + [elem.local_name, elem.local_name])
                ] = (
                    elem.local_name,
                    element_type,
                    pyarrow_type,
                    nullable,
                    None,
                    None,
                    None,
                    pyarrow_validation,
                    [None, None, None, None],
                )

            if elem.type.name:
                # add element type name to recursion counter in case child elements contain this element type
                recursion_check_list.append(elem.type.name)

            for child_elem in elem.type.content.iter_elements():
                convert_xsd_elem(
                    child_elem,
                    xpath_index,
                    xpath + [elem.local_name],
                    max_recursion,
                    recursion_check_list,
                )


def convert_xsd_to_xpath_index(xsd_schema, max_recursion=2):

    xpath_index = {
        0: {
            (): (
                None,
                ElementTypeEnum.DICT,
                None,
                False,
                None,
                None,
                None,
                None,
                [None, None, None, None],
            )
        }
    }
    for elem in xsd_schema.elements.values():
        if not elem.is_global():
            continue
        convert_xsd_elem(
            elem,
            xpath_index,
            xpath=[],
            max_recursion=max_recursion,
            recursion_check_list=[],
        )

    # Each xml file should only have one root element
    # However, we may have to merge different root elements across files.
    # Make all root elements nullable to enable root schema merging.
    if len(xpath_index[1]) > 1:
        for key, xpath_type in xpath_index[1].items():
            xpath_type = list(xpath_type)
            xpath_type[XpathTypeEnum.NULLABLE] = True
            xpath_type = tuple(xpath_type)
            xpath_index[1][key] = xpath_type

    # assign parents
    for level in list(xpath_index.keys())[:-1]:
        for parent, xpath_type in xpath_index[
            level
        ].items():  # for each parent assign child to parent
            for child, child_type in xpath_index[level + 1].items():
                if child[:-1] == parent:
                    child_type = list(child_type)
                    child_type[XpathTypeEnum.PARENT] = xpath_index[level][parent]
                    child_type = tuple(child_type)
                    xpath_index[level + 1][child] = child_type

    # assign children
    for level in reversed(list(xpath_index.keys())[1:]):
        grouped_dict = {}

        for child, child_type in xpath_index[level].items():
            parent = child[:-1]
            grouped_dict.setdefault(parent, []).append(child_type)

        for parent, children in grouped_dict.items():
            parent_type = xpath_index[level - 1][parent]
            parent_type = list(parent_type)
            parent_type[XpathTypeEnum.CHILDREN] = children
            parent_type = tuple(parent_type)
            xpath_index[level - 1][parent] = parent_type

    return xpath_index


def convert_xpath_index_to_pyarrow_schema(xpath_index):
    # convert xpath index to pyarrow schema
    for level in reversed(list(xpath_index.keys())):
        for child, child_type in xpath_index[level].items():
            if (
                isinstance(child_type[XpathTypeEnum.ELEMENT_TYPE], tuple)
                and child_type[XpathTypeEnum.ELEMENT_TYPE][0] == ElementTypeEnum.LIST
            ):
                xpath_index[level][child][XpathTypeEnum.VALUE][
                    XpathValueEnum.FIELD_TYPE
                ] = pa.list_(child_type[XpathTypeEnum.PYARROW_TYPE])

            elif child_type[XpathTypeEnum.ELEMENT_TYPE] in [
                ElementTypeEnum.DICT,
                ElementTypeEnum.LIST_OF_DICT,
            ]:
                struct_type = pa.struct(
                    [
                        pa.field(
                            subchild[XpathTypeEnum.NAME],
                            subchild[XpathTypeEnum.VALUE][XpathValueEnum.FIELD_TYPE],
                            nullable=subchild[XpathTypeEnum.NULLABLE],
                        )
                        for subchild in child_type[XpathTypeEnum.CHILDREN]
                    ]
                )

                if child_type[XpathTypeEnum.ELEMENT_TYPE] == ElementTypeEnum.DICT:
                    xpath_index[level][child][XpathTypeEnum.VALUE][
                        XpathValueEnum.FIELD_TYPE
                    ] = struct_type
                elif (
                    child_type[XpathTypeEnum.ELEMENT_TYPE]
                    == ElementTypeEnum.LIST_OF_DICT
                ):
                    xpath_index[level][child][XpathTypeEnum.VALUE][
                        XpathValueEnum.FIELD_TYPE
                    ] = pa.list_(struct_type)
            else:
                xpath_index[level][child][XpathTypeEnum.VALUE][
                    XpathValueEnum.FIELD_TYPE
                ] = child_type[XpathTypeEnum.PYARROW_TYPE]

    return xpath_index[0][()][XpathTypeEnum.VALUE][XpathValueEnum.FIELD_TYPE]
