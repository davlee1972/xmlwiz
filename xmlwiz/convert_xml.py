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
from glob import glob

import gzip
import tarfile
from zipfile import ZipFile
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow.lib import ArrowTypeError

import xmlschema
from lxml import etree
import datafusion as df

from xmlwiz.xsd_to_pyarrow import (
    convert_xsd_to_xpath_tree,
    convert_xpath_tree_to_schema_type,
)

from xmlwiz.xml_to_pyarrow import cast_vector_data, set_pyarrow_data

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


def parse_xml_file(xml_file, xpath_root, xpaths, rows_per_batch, full_schema=False):

    xpath_root.data_counter = 1

    xpath_elem = xpath_root

    rows_counter = 0

    current_xpaths = []

    skip = False
    skip_xpath = None

    context = etree.iterparse(
        xml_file,
        events=(
            "start",
            "end",
        ),
        remove_blank_text=True,
    )

    for event, elem in context:
        elem.tag = etree.QName(elem.tag).localname

        if event == "start":
            current_xpaths.append(elem.tag)

            if (
                elem.tag in xpath_elem.children
                and xpath_elem.xpaths + [elem.tag] == current_xpaths
            ):
                skip = False
                xpath_elem = xpath_elem.children[elem.tag]
                xpath_elem.data_counter += 1
                if xpath_elem.is_list:
                    # add missing offsets to match parent
                    if len(xpath_elem.data_offsets) != xpath_elem.parent.data_counter:
                        xpath_elem.data_offsets = (
                            xpath_elem.data_offsets[:-1]
                            + [None]
                            * (
                                xpath_elem.parent.data_counter
                                - len(xpath_elem.data_offsets)
                            )
                            + [xpath_elem.data_offsets[-1]]
                        )
                else:
                    # current data counter is short compared to parent
                    if xpath_elem.data_counter != xpath_elem.parent.data_counter:
                        # add missing rows as None
                        if xpath_elem.is_simple and not xpath_elem.is_dict:
                            missing_rows = (
                                xpath_elem.parent.data_counter - xpath_elem.data_counter
                            )
                            if missing_rows:
                                xpath_elem.data_vector.extend([None] * missing_rows)
                        # reset counter to match parent
                        xpath_elem.data_counter = xpath_elem.parent.data_counter

                if xpath_elem.is_dict:
                    if xpath_elem.is_simple:
                        child_elem = xpath_elem.children[elem.tag]
                        child_elem.data_counter += 1
                        missing_rows = xpath_elem.data_counter - child_elem.data_counter
                        if missing_rows:
                            child_elem.data_vector.extend([None] * missing_rows)
                            child_elem.data_counter = xpath_elem.data_counter

                    if elem.tag + "@tail" in xpath_elem.children:
                        tail_elem = xpath_elem.children[elem.tag + "@tail"]
                        tail_elem.data_counter += 1
                        missing_rows = xpath_elem.data_counter - tail_elem.data_counter
                        if missing_rows:
                            tail_elem.data_vector.extend([None] * missing_rows)
                            tail_elem.data_counter = xpath_elem.data_counter

                    if elem.attrib:
                        # Lxml will include stuff like xlms and xsi items in attributes which we don't want.
                        try:
                            attr_group = xpath_elem.children[elem.tag + "@attributes"]
                            attr_group.data_counter = xpath_elem.data_counter
                            for attr_tag, attr_text in elem.attrib.items():
                                attr_tag = etree.QName(attr_tag).localname

                                attribute = attr_group.children[attr_tag]
                                attribute.data_counter += 1
                                missing_rows = (
                                    attr_group.data_counter - attribute.data_counter
                                )
                                if missing_rows > 0:
                                    attribute.data_vector.extend([None] * missing_rows)
                                    attribute.data_counter = attr_group.data_counter
                                attribute.data_vector.append(attr_text)
                        except:
                            pass
            else:
                if skip is False:
                    skip_xpath = current_xpaths.copy()
                skip = True

        elif event == "end":
            if skip == True:
                if skip_xpath == current_xpaths:
                    skip = False
            else:
                if xpath_elem.is_dict:
                    if xpath_elem.is_simple:
                        xpath_elem.children[elem.tag].data_vector.append(elem.text)

                    if elem.tag + "@tail" in xpath_elem.children:
                        tail_elem = xpath_elem.children[elem.tag + "@tail"]
                        tail_elem.data_vector.append(elem.tail)
                elif xpath_elem.is_simple:
                    xpath_elem.data_vector.append(elem.text)

                # add offsets to track how many child rows belong to this list
                if xpath_elem.children:
                    for child_elem in xpath_elem.children.values():
                        if (
                            child_elem.is_list
                            and child_elem.data_offsets[-1] != child_elem.data_counter
                        ):
                            child_elem.data_offsets.append(child_elem.data_counter)

                if current_xpaths == xpaths:
                    rows_counter += 1
                    if xpaths and rows_per_batch and rows_counter == rows_per_batch:
                        cast_vector_data(xpath_root)
                        set_pyarrow_data(xpath_root, full_schema)

                        skip_check_elem = xpath_elem
                        while skip_check_elem.field_flat:
                            skip_check_elem = next(
                                iter(skip_check_elem.children.values())
                            )
                        yield skip_check_elem.data_pyarrow

                        xpath_elem.clear_data()
                        rows_counter = 0

                xpath_elem = xpath_elem.parent

            elem.clear()
            del current_xpaths[-1]

    if xpaths:
        if rows_counter > 0:
            cast_vector_data(xpath_root)
            set_pyarrow_data(xpath_root, full_schema)

            xpath_elem = xpath_root.find_elem(xpaths)
            while xpath_elem.field_flat:
                xpath_elem = next(iter(xpath_elem.children.values()))
            yield xpath_elem.data_pyarrow
    else:
        cast_vector_data(xpath_root)
        set_pyarrow_data(xpath_root, full_schema)
        yield xpath_root.data_pyarrow


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


def remove_none_nested(data):
    if isinstance(data, dict):
        return {
            k: cleaned_v
            for k, v in data.items()
            if (cleaned_v := remove_none_nested(v)) not in (None, {}, [])
        }
    elif isinstance(data, list):
        # Recursively clean lists if they contain nested dictionaries
        return [
            cleaned_item
            for item in data
            if (cleaned_item := remove_none_nested(item)) not in (None, {}, [])
        ]
    else:
        return data


def write_json(
    output_file,
    input_file,
    xpath_root,
    xpaths,
    processed,
    rows_per_batch,
    gzipfile,
    output_format,
):

    def write_xml_to_json(xml_file, processed):
        """
        :param xml_file: xml file
        :param processed: whether data has been found and processed
        :return: data found and processed
        """

        for xml_arrow in parse_xml_file(xml_file, xpath_root, xpaths, rows_per_batch):
            while isinstance(xml_arrow, pa.ListArray):
                xml_arrow = xml_arrow.flatten()

            pylist = xml_arrow.to_pylist()

            pylist = remove_none_nested(pylist)

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
    xpath_root,
    xpaths,
    processed,
    rows_per_batch,
):

    ctx = df.SessionContext()
    temp_dir = tempfile.gettempdir()

    schema_type = convert_xpath_tree_to_schema_type(xpath_root, xpaths)
    pyarrow_schema = pa.schema(schema_type)

    def write_xml_to_parquet(xml_file, processed):
        """
        :param xml_file: xml file
        :param pyarrow_schema: PyArrow schema
        :param processed: whether data has been found and processed
        :return: data found and processed
        """

        for xml_arrow in parse_xml_file(
            xml_file, xpath_root, xpaths, rows_per_batch, full_schema=True
        ):
            while isinstance(xml_arrow, pa.ListArray):
                xml_arrow = xml_arrow.flatten()

            # pyarrow cast has bugs so we have to add all missing columns in set_pyarrow_field for now
            # xml_arrow = xml_arrow.cast(schema_type)

            table = pa.Table.from_struct_array(xml_arrow)

            if len(table) > 0:
                # writer.write_table(table)
                df_table = ctx.from_arrow(table)
                temp_file = os.path.join(temp_dir, "temp_" + output_file)
                df_table.write_parquet(temp_file)
                new_table = pq.read_table(temp_file)
                writer.write_table(new_table)
                os.remove(temp_file)

                processed = True

        return processed

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


def convert_xml_file(
    xsd_file,
    input_file,
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
    :param input_file: xml input file
    :param xml_path: whether to parse a specific xml path
    :param output_format: jsonl or json
    :param output_file: output file
    :param output_path: directory to save file
    :param gzipfile: gzip saved file
    :param delete_xml: optional delete xml file after converting
    """

    processed = False

    xml_schema = xmlschema.XMLSchema11(xsd_file)

    xpath_root = convert_xsd_to_xpath_tree(xml_schema, max_recursion)

    xpaths = None
    if xml_path:
        xpaths = xml_path.split("/")
        xpaths = xpaths[1:]
        xpath_root.trim_elements(xpaths)

    xpath_root.reset_fields()

    if flat_attributes:
        xpath_root.flatten_attributes()

    if flat_elements:
        xpath_root.flatten_elements()

    _logger.info("Parsing " + input_file)
    _logger.info("Writing to file " + output_file)

    if output_format in ["json", "jsonl"]:
        processed = write_json(
            output_file,
            input_file,
            xpath_root,
            xpaths,
            processed,
            rows_per_batch,
            gzipfile,
            output_format,
        )

    elif output_format in ["parquet"]:
        processed = write_parquet(
            output_file,
            input_file,
            xpath_root,
            xpaths,
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

    _logger.info("Started processing For XML files..")

    if output_path and not os.path.exists(output_path):
        _logger.error("invalid output_path specified")
        sys.exit(1)

    expanded_files = []
    for pattern in xml_files:
        matches = glob(pattern)
        if matches:
            expanded_files.extend(matches)

    file_list = list(dict.fromkeys(expanded_files))
    file_count = len(file_list)

    if multi > 1:
        parse_queue_pool = Pool(processes=multi)

    _logger.info("Found " + str(file_count) + " total files")

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
                convert_xml_file,
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
            convert_xml_file(
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


def to_pyarrow_batches(
    xml_schema,
    xml_file,
    max_recursion=2,
    flat_attributes=False,
    flat_elements=False,
    xml_path=None,
    rows_per_batch=None,
):
    """
    :param xml_schema: xml_schema
    :param xml_file: xml file
    :param xml_path: whether to parse a specific xml path
    """

    processed = False

    xpath_root = convert_xsd_to_xpath_tree(xml_schema, max_recursion)

    xpaths = None
    if xml_path:
        xpaths = xml_path.split("/")
        xpaths = xpaths[1:]
        xpath_root.trim_elements(xpaths)

    xpath_root.reset_fields()

    if flat_elements:
        xpath_root.flatten_elements()

    if flat_attributes:
        xpath_root.flatten_attributes()

    for xml_arrow in parse_xml_file(xml_file, xpath_root, xpaths, rows_per_batch):
        while isinstance(xml_arrow, pa.ListArray):
            xml_arrow = xml_arrow.flatten()
        yield xml_arrow


def to_pylist_batches(
    xml_schema,
    xml_file,
    max_recursion=2,
    flat_attributes=False,
    flat_elements=False,
    xml_path=None,
    rows_per_batch=None,
):

    for xml_arrow in to_pyarrow_batches(
        xml_schema,
        xml_file,
        max_recursion,
        flat_attributes,
        flat_elements,
        xml_path,
        rows_per_batch,
    ):
        pylist = xml_arrow.to_pylist()
        pylist = remove_none_nested(pylist)

        yield pylist


def to_pyarrow(
    xml_schema,
    xml_file,
    max_recursion=2,
    flat_attributes=False,
    flat_elements=False,
    xml_path=None,
):
    for batch in to_pyarrow_batches(
        xml_schema,
        xml_file,
        max_recursion,
        flat_attributes,
        flat_elements,
        xml_path,
        rows_per_batch=None,
    ):
        return batch


def to_pylist(
    xml_schema,
    xml_file,
    max_recursion=2,
    flat_attributes=False,
    flat_elements=False,
    xml_path=None,
):
    for batch in to_pylist_batches(
        xml_schema,
        xml_file,
        max_recursion,
        flat_attributes,
        flat_elements,
        xml_path,
        rows_per_batch=None,
    ):
        return batch
