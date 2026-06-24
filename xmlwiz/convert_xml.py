"""
(c) 2019 David Lee.

Author: David Lee
"""

import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime, date, time
import decimal
import json
import glob
from multiprocessing import Pool
import subprocess
import os
import gzip
import tarfile
import logging
import sys
from zipfile import ZipFile

import xmlschema
from xmlschema import XMLResource

from xmlwiz.pyarrow_converter import PyArrowConverter

_logger = logging.getLogger(__name__)
_logger.setLevel(logging.INFO)


def get_base_type(parent_type):
    if hasattr(parent_type, "base_type") and parent_type.base_type:
        return get_base_type(parent_type.base_type)
    else:
        return parent_type

def json_decoder(obj):
    """
    :param obj: python data
    :return: converted type
    :raises:
    """
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    elif isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M:%S.%f')
    elif isinstance(obj, date):
        return obj.strftime('%Y-%m-%d')
    elif isinstance(obj, time):
        return obj.isoformat()
    elif isinstance(obj, set):
        return list(obj)
    raise TypeError(repr(obj) + " is not JSON serializable")


def map_xsd_type_to_arrow(xsd_type):

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
        "positiveInteger": pa.uint64(),      # Constraint: >= 1
        "nonNegativeInteger": pa.uint64(),   # Constraint: >= 0
        "negativeInteger": pa.int64(),       # Constraint: <= -1
        "nonPositiveInteger": pa.int64(),    # Constraint: <= 0
        
        # Floats & Decimals
        "float": pa.float32(),
        "double": pa.float64(),
        "decimal": pa.decimal128(38, 10),    # Defaulting to a standard precision/scale
        
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
    }

    xsd_local_type = get_base_type(xsd_type)

    if xsd_local_type.is_list():
        return pa.list_(XSD_TO_PYARROW.get(xsd_local_type.item_type.local_name, pa.string()))

    return XSD_TO_PYARROW.get(xsd_local_type.local_name, pa.string())  # Fallback to string for unknown primitives


def xsd_element_to_arrow_field(xsd_element):
    """Recursively processes an XSD element node into a PyArrow Field."""

    result_dict = {}

    if hasattr(xsd_element, "attributes"):
        result_dict = {xsd_element.local_name + attr_name: (map_xsd_type_to_arrow(attr.type), True) for attr_name, attr in xsd_element.attributes.items()}

    # Check if the element contains nested children (complex type)
    if xsd_element.type.simple_type:
        nullable = xsd_element.min_occurs == 0
        result_dict[xsd_element.local_name] = (map_xsd_type_to_arrow(xsd_element.type), nullable)

    if hasattr(xsd_element.type, "content") and hasattr(xsd_element.type.content, "iter_elements"):
        for xsd_child in xsd_element.type.content.iter_elements():
            name = xsd_child.local_name
            child_type = xsd_element_to_arrow_field(xsd_child)

            if xsd_child.is_single():
                if xsd_child.type is not None and xsd_child.type.simple_type is not None:
                    for i in range(child_type.num_fields):
                        field = child_type.field(i)
                        result_dict[field.name] = (field.type, field.nullable)
                else:
                    child_nullable = xsd_child.min_occurs == 0
                    result_dict[name] = (child_type, child_nullable)
            else:
                if xsd_child.type is not None and xsd_child.type.simple_type is not None and not xsd_child.attributes:
                    result_dict[name] = (pa.list_(child_type.field(0).type), True)
                else:
                    result_dict[name] = (pa.list_(child_type), True)

    result_dict = pa.struct([pa.field(k, v[0], nullable=v[1]) for k, v in result_dict.items()])

    return result_dict


def convert_xml_schema_to_pyarrow_schema(schema, path=None):
    """Converts the XML Schema into a PyArrow Schema."""
    arrow_fields = []

    # Process each top-level root element defined in the XML schema
    if path:
        xsd_elem = schema.find(path)
        pa_type = xsd_element_to_arrow_field(xsd_elem)
        arrow_fields = [
            pa.field(sub.name, sub.type, nullable=sub.nullable)
                for sub in pa_type
        ]
    else:
        arrow_fields=[]
        for name, element in schema.elements.items():
            pa_type = xsd_element_to_arrow_field(element)
            arrow_fields.append(pa.field(name, pa_type, nullable=True))

    return pa.schema(arrow_fields)

def open_zip_file(zip, filename):
    """
    :param zip: whether to open a new file using gzip
    :param filename: name of new file
    :return: file handlers
    """
    if zip:
        return gzip.open(filename, "wb")
    else:
        return open(filename, "wb")


def write_json(output_file, zip, input_file, xsd_file, lazy, root, xpath, output_format, processed):

    _logger.info("Generating schema from " + xsd_file)

    xml_schema = xmlschema.XMLSchema11(xsd_file, converter=PyArrowConverter)

    _logger.info("Parsing " + input_file)

    _logger.info("Writing to file " + output_file)

    def parse_xml_to_json(xml_file, processed):
        """
        :param xml_file: xml file
        :param processed: whether data has been found and processed
        :return: data found and processed
        """

        xml_file_resource = XMLResource(xml_file, lazy=lazy, thin_lazy=True)
        
        if xpath:
            xml_iter_decode = xml_schema.iter_decode(xml_file_resource, path=xpath)
        else:
            xml_iter_decode = xml_schema.iter_decode(xml_file_resource)

        for xml_dict in xml_iter_decode:
            if root:
                xml_dict = xml_dict[root]

            xml_json = json.dumps(xml_dict, default=json_decoder)

            if len(xml_json) > 0:
                if not processed:
                    processed = True
                    file_obj.write(bytes(xml_json, "utf-8"))
                else:
                    if output_format == "json":
                        file_obj.write(bytes("," + os.linesep + xml_json, "utf-8"))
                    else:
                        file_obj.write(bytes(os.linesep + xml_json, "utf-8"))
        return processed

    with open_zip_file(zip, output_file) as file_obj:

        if input_file.endswith((".zip", ".tar.gz")) and output_format == "json":
            file_obj.write(bytes("[" + os.linesep, "utf-8"))

        if input_file.endswith(".tar.gz"):
            zip_file = tarfile.open(input_file, 'r')
            zip_file_list = zip_file.getmembers()

            for member in zip_file_list:
                with zip_file.extractfile(member) as xml_file:
                    processed = parse_xml_to_json(xml_file, processed)

        elif input_file.endswith(".zip"):
            zip_file = ZipFile(input_file, 'r')
            zip_file_list = zip_file.infolist()

            for i in range(len(zip_file_list)):
                with zip_file.open(zip_file_list[i].filename) as xml_file:
                    processed = parse_xml_to_json(xml_file, processed)
        
        elif input_file.endswith(".gz"):
            with gzip.open(input_file) as xml_file:
                processed = parse_xml_to_json(xml_file, processed)

        else:
            processed = parse_xml_to_json(input_file, processed)

        if input_file.endswith((".zip", ".tar.gz")) and output_format == "json":
            file_obj.write(bytes(os.linesep + "]", "utf-8"))

        return processed


def write_parquet(output_file, input_file, xsd_file, lazy, root, xpath, processed, rows_per_batch=10000):

    _logger.info("Generating schema from " + xsd_file)

    xml_schema = xmlschema.XMLSchema(xsd_file, converter=PyArrowConverter)

    _logger.info("Parsing " + input_file)

    _logger.info("Writing to file " + output_file)

    if xpath:
        pyarrow_schema = convert_xml_schema_to_pyarrow_schema(xml_schema, path=xpath)
    else:
        pyarrow_schema = convert_xml_schema_to_pyarrow_schema(xml_schema)

    def parse_xml_to_parquet(xml_file, processed):
        """
        :param xml_file: xml file
        :param pyarrow_schema: PyArrow schema
        :param processed: whether data has been found and processed
        :return: data found and processed
        """

        xml_file_resource = XMLResource(xml_file, lazy=lazy, thin_lazy=True)
        
        if xpath:
            xml_iter_decode = xml_schema.iter_decode(xml_file_resource, path=xpath)
        else:
            xml_iter_decode = xml_schema.iter_decode(xml_file_resource)

        rowcount = 0
        rows = []
        for xml_dict in xml_iter_decode:
            if root:
                xml_dict = xml_dict[root]
            
            rows.append(xml_dict)
            rowcount += 1

            if rowcount == rows_per_batch:

                table = pa.Table.from_pylist(rows, schema=pyarrow_schema)
                writer.write_table(table)
                rows = []
                rowcount = 0
                processed = True

        if rowcount > 0:
            table = pa.Table.from_pylist(rows, schema=pyarrow_schema)
            writer.write_table(table)
            rows = []
            rowcount = 0
            processed = True

        return processed
    

    with pq.ParquetWriter(output_file, pyarrow_schema) as writer:

        if input_file.endswith(".tar.gz"):
            zip_file = tarfile.open(input_file, 'r')
            zip_file_list = zip_file.getmembers()

            for member in zip_file_list:
                with zip_file.extractfile(member) as xml_file:
                    processed = parse_xml_to_parquet(xml_file, processed)

        elif input_file.endswith(".zip"):
            zip_file = ZipFile(input_file, 'r')
            zip_file_list = zip_file.infolist()

            for i in range(len(zip_file_list)):
                with zip_file.open(zip_file_list[i].filename) as xml_file:
                    processed = parse_xml_to_parquet(xml_file, processed)
        
        elif input_file.endswith(".gz"):
            with gzip.open(input_file) as xml_file:
                processed = parse_xml_to_parquet(xml_file, processed)

        else:
            processed = parse_xml_to_parquet(input_file, processed)

        return processed


def parse_file(input_file, output_file, xsd_file, output_format, zip, xpath, target_path=None, delete_xml=False):
    """
    :param input_file: input file
    :param output_file: output file
    :param xsd_file: xsd file
    :param output_format: jsonl or json
    :param zip: zip save file
    :param xpath: whether to parse a specific xml path
    :param target_path: directory to save file
    :param delete_xml: optional delete xml file after converting
    """

    if xpath:
        xpath_items = xpath.split("/")
        lazy = len(xpath_items) - 2
        if lazy < 0:
            lazy = False
        root = xpath_items[-1]
    else:
        lazy = False
        root = None

    processed = False

    if output_format in ["json", "jsonl"]:
        processed = write_json(output_file, zip, input_file, xsd_file, lazy, root, xpath, output_format, processed)
    elif output_format in ["parquet"]:
        processed = write_parquet(output_file, input_file, xsd_file, lazy, root, xpath, processed)

    # Remove output file if no data is generated
    if not processed:
        os.remove(output_file)
        _logger.info("No data found in " + input_file)
        return

    if delete_xml:
        os.remove(input_file)

    _logger.info("Completed " + input_file)


def convert_xml(xsd_file=None, output_format="jsonl", target_path=None, zip=False, xpath=None, multi=1, no_overwrite=False, verbose="DEBUG", log=None, delete_xml=None, xml_files=None):
    """
    :param xsd_file: xsd file name
    :param output_format: jsonl or json
    :param target_path: directory to save file
    :param zip: zip save file
    :param xpath: whether to parse a specific xml path
    :param multi: how many files to convert concurrently
    :param no_overwrite: overwrite target file
    :param verbose: stdout log messaging level
    :param log: optional log file
    :param delete_xml: optional delete xml file after converting
    :param xml_files: list of xml_files

    """

    formatter = logging.Formatter("%(levelname)s - %(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    _logger.handlers.clear()
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    ch.setLevel(logging.getLevelName(verbose))
    _logger.addHandler(ch)

    if log:
        # create log file handler and set level to debug
        fh = logging.FileHandler(log)
        fh.setFormatter(formatter)
        fh.setLevel(logging.DEBUG)
        _logger.addHandler(fh)

    _logger.info("Parsing XML Files..")

    if target_path and not os.path.exists(target_path):
        _logger.error("invalid target_path specified")
        sys.exit(1)

    # open target files
    file_list = list(set([f for _files in [glob.glob(xml_files[x]) for x in range(0, len(xml_files))] for f in _files]))
    file_count = len(file_list)

    if multi > 1:
        parse_queue_pool = Pool(processes=multi)

    _logger.info("Processing " + str(file_count) + " files")

    if 1 < len(file_list) <= 1000:
        file_list.sort(key=os.path.getsize, reverse=True)
        _logger.info("Parsing files in the following order:")
        _logger.info(file_list)

    for filename in file_list:

        path, xml_file = os.path.split(os.path.realpath(filename))
        
        output_file = xml_file

        if output_file.endswith(".gz"):
            output_file = output_file[:-3]

        if output_file.endswith(".tar"):
            output_file = output_file[:-4]

        if output_file.endswith(".zip"):
            output_file = output_file[:-4]

        if output_file.endswith(".xml"):
            output_file = output_file[:-4]

        output_file = output_file + "." + output_format.lower()

        if zip and output_format in ["json", "jsonl", "txt", "csv"]:
            output_file = output_file + ".gz"

        if target_path:
            output_file = os.path.join(target_path, output_file)
            if no_overwrite and os.path.isfile(output_file):
                _logger.info("No overwrite. Skipping " + xml_file)
                continue
        else:
            output_file = os.path.join(path, output_file)
            if no_overwrite and os.path.isfile(output_file):
                _logger.info("No overwrite. Skipping " + xml_file)
                continue

        if multi > 1:
            parse_queue_pool.apply_async(parse_file, args=(filename, output_file, xsd_file, output_format, zip, xpath, target_path, delete_xml), error_callback=_logger.info)
        else:
            parse_file(filename, output_file, xsd_file, output_format, zip, xpath, target_path, delete_xml)

    if multi > 1:
        parse_queue_pool.close()
        parse_queue_pool.join()
