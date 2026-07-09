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

from xmlwiz.mappings import ComputeType


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
                new_vector.append(
                    {"yyyy": int(date_parts[0]), "mm": int(date_parts[1])}
                )
            elif date_len == 3:
                new_vector.append({"mm": int(date_parts[2])})
            elif date_len == 4:
                if date_parts[2]:
                    new_vector.append(
                        {"mm": int(date_parts[2]), "dd": int(date_parts[3])}
                    )
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

    if xpath_root.data_counter == 0:
        xpath_root.data_counter = 1

    for xpath_elem in xpath_root.iter_elem():
        if xpath_elem.data_vector:
            if xpath_elem.is_simple:
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


def set_pyarrow_data(xpath_root, full_schema):

    for xpath_elem in reversed(list(xpath_root.iter_elem())):
        if xpath_elem.data_counter == 0 and not full_schema:
            continue

        if xpath_elem.field_skip:
            if not xpath_elem.name.endswith("@attributes"):
                child_elem = next(iter(xpath_elem.children.values()))
                xpath_elem.data_pyarrow = child_elem.data_pyarrow
            continue

        if xpath_elem.is_dict:
            if full_schema:
                data = {
                    k: pa.concat_arrays(
                        [
                            v.data_pyarrow,
                            pa.nulls(
                                xpath_elem.data_counter - len(v.data_pyarrow),
                                type=v.field_pyarrow_type,
                            ),
                        ]
                    )
                    if v.data_pyarrow
                    else pa.nulls(xpath_elem.data_counter, type=v.field_pyarrow_type)
                    for k, v in xpath_elem.children.items()
                    if not (v.field_skip and v.name == xpath_elem.name + "@attributes")
                }
                struct_fields = [
                    pa.field(v.field_name, v.field_pyarrow_type, nullable=v.nullable)
                    for v in xpath_elem.children.values()
                    if not (v.field_skip and v.name == xpath_elem.name + "@attributes")
                ]
            else:
                data = {
                    k: pa.concat_arrays(
                        [
                            v.data_pyarrow,
                            pa.nulls(
                                xpath_elem.data_counter - len(v.data_pyarrow),
                                type=v.data_pyarrow.type,
                            ),
                        ]
                    )
                    for k, v in xpath_elem.children.items()
                    if v.data_pyarrow
                }

                struct_fields = [
                    pa.field(v.field_name, v.data_pyarrow.type, nullable=v.nullable)
                    for v in xpath_elem.children.values()
                    if v.data_pyarrow
                ]

            # add in flattened attributes
            attributes = xpath_elem.name + "@attributes"
            if (
                attributes in xpath_elem.children
                and xpath_elem.children[attributes].field_skip
            ):
                attributes_elem = xpath_elem.children[attributes]

                if full_schema:
                    attributes_data = {
                        k: pa.concat_arrays(
                            [
                                v.data_pyarrow,
                                pa.nulls(
                                    xpath_elem.data_counter - len(v.data_pyarrow),
                                    type=v.field_pyarrow_type,
                                ),
                            ]
                        )
                        if v.data_pyarrow
                        else pa.nulls(
                            xpath_elem.data_counter, type=v.field_pyarrow_type
                        )
                        for k, v in attributes_elem.children.items()
                    }
                    attr_struct_fields = [
                        pa.field(
                            v.field_name, v.field_pyarrow_type, nullable=v.nullable
                        )
                        for v in attributes_elem.children.values()
                    ]
                else:
                    attributes_data = {
                        k: pa.concat_arrays(
                            [
                                v.data_pyarrow,
                                pa.nulls(
                                    xpath_elem.data_counter - len(v.data_pyarrow),
                                    type=v.data_pyarrow.type,
                                ),
                            ]
                        )
                        for k, v in attributes_elem.children.items()
                        if v.data_pyarrow
                    }
                    attr_struct_fields = [
                        pa.field(v.field_name, v.data_pyarrow.type, nullable=v.nullable)
                        for v in attributes_elem.children.values()
                        if v.data_pyarrow
                    ]

                if attributes_data:
                    attributes_data.update(data)
                    data = attributes_data
                    attr_struct_fields.extend(struct_fields)
                    struct_fields = attr_struct_fields

            data = pa.StructArray.from_arrays(
                arrays=data.values(), fields=struct_fields
            )
            xpath_elem.data_pyarrow = data

        if xpath_elem.is_list and xpath_elem.data_pyarrow:
            if xpath_elem.data_offsets[-1] != xpath_elem.data_counter:
                xpath_elem.data_offsets.append(xpath_elem.data_counter)

            data = pa.ListArray.from_arrays(
                xpath_elem.data_offsets[:-1]
                + [None]
                * (xpath_elem.parent.data_counter - len(xpath_elem.data_offsets) + 1)
                + [xpath_elem.data_offsets[-1]],
                xpath_elem.data_pyarrow,
            )
            xpath_elem.data_pyarrow = data
