"""
(c) David Lee

Author: David Lee
"""

import argparse

from xmlwiz.convert_xml import convert_xml

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="XML Wizard Parser")
    parser.add_argument("-x", "--xsd_file", required=True, help="xsd file name")
    parser.add_argument(
        "-o",
        "--output_format",
        default="jsonl",
        help="output format json or jsonl. Default is jsonl.",
    )
    parser.add_argument("-t", "--target_path", help="target path. Examples: /proj/test")
    parser.add_argument(
        "-r",
        "--rows_per_batch",
        default=10000,
        help="number of rows to write per batch.",
    )
    parser.add_argument("-z", "--zip", action="store_true", help="gzip output file")
    parser.add_argument("-p", "--xpath", help="xpath to parse out.")
    parser.add_argument(
        "-m", "--multi", type=int, default=1, help="number of parsers. Default is 1."
    )
    parser.add_argument(
        "-n",
        "--no_overwrite",
        action="store_true",
        help="do not overwrite output file if it exists already",
    )
    parser.add_argument("-l", "--log", help="log file")
    parser.add_argument(
        "-v",
        "--verbose",
        default="DEBUG",
        help="verbose output level. INFO, DEBUG, etc.",
    )
    parser.add_argument(
        "-d",
        "--delete_xml",
        action="store_true",
        help="delete xml file after converting to json",
    )
    parser.add_argument(
        "input_files", nargs=argparse.REMAINDER, help="files to convert"
    )

    args = parser.parse_args()

    convert_xml(
        args.xsd_file,
        args.output_format,
        args.rows_per_batch,
        args.target_path,
        args.zip,
        args.xpath,
        args.multi,
        args.no_overwrite,
        args.verbose,
        args.log,
        args.delete_xml,
        args.input_files,
    )
