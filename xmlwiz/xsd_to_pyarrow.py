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
from typing import Any, Iterator

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
    field_flat: bool | XmlElement = False

    data_vector: list[str] = field(default_factory=list)
    data_offsets: list[int | None] = field(default_factory=lambda: [0])
    data_counter: int = 0
    data_pyarrow: pa.Array = None

    def add_child(
        self,
        name: str,
        is_simple: bool,
        is_list: bool,
        is_dict: bool,
        pyarrow_type: pa.DataType | None,
        nullable: bool | None,
        casting_exp: list[pc.Expression] | None,
        validation_exp: list[pc.Expression] | None,
    ) -> XmlElement:
        """
        Create and attach a child XmlElement.

        Parameters
        ----------
        name : str
            Name of the child element.
        is_simple : bool
            Whether the child is a simple value.
        is_list : bool
            Whether the child represents a list of values.
        is_dict : bool
            Whether the child represents a dictionary structure.
        pyarrow_type : pyarrow.DataType or None
            Target Arrow data type.
        nullable : bool or None
            Whether the child is nullable.
        casting_exp : list[pyarrow.compute.Expression] or None
            Optional casting expressions.
        validation_exp : list[pyarrow.compute.Expression] or None
            Optional validation expressions.

        Returns
        -------
        XmlElement
            Newly created child element.
        """

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

    def remove_child(self, name: str) -> None:
        """
        Remove a named child element and all its descendants.

        Parameters
        ----------
        name : str
            Name of the child element to remove.
        """
        if self.children[name].children:
            for child_name in list(self.children[name].children):
                self.children[name].remove_child(child_name)
        del self.children[name]

    def iter_elem(self) -> Iterator[XmlElement]:
        """
        Iterate over this element and all descendants.

        Yields
        ------
        XmlElement
            Each XmlElement in the tree.
        """
        yield self
        for child_elem in self.children.values():
            yield from child_elem.iter_elem()

    def find_elem(self, xml_path: str | list[str]) -> XmlElement | None:
        """
        Find an element by XPath path segments.

        Parameters
        ----------
        xml_path : str or list[str]
            XPath path string or list of segments.

        Returns
        -------
        XmlElement or None
            Matching XmlElement if found.
        """

        if isinstance(xml_path, str):
            xpaths = xml_path.split("/")
            xpaths = xpaths[1:]
        else:
            xpaths = xml_path

        for xpath_elem in self.iter_elem():
            if xpath_elem.xpaths == xpaths:
                return xpath_elem

    def display_tree(self) -> list[dict[str, Any]]:
        """
        Return a serializable representation of the XML element tree.

        Returns
        -------
        list[dict[str, Any]]
            List of element metadata dictionaries.
        """
        elem_list = []
        for item in self.iter_elem():
            elem_list.append(
                {
                    "name": item.name,
                    "xpaths": item.xpaths,
                    "is_simple": item.is_simple,
                    "is_list": item.is_list,
                    "is_dict": item.is_dict,
                    "pyarrow_type": item.pyarrow_type,
                    "nullable": item.nullable,
                    "parent": item.parent.name if item.parent else None,
                    "children": item.children.keys(),
                    "field_name": item.field_name,
                    "field_flat": item.field_flat,
                }
            )
        return elem_list

    def get_data_vector(self) -> dict[tuple[str, ...], list[str]]:
        """
        Return a mapping of XPath tuples to element data vectors.

        Returns
        -------
        dict[tuple[str, ...], list[str]]
            Mapping of XPath segment tuples to data vectors.
        """
        data = {}
        for xpath_elem in self.iter_elem():
            data[tuple(xpath_elem.xpaths)] = xpath_elem.data_vector
        return data

    def clear_data(self) -> None:
        """
        Reset runtime data on every element in the tree.
        """
        for elem in self.iter_elem():
            elem.data_vector = []
            elem.data_offsets = [0]
            elem.data_counter = 0
            elem.data_pyarrow = None

    def reset_fields(self) -> None:
        """
        Reset field metadata for every element in the tree.
        """
        for elem in self.iter_elem():
            elem.field_name = elem.name
            elem.field_pyarrow_type = elem.pyarrow_type
            elem.field_flat = False

    def trim_elements(self, xpaths: list[str]) -> None:
        """
        Remove non-matching child elements based on a specific XPath.

        Parameters
        ----------
        xpaths : list[str]
            XPath segments to keep.
        """
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

    def flatten_attributes(self) -> None:
        """
        Flatten attribute groups into their parent elements.
        """
        for xpath_elem in self.iter_elem():
            if xpath_elem.name.endswith("@attributes"):
                xpath_elem.field_flat = True
                # add all child items as replacement
                for child_elem in xpath_elem.children.values():
                    child_elem.field_name = (
                        xpath_elem.name.removesuffix("attributes") + child_elem.name
                    )
                    if xpath_elem.parent.nullable:
                        child_elem.nullable = True

    def flatten_elements(self) -> None:
        """
        Flatten elements with a single child into their child element.
        """
        for xpath_elem in self.iter_elem():
            if xpath_elem.name.endswith("@attributes"):
                continue

            child_count = len(xpath_elem.children)
            if child_count in (1, 2):
                child_attributes = False
                skip_to_elem = None
                for child, child_elem in xpath_elem.children.items():
                    # check if @attributes are skippable
                    if child == xpath_elem.name + "@attributes":
                        # add all child items
                        child_attributes = True
                        continue
                    # return if there are two items found
                    if skip_to_elem:
                        skip_to_elem = None
                        break
                    skip_to_elem = child_elem

                if child_attributes:
                    # add all child items as replacement
                    xpath_elem.field_flat = True
                    if skip_to_elem:
                        skip_to_elem.field_name = xpath_elem.field_name
                        if xpath_elem.nullable:
                            skip_to_elem.nullable = True
                            xpath_elem.children[
                                xpath_elem.name + "@attributes"
                            ].nullable = True
                elif skip_to_elem:
                    # replace item with skip to element
                    xpath_elem.field_flat = skip_to_elem

    # convert xpath element to pyarrow type
    def set_field_pyarrow_type(self) -> None:
        """
        Set the PyArrow type for each field in the tree.

        This method resolves nested structs and list types recursively.
        """
        # process data types in reverse. parent data types may be structs of child data types which in turn may also be structs.
        for xpath_elem in reversed(list(self.iter_elem())):
            if xpath_elem.field_flat and xpath_elem.field_flat != True:
                xpath_elem.field_pyarrow_type = xpath_elem.field_flat.field_pyarrow_type
                continue

            if (
                xpath_elem.is_simple
                and not xpath_elem.is_dict
                and ComputeType.LIST in xpath_elem.casting_exp
            ):
                xpath_elem.field_pyarrow_type = pa.list_(xpath_elem.field_pyarrow_type)

            if xpath_elem.is_dict:
                # merge in skipped fields
                struct_fields = []
                for k, v in xpath_elem.children.items():
                    if v.field_flat == True and not v.is_list:
                        struct_fields += v.field_pyarrow_type.fields
                    else:
                        struct_fields.append(
                            pa.field(
                                v.field_name, v.field_pyarrow_type, nullable=v.nullable
                            )
                        )
                xpath_elem.field_pyarrow_type = pa.struct(struct_fields)

            if xpath_elem.is_list:
                xpath_elem.field_pyarrow_type = pa.list_(xpath_elem.field_pyarrow_type)


def pyarrow_numeric(xsd_type: Any) -> pa.DataType:
    """
    Determine the numeric PyArrow type for an XSD numeric restriction.

    Parameters
    ----------
    xsd_type : Any
        XSD type object to inspect.

    Returns
    -------
    pyarrow.DataType
        Appropriate Arrow numeric type.
    """
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


def map_xsd_type_to_arrow(
    xsd_type: Any,
) -> tuple[pa.DataType, list[ComputeType], list[pc.Expression]]:
    """
    Map a simple XSD type to an Arrow type and casting expressions.

    Parameters
    ----------
    xsd_type : Any
        XSD type object.

    Returns
    -------
    tuple[pyarrow.DataType, list[ComputeType], list[pyarrow.compute.Expression]
        The Arrow type, casting expressions, and validation expressions.
    """
    # returns
    # pyarrow_type pyarrow type to transform from element text or python type
    # casting exp - pyarrow compute expressions to cast string to pyarrow types
    # validation exp - pyarrow compute expressions to validate vectors

    casting_exp = []
    validation_exp = []

    # if xsd_type.is_restriction():
    if xsd_type.is_simple():
        local_name = xsd_type.local_name
        base_type = xsd_type
    else:
        local_name = xsd_type.base_type.local_name
        base_type = xsd_type.base_type

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


def convert_xsd_elem(
    elem: Any,
    xpath_elem: XmlElement,
    max_recursion: int,
    recursion_check_list: list[str],
) -> None:
    """
    Convert an XSD element into an XPath tree node.

    Parameters
    ----------
    elem : Any
        XSD element object.
    xpath_elem : XmlElement
        Current tree node to attach children to.
    max_recursion : int
        Maximum recursion depth for repeated complex types.
    recursion_check_list : list[str]
        Recursion tracking list of complex type names.
    """

    nullable = elem.min_occurs == 0

    if elem.max_occurs is None or elem.max_occurs > 1:
        is_list = True
    else:
        is_list = False

    if elem.type.is_simple():
        pyarrow_type, casting_exp, validation_exp = map_xsd_type_to_arrow(elem.type)

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
                pyarrow_type, casting_exp, validation_exp = map_xsd_type_to_arrow(
                    attr.type
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
            pyarrow_type, casting_exp, validation_exp = map_xsd_type_to_arrow(elem.type)
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
                pyarrow_type, casting_exp, validation_exp = map_xsd_type_to_arrow(
                    elem.type
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


def convert_xsd_to_xpath_tree(xsd_schema: Any, max_recursion: int = 2) -> XmlElement:
    """
    Build an XPath tree representation from an XSD schema.

    Parameters
    ----------
    xsd_schema : Any
        Parsed XSD schema object.
    max_recursion : int, default 2
        Maximum recursion depth for repeated complex types.

    Returns
    -------
    XmlElement
        Root of the generated XPath tree.
    """

    xpath_root = XmlElement(
        "root", [], False, False, True, None, True, None, None, None
    )

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
    xpath_root: XmlElement,
    xpaths: list[str] | None = None,
) -> pa.DataType:
    """
    Convert an XPath tree into a PyArrow schema type.

    Parameters
    ----------
    xpath_root : XmlElement
        Root of the XPath tree.
    xpaths : list[str] or None, default None
        Optional specific XPath path to use for the schema.

    Returns
    -------
    pyarrow.DataType
        Schema type produced from the XPath tree.
    """
    xpath_root.set_field_pyarrow_type()

    if xpaths:
        schema_type = xpath_root.find_elem(xpaths).field_pyarrow_type
    else:
        if xpath_root.field_flat:
            xpath_elem = xpath_root
            while xpath_elem.field_flat:
                xpath_elem = xpath_elem.field_flat
            schema_type = xpath_elem.field_pyarrow_type
        else:
            schema_type = xpath_root.field_pyarrow_type

    while pa.types.is_list(schema_type):
        schema_type = schema_type.value_type

    return schema_type
