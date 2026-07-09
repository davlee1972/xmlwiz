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


# compute expressions for element text to pyarrow
# temporary enum will be replaced with a class that contains pyarrow.compute.Expression(s)
class ComputeType(IntEnum):
    LIST = 1
    DURATION = 2
    TIMESTAMP = 3
    TIME = 4
    GEGORIAN = 5
    LESS = 6
    LESS_EQUAL = 7
    GREATER = 8
    GREATER_EQUAL = 9
    UTF8_TRIM_WHITESPACE = 10
    REPLACE_SUBSTRING_REGEX = 11


# used to convert element text to pyarrow types
# this is needed if pyarrow cannot cast string values directly to pyarrow types
XSD_TO_COMPUTE_DECODE = {
    "time": ComputeType.TIME,
    "dateTime": ComputeType.TIMESTAMP,
    "duration": ComputeType.DURATION,
    "gYearMonth": ComputeType.GEGORIAN,
    "gYear": ComputeType.GEGORIAN,
    "gMonthDay": ComputeType.GEGORIAN,
    "gDay": ComputeType.GEGORIAN,
    "gMonth": ComputeType.GEGORIAN,
}

FACET_TO_COMPUTE_DECODE = {
    "maxExclusive": ComputeType.LESS,
    "maxInclusive": ComputeType.LESS_EQUAL,
    "minExclusive": ComputeType.GREATER,
    "minInclusive": ComputeType.GREATER_EQUAL,
    "whitespace|collapse": ComputeType.UTF8_TRIM_WHITESPACE,
    "whitespace|replace": ComputeType.REPLACE_SUBSTRING_REGEX,
}

# pyarrow type for gegorian periods
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
