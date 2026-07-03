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


import os
import sys
import subprocess
from multiprocessing import Pool
import logging

from datetime import datetime, date, time, timedelta
import decimal

import json
import glob

import gzip
import tarfile
from zipfile import ZipFile

import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow.lib import ArrowTypeError

import xmlschema
from lxml import etree

from xmlwiz.mappings import ElementType, xpathType, xpathValue

from xmlwiz.pyarrow_xsd_utils import (
    convert_xsd_to_xpath_index,
    convert_xpath_index_to_pyarrow_schema,
    xml_to_python,
)

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


def parse_xml(xml_file, parser_index, xpath_list):

    current_level = 0
    current_xpath = []

    xpath_elem = parser_index[0][()]
    xpath_key = []
    xpath_parent = None

    skip = False

    context = etree.iterparse(
        xml_file,
        events=(
            "start",
            "end",
        ),
    )

    for event, elem in context:
        elem.tag = etree.QName(elem.tag).localname
        current_level += 1
        current_xpath += [elem.tag]

        if event == "start":
            if (
                elem.tag in xpath_elem[xpathType.CHILDREN]
                and xpath_key + [elem.tag] == current_xpath
            ):
                skip = False
                xpath_key += [elem.tag]
                xpath_parent = xpath_elem
                xpath_elem = xpath_elem[xpathType.CHILDREN][elem.tag]
                if xpath_elem[xpathType.ELEMENT_TYPE] in [
                    ElementType.DICT,
                    ElementType.LIST_OF_DICT,
                ]:
                    for k, v in elem.attrib.items():
                        attr_tag = etree.QName(k).localname
                        attr_group = xpath_elem[xpathType.CHILDREN][
                            elem.tag + "@attributes"
                        ]
                        attribute = attr_group[xpathType.CHILDREN][attr_tag]
                        attr_data = xml_to_python(v, attribute[xpathType.ELEMENT_TYPE])
                        if attribute[xpathType.VALUE][xpathValue.VECTOR] is None:
                            attribute[xpathType.VALUE][xpathValue.VECTOR] = []
                        attribute[xpathType.VALUE][xpathValue.VECTOR].append(attr_data)
            else:
                skip = True

        elif event == "end":
            elem.tag = etree.QName(elem.tag).localname
            if skip == False:
                elem.tag = etree.QName(elem.tag).localname
                elem_data = xml_to_python(elem.text, xpath_elem[xpathType.ELEMENT_TYPE])
                if xpath_elem[xpathType.VALUE][xpathValue.VECTOR] is None:
                    xpath_elem[xpathType.VALUE][xpathValue.VECTOR] = []
                xpath_elem[xpathType.VALUE][xpathValue.VECTOR].append(elem_data)
                xpath_elem = xpath_elem[xpathType.PARENT]
                del xpath_key[-1]

            elem.clear()
            current_level -= 1
            del current_xpath[-1]

    if not xpath_list:
        for level in parser_index:
            for k, v in parser_index[level].items():
                print("==============")
                print(level)
                print(k)
                print(v[xpathType.VALUE][xpathValue.VECTOR])
        yield parser_index[0][()][xpathType.VALUE][xpathValue.VECTOR]


def open_gzip_file(gzipfile, filename):
    """
    :param gzipfile: whether to open a new file using gzip
    :param filename: name of new file
    :return: file handlers
    """
    if gzipfile:
        return gzip.open(filename, "wb")
    else:
        return open(filename, "wb")


def xml_batcher(xml_file, parser_index, xpath_list, rows_per_batch):
    row_counter = 0
    results = []
    for xml_dict in parse_xml(xml_file, parser_index, xpath_list):
        results.append(xml_dict)
        row_counter += 1

        if row_counter == rows_per_batch:
            yield results
            results = []
            row_counter = 0

    if row_counter > 0:
        yield results


def write_json(
    output_file,
    gzipfile,
    input_file,
    parser_index,
    xpath_list,
    schema_type,
    processed,
    output_format,
    rows_per_batch,
):

    def write_xml_to_json(xml_file, processed):
        """
        :param xml_file: xml file
        :param processed: whether data has been found and processed
        :return: data found and processed
        """

        for xml_batch in xml_batcher(
            xml_file, parser_index, xpath_list, rows_per_batch
        ):
            arrow_obj = pa.array(xml_batch).cast(schema_type)

            if pa.types.is_struct(arrow_obj.type):
                table = pa.Table.from_struct_array(arrow_obj)
            else:
                arrow_obj = arrow_obj.flatten()
                table = pa.Table.from_struct_array(arrow_obj)

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

    with open_gzip_file(gzipfile, output_file) as file_obj:
        if output_format == "json":
            file_obj.write(bytes("[" + os.linesep, "utf-8"))

        if input_file.endswith(".tar.gz"):
            tar_file = tarfile.open(input_file, "r")
            tar_file_list = tar_file.getmembers()

            for member in tar_file_list:
                with tar_file.extractfile(member) as xml_file:
                    processed = write_xml_to_json(xml_file, processed)

        elif input_file.endswith(".zip"):
            zip_file = ZipFile(input_file, "r")
            zip_file_list = zip_file.infolist()

            for i in range(len(zip_file_list)):
                with zip_file.open(zip_file_list[i].filename) as xml_file:
                    processed = write_xml_to_json(xml_file, processed)

        elif input_file.endswith(".gz"):
            with gzip.open(input_file) as xml_file:
                processed = write_xml_to_json(xml_file, processed)

        else:
            processed = write_xml_to_json(input_file, processed)

        if output_format == "json":
            file_obj.write(bytes(os.linesep + "]", "utf-8"))

        return processed


def write_parquet(
    output_file,
    input_file,
    parser_index,
    xpath_list,
    schema_type,
    processed,
    rows_per_batch,
):

    def write_xml_to_parquet(xml_file, processed):
        """
        :param xml_file: xml file
        :param pyarrow_schema: PyArrow schema
        :param processed: whether data has been found and processed
        :return: data found and processed
        """

        for xml_batch in xml_batcher(
            xml_file, parser_index, xpath_list, rows_per_batch
        ):
            arrow_obj = pa.array(xml_batch).cast(schema_type)

            if pa.types.is_struct(arrow_obj.type):
                table = pa.Table.from_struct_array(arrow_obj)
            else:
                arrow_obj = arrow_obj.flatten()
                table = pa.Table.from_struct_array(arrow_obj)

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
            gzip_file = tarfile.open(input_file, "r")
            gzip_file_list = gzip_file.getmembers()

            for member in gzip_file_list:
                with gzip_file.extractfile(member) as xml_file:
                    processed = write_xml_to_parquet(xml_file, processed)

        elif input_file.endswith(".zip"):
            zip_file = ZipFile(input_file, "r")
            zip_file_list = zip_file.infolist()

            for i in range(len(zip_file_list)):
                with zip_file.open(zip_file_list[i].filename) as xml_file:
                    processed = write_xml_to_parquet(xml_file, processed)

        elif input_file.endswith(".gz"):
            with gzip.open(input_file) as xml_file:
                processed = write_xml_to_parquet(xml_file, processed)

        else:
            processed = write_xml_to_parquet(input_file, processed)

        return processed


def parse_xml_file(
    xsd_file,
    xml_file,
    xml_path,
    output_file,
    output_path,
    max_recursion=2,
    rows_per_batch=None,
    output_format="jsonl",
    gzipfile=False,
    delete_xml=False,
    flat_attributes=False,
    flat_elements=False,
):
    """
    :param xsd_file: xsd file
    :param xml_file: xml input file
    :param xml_path: whether to parse a specific xml path
    :param output_format: jsonl or json
    :param output_file: output file
    :param output_path: directory to save file
    :param gzipfile: gzip saved file
    :param delete_xml: optional delete xml file after converting
    """

    processed = False
    xml_schema = xmlschema.XMLSchema11(xsd_file)

    xpath_index = convert_xsd_to_xpath_index(xml_schema, max_recursion)

    xpath_list = None
    if xml_path:
        xpath_list = xml_path.split("/")
        xpath_list = xpath_list[1:]

    pyarrow_schema = convert_xpath_index_to_pyarrow_schema(
        xpath_index, xpath_list, flat_attributes, flat_elements
    )

    print("zzz")
    print(xpath_index[2][tuple(["purchaseOrder", "shipTo"])][xpathType.NAME])
    print(xpath_index[2][tuple(["purchaseOrder", "shipTo"])][xpathType.CHILDREN].keys())
    print(
        xpath_index[3][tuple(["purchaseOrder", "shipTo", "name"])][xpathType.PARENT][
            xpathType.NAME
        ]
    )
    print(
        xpath_index[3][tuple(["purchaseOrder", "shipTo", "name"])][xpathType.PARENT]
        == xpath_index[2][tuple(["purchaseOrder", "shipTo"])]
    )
    print(
        xpath_index[3][tuple(["purchaseOrder", "shipTo", "name"])][xpathType.PARENT][
            xpathType.CHILDREN
        ].keys()
    )
    print("zzz")

    parser_index = xpath_index.copy()

    parser_index = {}
    for level in xpath_index:
        parser_index[level] = {}
        for k, v in xpath_index[level].items():
            parser_index[level][k] = v

    print("zzz")
    print(
        parser_index[2][tuple(["purchaseOrder", "shipTo"])][xpathType.CHILDREN].keys()
    )
    print("zzz")

    if xpath_list:
        parser_levels = len(parser_index)
        level = 0
        tracker = []
        for xpath in xpath_list:
            tracker.append(xpath)
            tracker_key = tuple(tracker)
            level += 1
            del_list = []
            for key in parser_index[level]:
                if key != tracker_key:
                    del_list.append(key)
            if del_list:
                for key in del_list:
                    del parser_index[level][key]

        for i in range(level + 1, parser_levels):
            del_list = []
            for key in parser_index[i]:
                if key[: len(xpath_list)] != tuple(xpath_list):
                    del_list.append(key)
            if del_list:
                for key in del_list:
                    del parser_index[i][key]

    _logger.info("Parsing " + xml_file)
    _logger.info("Writing to file " + output_file)

    if output_format in ["json", "jsonl"]:
        processed = write_json(
            output_file,
            gzipfile,
            xml_file,
            parser_index,
            xpath_list,
            pyarrow_schema,
            processed,
            output_format,
            rows_per_batch,
        )

    elif output_format in ["parquet"]:
        processed = write_parquet(
            output_file,
            xml_file,
            parser_index,
            xpath_list,
            pyarrow_schema,
            processed,
            rows_per_batch,
        )

    # Remove output file if no data is generated
    if not processed:
        os.remove(output_file)
        _logger.info("No data found in " + xml_file)
        return

    if delete_xml:
        os.remove(input_file)

    _logger.info("Completed " + xml_file)


def convert_xml(
    xsd_file=None,
    max_recursion=2,
    xml_path=None,
    rows_per_batch=None,
    multi=1,
    output_format="jsonl",
    output_path=None,
    gzipfile=False,
    no_overwrite=False,
    delete_xml=False,
    flatten=False,
    log_level="INFO",
    log_file=None,
    xml_files=None,
):
    """
    :param xsd_file: xsd file name
    :param max_recursions:
    :param xml_path: whether to parse a specific xml path
    :param rows_per_batch:
    :param multi: how many files to convert concurrently
    :param output_format: jsonl or json
    :param output_path: directory to save file
    :param gzipfile: gzip saved file
    :param no_overwrite: overwrite target file
    :param delete_xml: optional delete xml file after converting
    :param flatten:
    :param log_level: stdout log messaging level
    :param log_file: optional log file
    :param xml_files: list of xml_files

    """

    formatter = logging.Formatter(
        "%(levelname)s - %(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    _logger.handlers.clear()
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    ch.setLevel(logging.getLevelName(log_level))
    _logger.addHandler(ch)

    if log_file:
        # create log file handler and set level to debug
        fh = logging.FileHandler(log_file)
        fh.setFormatter(formatter)
        fh.setLevel(logging.DEBUG)
        _logger.addHandler(fh)

    _logger.info("Parsing XML Files..")

    if output_path and not os.path.exists(output_path):
        _logger.error("invalid output_path specified")
        sys.exit(1)

    # open xml files
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

    for xml_file in file_list:
        path, output_file = os.path.split(os.path.realpath(xml_file))

        if output_file.endswith(".gz"):
            output_file = output_file[:-3]

        if output_file.endswith(".tar"):
            output_file = output_file[:-4]

        if output_file.endswith(".zip"):
            output_file = output_file[:-4]

        if output_file.endswith(".xml"):
            output_file = output_file[:-4]

        output_file = output_file + "." + output_format.lower()

        if gzipfile and output_format in ["json", "jsonl", "txt", "csv"]:
            output_file = output_file + ".gz"

        if output_path:
            output_file = os.path.join(output_path, output_file)
            if no_overwrite and os.path.isfile(output_file):
                _logger.info("No overwrite. Skipping " + xml_file)
                continue
        else:
            output_file = os.path.join(path, output_file)
            if no_overwrite and os.path.isfile(output_file):
                _logger.info("No overwrite. Skipping " + xml_file)
                continue

        if flatten is True:
            flat_attributes = True
            flat_elements = True
        elif flatten == "attributes":
            flat_attributes = True
            flat_elements = False
        elif flatten == "elements":
            flat_attributes = False
            flat_elements = True
        else:
            flat_attributes = False
            flat_elements = False

        if multi > 1:
            parse_queue_pool.apply_async(
                parse_xml_file,
                args=(
                    xsd_file,
                    xml_file,
                    xml_path,
                    output_file,
                    output_path,
                    max_recursion,
                    rows_per_batch,
                    output_format,
                    gzipfile,
                    delete_xml,
                    flat_attributes,
                    flat_elements,
                ),
                error_callback=_logger.info,
            )
        else:
            parse_xml_file(
                xsd_file,
                xml_file,
                xml_path,
                output_file,
                output_path,
                max_recursion,
                rows_per_batch,
                output_format,
                gzipfile,
                delete_xml,
                flat_attributes,
                flat_elements,
            )

    if multi > 1:
        parse_queue_pool.close()
        parse_queue_pool.join()
