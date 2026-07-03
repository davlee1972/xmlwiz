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

from enum import IntEnum
import pyarrow as pa


class ElementType(IntEnum):
    LIST = 1
    # Only used when flattening content with no attributes and a single element.
    # with max_occurs > 0.
    DICT = 2
    # content without max_occurs > 0.
    # or flattened content with no attributes and a single element.
    LIST_OF_DICT = 3
    # content with max_occurs > 0.
    DECIMAL = 4
    DURATION = 5
    DATE = 6
    TIMESTAMP = 7
    TIME = 8
    GEGORIAN = 9
    OTHER = 10


gegorianPeriod = pa.struct(
    [
        pa.field("yyyy", pa.int16(), nullable=True),
        pa.field("mm", pa.int8(), nullable=True),
        pa.field("dd", pa.int8(), nullable=True),
    ]
)

# Core mapping dictionary
XSD_TO_PYARROW = {
    # Signed Integers
    "byte": pa.int8(),
    "short": pa.int16(),
    "int": pa.int32(),
    "long": pa.int64(),
    "integer": pa.int64(),
    # Unsigned Integers
    "unsignedByte": pa.uint8(),
    "unsignedShort": pa.uint16(),
    "unsignedInt": pa.uint32(),
    "unsignedLong": pa.uint64(),
    # Special Constrained Integers (Mapped to standard physical types)
    "positiveInteger": "numeric",  # Constraint: >= 1
    "nonNegativeInteger": "numeric",  # Constraint: >= 0
    "negativeInteger": "numeric",  # Constraint: <= -1
    "nonPositiveInteger": "numeric",  # Constraint: <= 0
    # Floats & Decimals
    "float": pa.float32(),
    "double": pa.float64(),
    "decimal": "numeric",  # Defaults to (38,10)
    # Strings & Identifiers
    "string": pa.string(),
    "normalizedString": pa.string(),
    "token": pa.string(),
    "Name": pa.string(),
    "NCName": pa.string(),
    "NMTOKEN": pa.string(),
    "ID": pa.string(),
    "IDREF": pa.string(),
    "anyURI": pa.string(),
    "QName": pa.string(),
    # Binary
    "hexBinary": pa.binary(),
    "base64Binary": pa.binary(),
    # Boolean
    "boolean": pa.bool_(),
    # Temporal (Defaulting to standard microsecond resolution)
    "date": pa.date32(),
    "time": pa.time64("us"),
    "dateTime": pa.timestamp("us"),
    "duration": pa.duration("us"),
    "gYearMonth": gegorianPeriod,
    "gYear": gegorianPeriod,
    "gMonthDay": gegorianPeriod,
    "gDay": gegorianPeriod,
    "gMonth": gegorianPeriod,
}

# used to convert element text to python types
# this is needed if pyarrow cannot cast string values directly to pyarrow types
XSD_TO_ELEMENT_DECODE = {
    "decimal": ElementType.DECIMAL,
    "date": ElementType.DATE,
    "time": ElementType.TIME,
    "dateTime": ElementType.TIMESTAMP,
    "duration": ElementType.DURATION,
    "gYearMonth": ElementType.GEGORIAN,
    "gYear": ElementType.GEGORIAN,
    "gMonthDay": ElementType.GEGORIAN,
    "gDay": ElementType.GEGORIAN,
    "gMonth": ElementType.GEGORIAN,
}
