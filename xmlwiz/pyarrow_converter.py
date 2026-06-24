#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
from datetime import datetime
import isodate

from xmlschema.converters import ColumnarConverter

from typing import TYPE_CHECKING, Any

from xmlschema.aliases import BaseXsdType
from xmlschema.converters.base import ElementData

if TYPE_CHECKING:
    from xmlschema.validators import XsdElement

class PyArrowConverter(ColumnarConverter):
    """
    XML Schema based converter class for columnar formats.

    :param namespaces: map from namespace prefixes to URI.
    :param dict_class: dictionary class to use for decoded data. Default is `dict`.
    :param list_class: list class to use for decoded data. Default is `list`.
    :param attr_prefix: used as separator string for renaming the decoded attributes. \
    Can be the empty string (the default) or a single/double underscore.
    """
    __slots__ = ()

    def get_python_value(self, data_text, xsd_type):

        from xmlwiz.convert_xml import get_base_type

        local_type = get_base_type(xsd_type)
        if local_type.local_name == "date":
            return datetime.strptime(data_text, "%Y-%m-%d").date()
        elif local_type.local_name == "dateTime":
            return datetime.fromisoformat(data_text)
        elif local_type.local_name == "duration":
            dur = isodate.parse_duration(data_text)
            microseconds = int(dur.total_seconds() * 1_000_000)
            return microseconds
        elif local_type.local_name == "time":
            return datetime.strptime(data_text, "%H:%M:%S%z").time()
        else:
            return data_text

    def element_decode(self, data: ElementData, xsd_element: 'XsdElement',
                       xsd_type: BaseXsdType | None = None, level: int = 0) -> Any:
        result_dict: Any

        xsd_type = xsd_type or xsd_element.type
        if data.attributes:
            if self.attr_prefix:
                pfx = xsd_element.local_name + self.attr_prefix
            else:
                pfx = xsd_element.local_name
            result_dict = self.dict_class((pfx + self.map_qname(k), self.get_python_value(v, xsd_type.attributes[k].type)) for k, v in data.attributes if k in xsd_type.attributes)
        else:
            result_dict = self.dict_class()

        if xsd_type.simple_type is not None:
            result_dict[xsd_element.local_name] = self.get_python_value(data.text, xsd_type)

        if data.content:
            for name, value, xsd_child in self.map_content(data.content):
                if not value:
                    continue
                elif xsd_child.local_name:
                    name = xsd_child.local_name
                else:
                    name = name[2 + len(xsd_child.namespace):]

                if xsd_child.is_single():
                    if xsd_child.type is not None and xsd_child.type.simple_type is not None:
                        for k in value:
                            result_dict[k] = value[k]
                    else:
                        result_dict[name] = value
                else:
                    if xsd_child.type is not None and xsd_child.type.simple_type is not None \
                            and not xsd_child.attributes:
                        try:
                            result_dict[name].append(list(value.values())[0])
                        except KeyError:
                            result_dict[name] = self.list_class(value.values())
                        except AttributeError:
                            result_dict[name] = self.list_class(value.values())
                    else:
                        try:
                            result_dict[name].append(value)
                        except KeyError:
                            result_dict[name] = self.list_class([value])
                        except AttributeError:
                            result_dict[name] = self.list_class([value])

        if level == 0:
            return self.dict_class([(xsd_element.local_name, result_dict)])
        else:
            return result_dict

