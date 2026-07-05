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


from datetime import datetime, time
import isodate
from decimal import Decimal
import pyarrow as pa
import pyarrow.compute as pc

from xmlwiz.mappings import ComputeType, ElementType


# temporary function to use python instead of pyarrow.compute
def xml_to_pyarrow(compute_type, data_vector, pyarrow_type):
    # handles decoding element text to pyarrow data
    if compute_type == ComputeType.LIST:
        data_vector = [elem_text.split(" ") for elem_text in data_vector]
        data_vector = pa.array(data_vector).cast(pa.list_(pyarrow_type))
    elif compute_type == ComputeType.DURATION:
        data_vector = [
            int(isodate.parse_duration(elem_text).total_seconds() * 1_000_000)
            for elem_text in data_vector
        ]
        data_vector = pa.array(data_vector).cast(pyarrow_type)
    elif compute_type == ComputeType.TIMESTAMP:
        data_vector = [datetime.fromisoformat(elem_text) for elem_text in data_vector]
        data_vector = pa.array(data_vector).cast(pyarrow_type)
    elif compute_type == ComputeType.TIME:
        new_vector = []
        for elem_text in data_vector:
            try:
                new_vector.append(time.fromisoformat(elem_text))
            except:
                new_vector.append(datetime.strptime(elem_text, "%H:%M:%S%z").time())
        data_vector = pa.array(new_vector).cast(pyarrow_type)
    elif compute_type == ComputeType.GEGORIAN:
        new_vector = []
        for elem_text in data_vector:
            date_parts = elem_text.split("-")
            date_len = len(date_parts)
            """
                <gYearMonthType>2026-06</gYear MonthType> <gYearType>2026</gYearType>
                <gMonthDayType>--06-23</gMonthDayType>
                <gDayType>---23</gDayType>
                <gMonthType>--86</gMonthType>
            """
            if date_len == 1:
                new_vector.append({"yyyy": int(date_parts[0])})
            elif date_len == 2:
                new_vector.append({"yyyy": int(date_parts[0]), "mm": int(date_parts[1])})
            elif date_len == 3:
                new_vector.append({"mm": int(date_parts[2])})
            elif date_len == 4:
                if date_parts[2]:
                    new_vector.append({"mm": int(date_parts[2]), "dd": int(date_parts[3])})
                else:
                    new_vector.append({"dd": int(date_parts[3])})
        data_vector = pa.array(new_vector).cast(pyarrow_type)

    return data_vector


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


def cast_vector_data(xpath_root):
    for xpath_elem in xpath_root.iter_elem():
        if xpath_elem.data_counter:
            if xpath_elem.element_type == ElementType.SIMPLE:
                if xpath_elem.parent and xpath_elem.parent.element_type in (
                    ElementType.DICT,
                    ElementType.LIST_OF_DICT,
                ):
                    missing_rows = (
                        xpath_elem.parent.data_counter - xpath_elem.data_counter
                    )
                    if missing_rows > 0:
                        xpath_elem.data_vector.extend([None] * missing_rows)
                        xpath_elem.data_counter += missing_rows

                if xpath_elem.casting_exp:
                    for compute_type in xpath_elem.casting_exp:
                        xpath_elem.data_pyarrow = xml_to_pyarrow(
                            compute_type,
                            xpath_elem.data_vector,
                            xpath_elem.pyarrow_type,
                        )
                else:
                    xpath_elem.data_pyarrow = pa.array(xpath_elem.data_vector).cast(
                        xpath_elem.pyarrow_type
                    )


def set_pyarrow_data(xpath_root):
    for xpath_elem in reversed(list(xpath_root.iter_field_elem())):
        if xpath_elem.field_element_type in (ElementType.DICT, ElementType.LIST_OF_DICT):
            data = {
                k: v.data_pyarrow
                for k, v in xpath_elem.field_children.items()
                if v.data_pyarrow
            }

            struct_fields = [
                pa.field(v.field_name, v.data_pyarrow.type, nullable=v.nullable)
                for v in xpath_elem.field_children.values()
                if v.data_pyarrow
            ]

            if data:
                data = pa.StructArray.from_arrays(
                    arrays=data.values(), fields=struct_fields
                )
                if xpath_elem.field_element_type == ElementType.LIST_OF_DICT:
                    xpath_elem.data_pyarrow = pa.ListArray.from_arrays(
                        xpath_elem.field_data_offsets, data
                    )
                else:
                    xpath_elem.data_pyarrow = data

        elif xpath_elem.field_element_type == ElementType.LIST:
            if xpath_elem.data_vector:
                xpath_elem.data_pyarrow = pa.ListArray.from_arrays(
                    xpath_elem.field_data_offsets, xpath_elem.data_vector
                )
