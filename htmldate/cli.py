"""
Implementing a basic command-line interface.
"""

import argparse
import json
import logging
import sys

from platform import python_version
from typing import Any

from lxml.html import HtmlElement

from . import __version__
from .core import find_date
from .sources import SourcePolicy
from .utils import fetch_url, is_wrong_document


def _load_policy(value: str | None) -> SourcePolicy | None:
    "Read an inline JSON policy or a policy stored in a file."
    if value is None:
        return None
    if value.lstrip().startswith("{"):
        return SourcePolicy.from_json(value)
    with open(value, mode="r", encoding="utf-8") as policy_file:
        return SourcePolicy.from_json(policy_file.read())


def _render(result: str | dict[str, Any] | None) -> str:
    "Serialize a date or an explanation audit for standard output."
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)
    return result or "None"


def cli_examine(
    htmlstring: str | HtmlElement | None,
    args: argparse.Namespace,
) -> str | dict[str, Any] | None:
    """Generic safeguards and triggers"""
    # safety check
    if htmlstring is None or is_wrong_document(htmlstring):
        sys.stderr.write("# ERROR: document is empty or too large\n")
        return None
    return find_date(
        htmlstring,
        extensive_search=not args.fast,
        original_date=args.original,
        verbose=args.verbose,
        min_date=args.mindate,
        max_date=args.maxdate,
        source_policy=args.source_policy,
        explain=args.explain,
    )


def parse_args(args: list[str]) -> argparse.Namespace:
    """Define parser for command-line arguments"""
    argsparser = argparse.ArgumentParser()
    argsparser.add_argument(
        "-f", "--fast", help="fast mode: disable extensive search", action="store_true"
    )
    argsparser.add_argument(
        "-i",
        "--inputfile",
        help="""name of input file for batch processing
                            (similar to wget -i)""",
        type=str,
    )
    argsparser.add_argument(
        "--original", help="original date prioritized", action="store_true"
    )
    argsparser.add_argument(
        "-min", "--mindate", help="earliest acceptable date (ISO 8601 YMD)", type=str
    )
    argsparser.add_argument(
        "-max", "--maxdate", help="latest acceptable date (ISO 8601 YMD)", type=str
    )
    argsparser.add_argument("-u", "--URL", help="custom URL download", type=str)
    argsparser.add_argument(
        "--source-policy",
        help="JSON source policy (path to a file or inline JSON) controlling "
        "the allow/disable/require evidence channels",
        type=str,
    )
    argsparser.add_argument(
        "--explain",
        help="output a JSON audit of adopted and excluded channels instead of a date",
        action="store_true",
    )
    argsparser.add_argument(
        "-v", "--verbose", help="increase output verbosity", action="store_true"
    )
    argsparser.add_argument(
        "--version",
        help="show version information and exit",
        action="version",
        version=f"Htmldate {__version__} - Python {python_version()}",
    )
    return argsparser.parse_args(args)


def process_args(args: argparse.Namespace) -> None:
    """Process the arguments passed on the command-line."""
    # verbosity
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # resolve the source policy once: inline JSON or a JSON file
    try:
        args.source_policy = _load_policy(args.source_policy)
    except (OSError, ValueError) as err:
        sys.exit(f"Invalid source policy: {err}\n")

    # input type
    if not args.inputfile:
        # URL as input
        if args.URL:
            htmlstring = fetch_url(args.URL)
            if htmlstring is None:
                sys.exit(f"No data for URL: {args.URL}\n")
        # unicode check
        else:
            try:
                htmlstring = sys.stdin.read()
            except UnicodeDecodeError as err:
                sys.exit(f"Wrong buffer encoding: {err}\n")
        result = cli_examine(htmlstring, args)
        if result is not None:
            sys.stdout.write(_render(result) + "\n")

    # process input file line by line
    else:
        with open(args.inputfile, mode="r", encoding="utf-8") as inputfile:
            for line in inputfile:
                url = line.strip()
                result = cli_examine(fetch_url(url), args)
                sys.stdout.write(f"{url}\t{_render(result)}\n")


def main() -> None:
    """Run as a command-line utility."""
    # arguments
    args = parse_args(sys.argv[1:])
    # process input on STDIN
    process_args(args)


if __name__ == "__main__":
    main()
