"""
(c) David Lee

Author: David Lee
"""

import argparse
from xmlwiz.convert_xml import convert_xml


def arg_flatten(value):
    """Convert a string representation of truth to True or False."""
    if isinstance(value, bool):
        return value
    if value.lower() in ("attribs", "attributes"):
        return "attributes"
    elif value.lower() in ("elems", "elements"):
        return "elements"
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="XML Wizard")

    parser.add_argument("-x", "--xsd_file", required=True, help="xsd file location.")

    parser.add_argument(
        "--max_recursion",
        default=2,
        type=int,
        help="max recursions for self referencing elements.",
    )

    parser.add_argument("-p", "--xml_path", help="xpath to parse.")

    parser.add_argument(
        "--rows_per_batch",
        type=int,
        help="number of rows to write per batch when using xpath.",
    )

    parser.add_argument(
        "-m", "--multi", type=int, default=1, help="number of parsers. default is 1."
    )

    parser.add_argument(
        "-o",
        "--output_format",
        default="jsonl",
        help="output format json or jsonl. default is jsonl.",
    )

    parser.add_argument("-t", "--output_path", help="output directory.")

    parser.add_argument(
        "-z", "--gzipfile", action="store_true", help="gzip output json file."
    )

    parser.add_argument(
        "--no_overwrite",
        action="store_true",
        help="do not overwrite output file if it exists already.",
    )
    parser.add_argument(
        "--delete_xml",
        action="store_true",
        help="delete xml file after conversion.",
    )

    parser.add_argument(
        "--flatten",
        type=arg_flatten,
        nargs="?",  # Makes the value optional
        const=True,  # Value if --flatten is provided without any value
        default=False,  # Value if --flatten is omitted entirely
        help="Flatten results. (accepts optional 'attributes' or 'elements' values).",
    )

    parser.add_argument(
        "-l",
        "--log_level",
        default="INFO",
        help="logging level. INFO, DEBUG, etc.",
    )

    parser.add_argument("--log_file", help="log file location.")

    parser.add_argument("xml_files", nargs="+", help="xml files to convert")

    args = parser.parse_args()

    convert_xml(
        args.xsd_file,
        args.max_recursion,
        args.xml_path,
        args.rows_per_batch,
        args.multi,
        args.output_format,
        args.output_path,
        args.gzipfile,
        args.no_overwrite,
        args.delete_xml,
        args.flatten,
        args.log_level,
        args.log_file,
        args.xml_files,
    )
