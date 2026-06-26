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


import os
import sys
import subprocess
from multiprocessing import Pool
import logging

from datetime import datetime, date, time, timedelta
import decimal
import isodate

import json
import glob
from functools import reduce

import gzip
import tarfile
from zipfile import ZipFile

import pyarrow as pa
import pyarrow.parquet as pq

import xmlschema
from lxml import etree

from xmlwiz.mappings import ElementTypeEnum, element_decode
from xmlwiz.pyarrow_xsd_utils import convert_xsd_to_pyarrow, build_action_items

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
    elif isinstance(obj, datetime):
        return obj.strftime("%Y-%m-%d %H:%M:%S.%f")
    elif isinstance(obj, date):
        return obj.strftime("%Y-%m-%d")
    elif isinstance(obj, time):
        return obj.isoformat()
    elif isinstance(obj, timedelta):
        return int(obj.total_seconds() * 1_000_000)
    elif isinstance(obj, bytes):
        return obj.decode()
    elif isinstance(obj, set):
        return list(obj)
    raise TypeError(repr(obj) + ":" + str(type(obj)) + " is not JSON serializable")


def parse_xml(input_file, action_index, xpath_items, rows_per_batch):

    row_counter = 0

    result = {}
    parent = result

    current_xpath = []
    current_level = 0

    context = etree.iterparse(
        input_file,
        events=(
            "start",
            "end",
        ),
    )

    for event, elem in context:
        elem.tag = etree.QName(elem.tag).localname
        if event == "start":
            current_xpath.append(elem.tag)
            current_level += 1
            try:
                element_type = action_index[current_level][tuple(current_xpath)]
            except KeyError:
                continue

            if element_type == ElementTypeEnum.DICT:
                parent[elem.tag] = {}
                old_parent = parent
                parent = parent[elem.tag]
                for k, v in elem.attrib.items():
                    k = etree.QName(k).localname
                    try:
                        attr_type = action_index[current_level+1][tuple(current_xpath + [elem.tag + k])]
                        elem_data = element_decode(v, attr_type)
                        parent[elem.tag + k] = elem_data
                    except KeyError:
                        pass
            elif element_type == ElementTypeEnum.LIST:
                if elem.tag not in parent:
                    parent[elem.tag] = []
                    old_parent = parent
                    parent = parent[elem.tag]
            elif element_type == ElementTypeEnum.LIST_OF_DICT:
                if elem.tag not in parent:
                    parent[elem.tag] = [{}]
                    old_parent = parent
                    parent = parent[elem.tag][0]
                    for k, v in elem.attrib.items():
                        k = etree.QName(k).localname
                        try:
                            attr_type = action_index[current_level+1][tuple(current_xpath + [elem.tag + k])]
                            elem_data = element_decode(v, attr_type)
                            parent[elem.tag + k] = elem_data
                        except KeyError:
                            pass
                else:
                    parent[elem.tag].append({})
                    parent = parent[elem.tag][-1]

        elif event == "end":
            try:
                element_type = action_index[current_level][tuple(current_xpath)]

                if element_type in [
                    ElementTypeEnum.DICT,
                    ElementTypeEnum.LIST,
                    ElementTypeEnum.LIST_OF_DICT,
                ]:
                    parent = old_parent
                else:
                    parent_type = action_index[current_level - 1][
                        tuple(current_xpath[:-1])
                    ]

                    elem_data = element_decode(elem.text, element_type)
                    if parent_type in [
                        ElementTypeEnum.DICT,
                        ElementTypeEnum.LIST_OF_DICT,
                    ]:
                        parent[elem.tag] = elem_data
                    elif parent_type == ElementTypeEnum.LIST:
                        parent.append(elem_data)
            except KeyError:
                pass

            elem.clear()
            if current_xpath == xpath_items:
                row_counter += 1
                if row_counter == rows_per_batch:
                    yield result
                    parent[elem.tag] = {}
                    row_counter = 0
            
            current_level -= 1
            child = current_xpath.pop()

    if xpath_items:
        if row_counter:
            yield result
    else:
        yield result


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


def write_json(
    output_file,
    zip,
    input_file,
    action_index,
    xpath_items,
    schema_type,
    processed,
    output_format,
    rows_per_batch,
):

    def parse_xml_to_json(xml_file, processed):
        """
        :param xml_file: xml file
        :param processed: whether data has been found and processed
        :return: data found and processed
        """

        for xml_dict in parse_xml(xml_file, action_index, xpath_items, rows_per_batch):
            if xpath_items:
                xml_dict = reduce(dict.get, xpath_items, xml_dict)

            if not xml_dict:
                return processed

            arrow_obj = pa.array([xml_dict]).cast(schema_type)

            if pa.types.is_struct(arrow_obj.type):
                names = arrow_obj.type.names
            else:
                names = arrow_obj.type.value_type.names
                arrow_obj = arrow_obj.flatten()

            table = pa.Table.from_arrays(
                arrow_obj.flatten(), names=names
            )

            pylist = table.to_pylist()

            for row in pylist:
                xml_json = json.dumps(row, default=json_decoder)
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
            zip_file = tarfile.open(input_file, "r")
            zip_file_list = zip_file.getmembers()

            for member in zip_file_list:
                with zip_file.extractfile(member) as xml_file:
                    processed = parse_xml_to_json(xml_file, processed)

        elif input_file.endswith(".zip"):
            zip_file = ZipFile(input_file, "r")
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


def write_parquet(
    output_file,
    input_file,
    action_index,
    xpath_items,
    schema_type,
    processed,
    rows_per_batch,
):

    def parse_xml_to_parquet(xml_file, processed):
        """
        :param xml_file: xml file
        :param pyarrow_schema: PyArrow schema
        :param processed: whether data has been found and processed
        :return: data found and processed
        """

        for xml_dict in parse_xml(xml_file, action_index, xpath_items, rows_per_batch):

            if xpath_items:
                xml_dict = reduce(dict.get, xpath_items, xml_dict)

            if not xml_dict:
                return processed

            arrow_obj = pa.array([xml_dict]).cast(schema_type)

            if pa.types.is_struct(arrow_obj.type):
                names = arrow_obj.type.names
            else:
                names = arrow_obj.type.value_type.names
                arrow_obj = arrow_obj.flatten()

            table = pa.Table.from_arrays(
                arrow_obj.flatten(), names=names
            )

            if table.num_rows > 0:
                writer.write_table(table)
                processed = True

        return processed

    if pa.types.is_struct(schema_type):
        pyarrow_schema = pa.schema(schema_type)
    else:
        pyarrow_schema = pa.schema(schema_type.value_type)

    with pq.ParquetWriter(output_file, pyarrow_schema) as writer:
        if input_file.endswith(".tar.gz"):
            zip_file = tarfile.open(input_file, "r")
            zip_file_list = zip_file.getmembers()

            for member in zip_file_list:
                with zip_file.extractfile(member) as xml_file:
                    processed = parse_xml_to_parquet(xml_file, processed)

        elif input_file.endswith(".zip"):
            zip_file = ZipFile(input_file, "r")
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


def set_unqualified_form(xsd_file_contents):
    # Parse the XSD file
    root = etree.fromstring(xsd_file_contents.encode("utf-8"))
    
    # Define the W3C XML Schema namespace (required for tag matching)
    xs_ns = "{http://www.w3.org/2001/XMLSchema}"
    
    # Check if the root tag is 'schema' and add the attribute
    if root.tag == f"{xs_ns}schema":
        root.set("elementFormDefault", "unqualified")
        
        # Save the updated XSD back to the file
        return etree.tostring(root, encoding="utf-8", xml_declaration=True)
    else:
        raise TypeError("Root element is not an xs:schema")


def parse_file(
    input_file,
    output_file,
    xsd_file,
    output_format,
    zip,
    xpath,
    target_path=None,
    delete_xml=False,
):
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

    processed = False

    with open(xsd_file) as f:
        xsd_file_contents = f.read()
    
    xsd_file_contents = set_unqualified_form(xsd_file_contents)

    xml_schema = xmlschema.XMLSchema11(xsd_file_contents)
    pyarrow_schema = convert_xsd_to_pyarrow(xml_schema)

    # Each xml file should only have one root element, but have to merge different root elements across files.
    if len(pyarrow_schema.names) > 1:
        pyarrow_schema = pa.schema(
            [column.with_nullable(True) for column in pyarrow_schema]
        )

    schema_type = pa.struct(pyarrow_schema)

    action_items = build_action_items(pyarrow_schema, [], xml_schema)

    action_index = {}
    for k, v in action_items.items():
        level = len(k)
        if level not in action_index:
            action_index[level] = {}
        action_index[level][k] = v

    xpath_items = None
    new_action_index = action_index

    if xpath:
        xpath_items = xpath.split("/")
        xpath_items = xpath_items[1:]

        current_type = pyarrow_schema
        for column in xpath_items:
            current_field = current_type.field(column)
            current_type = current_field.type
        schema_type = current_type

        xpath_count = len(xpath_items)
        xpath_key = tuple(xpath_items)
        new_action_index = {}

        for i, v in action_index.items():
            new_items = {}
            if i > xpath_count:
                new_items = {
                    k2: v2 for k2, v2 in v.items() if k2[:xpath_count] == xpath_key
                }
            else:
                new_items = {k2: v2 for k2, v2 in v.items() if k2 == xpath_key[:i]}

            if new_items:
                new_action_index[i] = new_items

    _logger.info("Parsing " +  input_file)
    _logger.info("Writing to file " + output_file)

    if output_format in ["json", "jsonl"]:
        processed = write_json(
            output_file,
            zip,
            input_file,
            new_action_index,
            xpath_items,
            schema_type,
            processed,
            output_format,
            rows_per_batch=10000,
        )

    elif output_format in ["parquet"]:
        processed = write_parquet(
            output_file,
            input_file,
            new_action_index,
            xpath_items,
            schema_type,
            processed,
            rows_per_batch=10000,
        )

    # Remove output file if no data is generated
    if not processed:
        os.remove(output_file)
        _logger.info("No data found in " + input_file)
        return

    if delete_xml:
        os.remove(input_file)

    _logger.info("Completed " + input_file)


def convert_xml(
    xsd_file=None,
    output_format="jsonl",
    target_path=None,
    zip=False,
    xpath=None,
    multi=1,
    no_overwrite=False,
    verbose="DEBUG",
    log=None,
    delete_xml=None,
    xml_files=None,
):
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

    formatter = logging.Formatter(
        "%(levelname)s - %(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

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
    file_list = list(
        set(
            [
                f
                for _files in [
                    glob.glob(xml_files[x]) for x in range(0, len(xml_files))
                ]
                for f in _files
            ]
        )
    )
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
            parse_queue_pool.apply_async(
                parse_file,
                args=(
                    filename,
                    output_file,
                    xsd_file,
                    output_format,
                    zip,
                    xpath,
                    target_path,
                    delete_xml,
                ),
                error_callback=_logger.info,
            )
        else:
            parse_file(
                filename,
                output_file,
                xsd_file,
                output_format,
                zip,
                xpath,
                target_path,
                delete_xml,
            )

    if multi > 1:
        parse_queue_pool.close()
        parse_queue_pool.join()
