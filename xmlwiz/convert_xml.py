"""
(c) 2019 David Lee.

Author: David Lee
"""

import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime
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
from xmlschema.converters import ColumnarConverter
from xmlschema import XMLResource

from xmlwiz.pyarrow_converter import PyArrowConverter

_logger = logging.getLogger(__name__)
_logger.setLevel(logging.INFO)


def json_decoder(obj):
    """
    :param obj: python data
    :return: converted type
    :raises:
    """
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M:%S.%f')
    if isinstance(obj, set):
        return list(obj)
    raise TypeError(repr(obj) + " is not JSON serializable")


def map_xsd_type_to_arrow(xsd_type):
    
    local_name = xsd_type.primitive_type.local_name
    
    mapping = {
        'string': pa.string(),
        'normalizedString': pa.string(),
        'token': pa.string(),
        'int': pa.int32(),
        'integer': pa.int64(),
        'long': pa.int64(),
        'short': pa.int16(),
        'byte': pa.int8(),
        'unsignedInt': pa.uint32(),
        'unsignedLong': pa.uint64(),
        'unsignedShort': pa.uint16(),
        'unsignedByte': pa.uint8(),
        'float': pa.float32(),
        'double': pa.float64(),
        'decimal': pa.decimal128(38, 10), # Precision and scale can be adjusted
        'boolean': pa.bool_(),
        'date': pa.date32(),
        'dateTime': pa.timestamp('ms'),
        'time': pa.time32('ms'),
        'base64Binary': pa.binary(),
        'hexBinary': pa.binary(),
    }
    
    return mapping.get(local_name, pa.string())  # Fallback to string for unknown primitives


def xsd_element_to_arrow_field(element, root=False):
    """Recursively processes an XSD element node into a PyArrow Field."""
    name = element.local_name
    
    # Check if the element contains nested children (complex type)
    if element.type.has_complex_content() or element.type.is_complex():
        fields = []
        
        # Process attributes of this element as fields
        for attr_name, attr in element.type.attributes.items():
            if attr_name is not None:
                attr_type = map_xsd_type_to_arrow(attr.type)
                # Prefix attributes with '@' to follow common XML-to-JSON/Dict conventions
                fields.append(pa.field(attr_name, attr_type, nullable=True))
        
        # Process nested elements
        for child in element.type.content.iter_elements():
            child_field = xsd_element_to_arrow_field(child)
            
            # If the element can occur multiple times, wrap it in an Arrow List
            if child.max_occurs is None or child.max_occurs > 1:
                list_type = pa.list_(child_field.type)
                fields.append(pa.field(child.local_name, list_type, nullable=True))
            else:
                fields.append(child_field)
                
        # Struct type holds the compiled complex structure
        arrow_type = pa.struct(fields)
    else:
        # Simple types map directly to primitive types
        arrow_type = map_xsd_type_to_arrow(element.type)
        
    if root:
        nullable = True
    else:
        nullable = element.min_occurs == 0

    return pa.field(name, arrow_type, nullable=nullable)


def convert_xml_schema_to_pyarrow_schema(schema, path=None):
    """Converts the XML Schema into a PyArrow Schema."""
    arrow_fields = []
    
    # Process each top-level root element defined in the XML schema
    if path:
        xsd_elem = schema.find(path)
        parent_field = xsd_element_to_arrow_field(xsd_elem)
        child_fields = parent_field.flatten()
        arrow_fields = [pa.field(field.name.removeprefix(parent_field.name + "."), field.type) for field in child_fields]
    else:
        for name, element in schema.elements.items():
            field = xsd_element_to_arrow_field(element, True)
            arrow_fields.append(field)

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

    xml_schema = xmlschema.XMLSchema(xsd_file, converter=ColumnarConverter)

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


def write_parquet(output_file, input_file, xsd_file, lazy, root, xpath, processed, rows_per_batch=1000):

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

            if rowcount == 100:

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
