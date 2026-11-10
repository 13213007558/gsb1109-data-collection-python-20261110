"""
Command line entry point for seek verification.

Subcommands:
  verify        re-seek every record named by an index and cross-check it
  truth-index   emit a ground-truth index built from the raw bytes
  scan          sequential integrity scan without any index
  gen-fixtures  materialize the reproducible counterexample bundle
"""

import argparse
import json
import sys

from . import fixtures as fixture_builder
from . import rawreader
from . import report as report_mod
from .errors import CorruptionError, IndexInconsistentError
from .verifier import SeekVerifier, load_index


def _detect_gzip(value, path):
    if value == "gzip":
        return True
    if value == "plain":
        return False
    return rawreader.is_gzip_file(path)


def cmd_verify(args):
    gzip = _detect_gzip(args.format, args.archive)
    entries = load_index(args.index)
    verifier = SeekVerifier(args.archive, gzip=gzip)
    results = verifier.verify_all(entries, partial_consume=args.partial)
    if args.output_format == "json":
        stream = sys.stdout
        for result in results:
            stream.write(report_mod.result_to_jsonl(result) + "\n")
        stream.write(json.dumps({"summary": report_mod.summarize(results)})
                     + "\n")
    else:
        sys.stdout.write(report_mod.render_text(results) + "\n")
    return report_mod.exit_code(results)


def cmd_truth_index(args):
    gzip = _detect_gzip(args.format, args.archive)
    rows = fixture_builder.truth_index(args.archive, gzip=gzip)
    stream = sys.stdout
    for row in rows:
        stream.write(json.dumps(row) + "\n")
    return 0


def cmd_scan(args):
    gzip = _detect_gzip(args.format, args.archive)
    failures = 0
    try:
        for obs in rawreader.scan_all_records(args.archive, gzip):
            digests = obs.digests("sha256")
            sys.stdout.write(json.dumps({
                "offset": obs.offset,
                "length": obs.physical_length,
                "id": obs.record_id,
                "url": obs.url,
                "type": obs.rec_type,
                "payload_size": digests["payload_size"],
                "payload_sha256": digests["payload"],
                "next_record_offset": obs.next_record_offset,
                "gzip_member": obs.gzip_member,
            }) + "\n")
    except CorruptionError as exc:
        failures = 1
        sys.stderr.write(json.dumps(exc.to_dict()) + "\n")
    return failures


def cmd_gen_fixtures(args):
    manifest = fixture_builder.generate_fixture_bundle(args.directory)
    sys.stdout.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m seekverify",
        description="Offline verification of WARC/ARC seek offsets")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="verify an index against an archive")
    verify.add_argument("archive")
    verify.add_argument("index", help="JSON-lines index file")
    verify.add_argument("--format", choices=("auto", "gzip", "plain"),
                        default="auto")
    verify.add_argument("--partial", type=int, default=0,
                        help="consume only N payload bytes per record")
    verify.add_argument("--output-format", choices=("text", "json"),
                        default="text")
    verify.set_defaults(func=cmd_verify)

    truth = sub.add_parser("truth-index",
                           help="build a ground-truth index from raw bytes")
    truth.add_argument("archive")
    truth.add_argument("--format", choices=("auto", "gzip", "plain"),
                       default="auto")
    truth.set_defaults(func=cmd_truth_index)

    scan = sub.add_parser("scan", help="sequential integrity scan")
    scan.add_argument("archive")
    scan.add_argument("--format", choices=("auto", "gzip", "plain"),
                      default="auto")
    scan.set_defaults(func=cmd_scan)

    gen = sub.add_parser("gen-fixtures",
                         help="generate valid archives and counterexamples")
    gen.add_argument("directory")
    gen.set_defaults(func=cmd_gen_fixtures)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (CorruptionError, IndexInconsistentError) as exc:
        sys.stderr.write(json.dumps(exc.to_dict()) + "\n")
        return 2 if isinstance(exc, CorruptionError) else 1


if __name__ == "__main__":
    sys.exit(main())
