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
from enum import IntEnum

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

from xmlwiz.mappings import ElementTypeEnum
from xmlwiz.pyarrow_xsd_utils import convert_xsd_to_pyarrow, element_decode

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


class TrackerTypeEnum(IntEnum):
    ELEMENT_TYPE = 0
    PARENT = 1
    OBJ = 2


def parse_xml_file(xml_file, action_index, xpath_list, rows_per_batch):

    row_counter = 0

    tracker_index = {0:{():[ElementTypeEnum.DICT, None, None]}}
    for i, v in action_index.items():
        tracker_subtree = {}
        for k, v2 in v.items():
            if len(k) == i:
                tracker_subtree[k] = [v2, tracker_index[i - 1][k[:-1]], None]
        if tracker_subtree:
            tracker_index[i] = tracker_subtree

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

        if event == "start":
            current_xpath.append(elem.tag)
            current_level += 1

            current_xpath_key = tuple(current_xpath)
            try:
                [element_type, parent, obj] = tracker_index[current_level][tuple(current_xpath_key)]
            except KeyError:
                continue

            if element_type in [ElementTypeEnum.DICT, ElementTypeEnum.LIST]:
                attributes = {}
                for k, v in elem.attrib.items():
                    k = etree.QName(k).localname
                    try:
                        attr_type = action_index[current_level][tuple(current_xpath + [k])]
                        attr_data = element_decode(v, attr_type)
                        attributes[elem.tag + "@" + k] = attr_data
                    except KeyError:
                        pass
                if attributes:
                    if obj is None:
                        tracker_index[current_level][tuple(current_xpath_key)][TrackerTypeEnum.OBJ] = {}
                    tracker_index[current_level][tuple(current_xpath_key)][TrackerTypeEnum.OBJ].update(attributes)

        elif event == "end":
            current_xpath_key = tuple(current_xpath)
            try:
                [element_type, parent, obj] = tracker_index[current_level][current_xpath_key]
            except KeyError:
                elem.clear()            
                current_level -= 1
                del current_xpath[-1]
                continue

            if element_type in [ElementTypeEnum.DICT, ElementTypeEnum.LIST]:
                data = obj
            else:
                data = element_decode(elem.text, element_type)

            # flush data to parent
            if data:
                if parent[TrackerTypeEnum.OBJ] is None:
                    parent[TrackerTypeEnum.OBJ] = {}

                if element_type == ElementTypeEnum.DICT:
                    parent[TrackerTypeEnum.OBJ][elem.tag] = data.copy()
                    tracker_index[current_level][current_xpath_key][TrackerTypeEnum.OBJ] = None
                elif element_type == ElementTypeEnum.LIST:
                    if elem.tag not in parent[TrackerTypeEnum.OBJ]:
                        parent[TrackerTypeEnum.OBJ][elem.tag] = []
                    parent[TrackerTypeEnum.OBJ][elem.tag].append(data.copy())
                    tracker_index[current_level][current_xpath_key][TrackerTypeEnum.OBJ] = None
                else:
                    parent[TrackerTypeEnum.OBJ].update({elem.tag: data})

            if current_xpath == xpath_list:
                row_counter += 1
                if row_counter == rows_per_batch:
                    # flush all child data up to root
                    for xpath in reversed(xpath_list[:-1]):
                        if parent[TrackerTypeEnum.PARENT][TrackerTypeEnum.OBJ] is None:
                            parent[TrackerTypeEnum.PARENT][TrackerTypeEnum.OBJ] = {}
                        if xpath not in parent[TrackerTypeEnum.PARENT][TrackerTypeEnum.OBJ]:
                            if parent[TrackerTypeEnum.ELEMENT_TYPE] == ElementTypeEnum.DICT:
                                parent[TrackerTypeEnum.PARENT][TrackerTypeEnum.OBJ][xpath] = {}
                            elif parent[TrackerTypeEnum.ELEMENT_TYPE] == ElementTypeEnum.LIST:
                                parent[TrackerTypeEnum.PARENT][TrackerTypeEnum.OBJ][xpath] = []


                        if parent[TrackerTypeEnum.ELEMENT_TYPE] == ElementTypeEnum.DICT:
                            parent[TrackerTypeEnum.PARENT][TrackerTypeEnum.OBJ][xpath] = parent[TrackerTypeEnum.OBJ].copy()
                        elif parent[TrackerTypeEnum.ELEMENT_TYPE] == ElementTypeEnum.LIST:                            
                            parent[TrackerTypeEnum.PARENT][TrackerTypeEnum.OBJ][xpath].append(parent[TrackerTypeEnum.OBJ].copy())
                        parent[TrackerTypeEnum.OBJ] = None                            

                        parent = parent[TrackerTypeEnum.PARENT]

                    results = [tracker_index[0][()][TrackerTypeEnum.OBJ]]
                    for xpath in xpath_list:
                        try:
                            results = [row[xpath] for row in results]
                        except:
                            results =[childrow2[xpath] for childrow2 in [childrow for row in results for childrow in row]]
                    yield results

                    #t_index = len(xpath_list) - 1
                    #t_key = tuple(xpath_list[:-1])
                    #tracker_index[t_index][t_key][TrackerTypeEnum.OBJ] = None

                    row_counter = 0

            elem.clear()            
            current_level -= 1
            del current_xpath[-1]

    if rows_per_batch and row_counter == 0:
        return

    results = [tracker_index[0][()][TrackerTypeEnum.OBJ]]
    if xpath_list:
        for xpath in xpath_list:
            try:
                results = [row[xpath] for row in results]
            except:
                results =[childrow2[xpath] for childrow2 in [childrow for row in results for childrow in row]]
        yield results
    else:
        yield results

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

        for xml_dict in parse_xml_file(xml_file, action_index, xpath_list, rows_per_batch):

            if not xml_dict:
                return processed

            #print(xml_dict)
            #if pa.types.is_struct(schema_type):
            #    pyarrow_schema = pa.schema(schema_type)
            #else:
            #    pyarrow_schema = pa.schema(schema_type.value_type)

            # print(pyarrow_schema.to_string)
            #print("====================")

            arrow_obj = pa.array(xml_dict).cast(schema_type)

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
    action_index,
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

        for xml_dict in parse_xml_file(xml_file, action_index, xpath_list, rows_per_batch):

            if not xml_dict:
                return processed

            arrow_obj = pa.array([xml_dict]).cast(schema_type)

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
    pyarrow_schema, action_index = convert_xsd_to_pyarrow(xml_schema, [])

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
                    if k == tuple(xpath_list[:len(k)]):
                        new_items[k] = v2
                    elif k[:i] == tuple(xpath_list[:i]) and len(k) > i:
                        new_items[k] = v2
                elif k[:len(xpath_list)] == tuple(xpath_list):
                    new_items[k] = v2

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
            new_action_index,
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
