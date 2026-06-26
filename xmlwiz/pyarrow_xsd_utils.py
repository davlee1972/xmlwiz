#
# MIT License
#
# $Copyright (c) 2026 David Lee
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

import pyarrow as pa

import xmlschema

from xmlwiz.mappings import ElementTypeEnum, XSD_TO_PYARROW, gegorianPeriod


def build_action_items(obj, current_keys, xml_schema):
    action_items = {}
    current_xpath = "/" + "/".join(current_keys)
    for i, name in enumerate(obj.names):
        action_key = tuple(current_keys + [name])
        field_type = obj.field(i).type
        if pa.types.is_struct(field_type):
            if map_xsd_type_to_arrow(xml_schema.find(current_xpath + "/" + name).type) == gegorianPeriod:
                action_items[action_key] = ElementTypeEnum.GEGORIAN
            else:
                action_items[action_key] = ElementTypeEnum.DICT
                action_items.update(build_action_items(field_type, current_keys + [name], xml_schema))
        elif (
            pa.types.is_list(field_type)
            or pa.types.is_large_list(field_type)
            or pa.types.is_list_view(field_type)
            or pa.types.is_large_list_view(field_type)
        ):

            if name.startswith(current_keys[-1]):
                xpath_name = name[len(current_keys[-1]):]
            else:
                xpath_name = None

            current_xpath = "/" + "/".join(current_keys)

            if xml_schema.find(current_xpath + "/" + name) and xml_schema.find(current_xpath + "/" + name).type.is_list():
                action_items[action_key] = ElementTypeEnum.STRING_LIST
            elif xpath_name and xpath_name in xml_schema.find(current_xpath).attributes and xml_schema.find(current_xpath).attributes[xpath_name].type.is_list():
                action_items[action_key] = ElementTypeEnum.STRING_LIST
            elif pa.types.is_struct(field_type.value_type):
                action_items[action_key] = ElementTypeEnum.LIST_OF_DICT
                action_items.update(
                    build_action_items(field_type.value_type, current_keys + [name], xml_schema)
                )
            else:
                action_items[action_key] = ElementTypeEnum.LIST
        else:
            if pa.types.is_duration(field_type):
                action_items[action_key] = ElementTypeEnum.DURATION
            elif pa.types.is_timestamp(field_type):
                action_items[action_key] = ElementTypeEnum.TIMESTAMP
            elif pa.types.is_time(field_type):
                action_items[action_key] = ElementTypeEnum.TIME
            else:
                action_items[action_key] = ElementTypeEnum.STRING
    return action_items


def get_base_type(parent_type):
    if hasattr(parent_type, "base_type") and parent_type.base_type:
        return get_base_type(parent_type.base_type)
    else:
        return parent_type


def map_xsd_type_to_arrow(xsd_type):

    xsd_local_type = get_base_type(xsd_type)

    if xsd_local_type.is_list():
        return pa.list_(
            XSD_TO_PYARROW.get(xsd_local_type.item_type.local_name, pa.string())
        )

    return XSD_TO_PYARROW.get(
        xsd_local_type.local_name, pa.string()
    )  # Fallback to string for unknown primitives


def convert_xsd_type(elem):
    # Handle simple types (scalars)

    nullable = elem.min_occurs == 0

    if elem.type.is_simple():
        return map_xsd_type_to_arrow(elem.type), nullable

    # Handle complex types (structs)
    if elem.type.is_complex():
        fields = []

        # 1. Process Attributes
        # Access the attribute group associated with the complex type
        if hasattr(elem.type, "attributes"):
            attr_fields = []
            for attr_name, attr_obj in elem.type.attributes.items():
                # Attributes are always scalars
                attr_type = map_xsd_type_to_arrow(attr_obj.type)
                fields.append(pa.field(elem.tag + attr_name, attr_type, nullable=True))


        # 2. Process Child Elements
        if hasattr(elem.type, "content") and hasattr(
            elem.type.content, "iter_elements"
        ):

            for child_elem in elem.type.content.iter_elements():
                child_type, child_nullable = convert_xsd_type(child_elem)
                fields.append(pa.field(child_elem.name, child_type, nullable=child_nullable))
            
        if elem.max_occurs is None or elem.max_occurs > 1:
            return pa.list_(pa.struct(fields)), nullable

        return pa.struct(fields), nullable


def convert_xsd_to_pyarrow(xml_schema):
    schema_fields = []
    for name, elem in xml_schema.elements.items():
        if not elem.is_global:
            continue

        field_type, field_nullable = convert_xsd_type(elem)
        schema_fields.append(pa.field(name, field_type, nullable=field_nullable))

    return pa.schema(schema_fields)

