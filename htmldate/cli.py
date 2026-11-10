"""
Implementing a basic command-line interface.
"""

import argparse
import json
import logging
import sys

from datetime import datetime
from platform import python_version

from lxml.html import HtmlElement

from . import __version__
from .core import find_date
from .history import compare_snapshots
from .utils import fetch_url, is_wrong_document


def cli_examine(
    htmlstring: str | HtmlElement | None,
    args: argparse.Namespace,
) -> str | None:
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
        "--compare",
        help="""offline snapshot comparison: path to a JSON file describing
                            several snapshots of the same page (list of objects
                            with "id", "observed_at" and "file" or "html" keys);
                            use "-" to read the description from standard input""",
        type=str,
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


def _read_snapshot_spec(path: str) -> list[dict[str, str]]:
    "Load the JSON snapshot description, from a file or standard input."
    if path == "-":
        raw = sys.stdin.read()
    else:
        try:
            with open(path, mode="r", encoding="utf-8") as inputfile:
                raw = inputfile.read()
        except OSError as err:
            sys.exit(f"Cannot read snapshot description {path}: {err}\n")
    try:
        specs = json.loads(raw)
    except json.JSONDecodeError as err:
        sys.exit(f"Invalid JSON snapshot description: {err}\n")
    if not isinstance(specs, list):
        sys.exit("Snapshot description must be a JSON array\n")
    return specs


def _build_snapshots(
    specs: list[dict[str, str]],
) -> list[tuple[str, datetime, str | bytes]]:
    """Turn JSON specs into (id, observation time, HTML) triples."""
    snapshots: list[tuple[str, datetime, str | bytes]] = []
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            sys.exit(f"Snapshot entry #{index} must be a JSON object\n")
        snapshot_id = spec.get("id") or f"snapshot-{index + 1}"
        observed_raw = spec.get("observed_at")
        if not observed_raw:
            sys.exit(f"Snapshot {snapshot_id}: missing 'observed_at'\n")
        try:
            observed_at = datetime.fromisoformat(observed_raw)
        except ValueError:
            sys.exit(
                f"Snapshot {snapshot_id}: unparseable 'observed_at' "
                f"(expected ISO 8601): {observed_raw}\n"
            )
        if spec.get("file"):
            try:
                with open(spec["file"], "rb") as htmlfile:
                    htmlobject: str | bytes = htmlfile.read()
            except OSError as err:
                sys.exit(
                    f"Snapshot {snapshot_id}: cannot read file "
                    f"{spec['file']}: {err}\n"
                )
        elif "html" in spec:
            htmlobject = spec["html"]
        else:
            sys.exit(
                f"Snapshot {snapshot_id}: provide either a 'file' path "
                "or an inline 'html' string\n"
            )
        snapshots.append((str(snapshot_id), observed_at, htmlobject))
    return snapshots


def run_comparison(args: argparse.Namespace) -> None:
    """Offline entry point: compare snapshots and print the traceable history."""
    specs = _read_snapshot_spec(args.compare)
    snapshots = _build_snapshots(specs)
    history = compare_snapshots(
        snapshots,
        extensive_search=not args.fast,
        min_date=args.mindate,
        max_date=args.maxdate,
    )
    sys.stdout.write(json.dumps(history.as_dict(), ensure_ascii=False, indent=2) + "\n")


def process_args(args: argparse.Namespace) -> None:
    """Process the arguments passed on the command-line."""
    # verbosity
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # offline snapshot comparison
    if args.compare:
        run_comparison(args)
        return

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
            sys.stdout.write(result + "\n")

    # process input file line by line
    else:
        with open(args.inputfile, mode="r", encoding="utf-8") as inputfile:
            for line in inputfile:
                url = line.strip()
                result = cli_examine(fetch_url(url), args)
                sys.stdout.write(f"{url}\t{result or 'None'}\n")


def main() -> None:
    """Run as a command-line utility."""
    # arguments
    args = parse_args(sys.argv[1:])
    # process input on STDIN
    process_args(args)


if __name__ == "__main__":
    main()
