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
from enum import IntEnum

from datetime import datetime, date, time, timedelta
import decimal
import isodate

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

from xmlwiz.mappings import ElementTypeEnum, XpathTypeEnum

from xmlwiz.pyarrow_xsd_utils import convert_xsd_to_xpath_index, element_decode_type

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


def parse_xml_file(xml_file, tracker_index, xpath_list):
    current_xpath = []
    current_level = 0

    context = etree.iterparse(
        xml_file,
        events=(
            "start",
            "end",
        ),
    )

    for event, elem in context:
        elem.tag = etree.QName(elem.tag).localname
        print(elem.tag)

        if event == "start":
            current_xpath.append(elem.tag)
            current_level += 1

            current_xpath_key = tuple(current_xpath)
            try:
                [element_type, parent, obj] = tracker_index[current_level][
                    tuple(current_xpath_key)
                ]
            except KeyError:
                continue

            if element_type[0] in [ElementTypeEnum.DICT, ElementTypeEnum.LIST]:
                attributes = {}
                for k, v in elem.attrib.items():
                    k = etree.QName(k).localname
                    try:
                        attr_type = tracker_index[current_level][
                            tuple(current_xpath + [k])
                        ][0][0]
                        attr_data = element_decode(v, attr_type)
                        attributes[k] = attr_data
                    except KeyError:
                        pass
                if attributes:
                    print(":==================")
                    try:
                        tracker_index[current_level][
                            tuple(current_xpath + ["@attributes"])
                        ][XpathTypeEnum.OBJ] = attributes
                        print(":==================")
                        if parent[XpathTypeEnum.OBJ] is None:
                            parent[XpathTypeEnum.OBJ] = {}
                        parent[XpathTypeEnum.OBJ]["@attributes"] = attributes
                        print(":==================")
                    except KeyError:
                        if obj is None:
                            tracker_index[current_level][current_xpath_key][
                                XpathTypeEnum.OBJ
                            ] = {}
                        attributes = {
                            elem.tag + "@" + k: v for k, v in attributes.items()
                        }
                        tracker_index[current_level][current_xpath_key][
                            XpathTypeEnum.OBJ
                        ].update(attributes)

        elif event == "end":
            current_xpath_key = tuple(current_xpath)
            try:
                [element_type, parent, obj] = tracker_index[current_level][
                    current_xpath_key
                ]
            except KeyError:
                elem.clear()
                current_level -= 1
                del current_xpath[-1]
                continue

            if element_type[0] in [ElementTypeEnum.DICT, ElementTypeEnum.LIST]:
                data = obj
            else:
                data = element_decode(elem.text, element_type[0])

            # flush data to parent
            if data:
                if parent[XpathTypeEnum.OBJ] is None:
                    parent[XpathTypeEnum.OBJ] = {}

                if element_type[0] == ElementTypeEnum.DICT:
                    parent[XpathTypeEnum.OBJ][elem.tag] = data.copy()
                    tracker_index[current_level][current_xpath_key][
                        XpathTypeEnum.OBJ
                    ] = None
                elif element_type[0] == ElementTypeEnum.LIST:
                    if elem.tag not in parent[XpathTypeEnum.OBJ]:
                        parent[XpathTypeEnum.OBJ][elem.tag] = []
                    parent[XpathTypeEnum.OBJ][elem.tag].append(data.copy())
                    tracker_index[current_level][current_xpath_key][
                        XpathTypeEnum.OBJ
                    ] = None
                else:
                    parent[XpathTypeEnum.OBJ].update({elem.tag: data})

            if current_xpath == xpath_list:
                if element_type[0] == ElementTypeEnum.DICT:
                    yield data
                else:
                    yield [data]

            elem.clear()
            current_level -= 1
            del current_xpath[-1]

    if not xpath_list:
        yield tracker_index[0][()][XpathTypeEnum.OBJ]


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


def xml_batcher(xml_file, tracker_index, xpath_list, rows_per_batch):
    row_counter = 0
    results = []
    for xml_dict in parse_xml_file(xml_file, tracker_index, xpath_list):
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
    zip,
    input_file,
    tracker_index,
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
            xml_file, tracker_index, xpath_list, rows_per_batch
        ):
            print(xml_batch)

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

    with open_zip_file(zip, output_file) as file_obj:
        if output_format == "json":
            file_obj.write(bytes("[" + os.linesep, "utf-8"))

        if input_file.endswith(".tar.gz"):
            zip_file = tarfile.open(input_file, "r")
            zip_file_list = zip_file.getmembers()

            for member in zip_file_list:
                with zip_file.extractfile(member) as xml_file:
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
    tracker_index,
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
            xml_file, tracker_index, xpath_list, rows_per_batch
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
            zip_file = tarfile.open(input_file, "r")
            zip_file_list = zip_file.getmembers()

            for member in zip_file_list:
                with zip_file.extractfile(member) as xml_file:
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


def parse_file(
    input_file,
    output_file,
    xsd_file,
    flat_attributes,
    flat_lists,
    max_recursion,
    output_format,
    zip,
    xpath,
    rows_per_batch,
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

    xml_schema = xmlschema.XMLSchema11(xsd_file)
    pyarrow_schema, action_index = convert_xsd_to_pyarrow(
        xml_schema,
        xpath=[],
        flat_attributes=flat_attributes,
        flat_lists=flat_lists,
        max_recursion=max_recursion,
    )

    schema_type = pa.struct(pyarrow_schema)

    xpath_list = None

    new_action_index = action_index

    if xpath:
        xpath_list = xpath.split("/")
        xpath_list = xpath_list[1:]

        current_type = pyarrow_schema
        for column in xpath_list:
            try:
                current_field = current_type.field(column)
            except:
                current_field = current_type.value_type.field(column)
            current_type = current_field.type
        schema_type = current_type

        new_action_index = {}

        for i, v in action_index.items():
            new_items = {}
            for k, v2 in v.items():
                if i <= len(xpath_list):
                    if k == tuple(xpath_list[: len(k)]):
                        new_items[k] = v2
                    elif k[:i] == tuple(xpath_list[:i]) and len(k) > i:
                        new_items[k] = v2
                elif k[: len(xpath_list)] == tuple(xpath_list):
                    new_items[k] = v2

            if new_items:
                new_action_index[i] = new_items

    tracker_index = {0: {(): [(ElementTypeEnum.DICT, schema_type, False), None, None]}}
    for i, v in new_action_index.items():
        tracker_subtree = {}
        for k, v2 in v.items():
            if len(k) == i:
                tracker_subtree[k] = [v2, tracker_index[i - 1][k[:-1]], None]
            else:
                tracker_subtree[k] = [v2, tracker_index[i - 1][k[:-2]], None]
        if tracker_subtree:
            tracker_index[i] = tracker_subtree

    _logger.info("Parsing " + input_file)
    _logger.info("Writing to file " + output_file)

    if output_format in ["json", "jsonl"]:
        processed = write_json(
            output_file,
            zip,
            input_file,
            tracker_index,
            xpath_list,
            schema_type,
            processed,
            output_format,
            rows_per_batch,
        )

    elif output_format in ["parquet"]:
        processed = write_parquet(
            output_file,
            input_file,
            tracker_index,
            xpath_list,
            schema_type,
            processed,
            rows_per_batch,
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
    flat_attributes=True,
    flat_lists=True,
    max_recursion=2,
    rows_per_batch=None,
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
                    flat_attributes,
                    flat_lists,
                    max_recursion,
                    output_format,
                    zip,
                    xpath,
                    rows_per_batch,
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
                flat_attributes,
                flat_lists,
                max_recursion,
                output_format,
                zip,
                xpath,
                rows_per_batch,
                target_path,
                delete_xml,
            )

    if multi > 1:
        parse_queue_pool.close()
        parse_queue_pool.join()
