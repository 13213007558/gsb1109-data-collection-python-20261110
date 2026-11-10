from argparse import ArgumentParser, RawTextHelpFormatter

from warcio.indexer import Indexer
from warcio.checker import Checker
from warcio.extractor import Extractor
from warcio.recompressor import Recompressor
from warcio.redactor import (
    DEFAULT_SENSITIVE_HEADERS,
    RedactionError,
    RedactionPolicy,
    sanitize_file,
)


try:
    from importlib.metadata import version
except ImportError:
    import pkg_resources
    def version(package):
        return pkg_resources.get_distribution(package).version
import sys


# ============================================================================
def main(args=None):
    parser = ArgumentParser(description='warcio utils',
                            formatter_class=RawTextHelpFormatter)

    parser.add_argument('-V', '--version', action='version', version=get_version())

    subparsers = parser.add_subparsers(dest='cmd')
    subparsers.required = True

    index = subparsers.add_parser('index', help='WARC/ARC Indexer')
    index.add_argument('inputs', nargs='*', help='input file(s); default is stdin')
    index.add_argument('-f', '--fields', default='offset,warc-type,warc-target-uri',
            help='fields to include in json output; supported values are "offset", '
                 '"length", "filename", "http:status", "http:{http-header}" '
                 '(arbitrary http header), and "{warc-header}" (arbitrary warc '
                 'record header)')
    index.add_argument('-o', '--output', help='output file; default is stdout')
    index.set_defaults(func=indexer)

    recompress = subparsers.add_parser('recompress', help='Recompress an existing WARC or ARC',
                                       description='Read an existing, possibly broken WARC ' +
                                                   'and correctly recompress it to fix any compression errors\n' +
                                                   'Also convert any ARC file to a standard compressed WARC file')
    recompress.add_argument('filename')
    recompress.add_argument('output')
    recompress.add_argument('-v', '--verbose', action='store_true')
    recompress.set_defaults(func=recompressor)

    extract = subparsers.add_parser('extract', help='Extract WARC/ARC Record')
    extract.add_argument('filename')
    extract.add_argument('offset')
    group = extract.add_mutually_exclusive_group()
    group.add_argument('--payload', action='store_true', help='output only record payload (after content and transfer decoding, if applicable)')
    group.add_argument('--headers', action='store_true', help='output only record headers (and http headers, if applicable)')

    extract.set_defaults(func=extractor)

    check = subparsers.add_parser('check', help='WARC digest checker')
    check.add_argument('inputs', nargs='+')
    check.add_argument('-v', '--verbose', action='store_true')
    check.set_defaults(func=checker)

    redact = subparsers.add_parser(
        'redact',
        help='Redact sensitive HTTP headers and form fields into a new WARC',
        description='Read a WARC/ARC read-only and write a sanitized WARC. '
                    'Removes Cookie/Set-Cookie and authentication headers by '
                    'default and any named form fields from urlencoded request '
                    'bodies. Digests and lengths are recomputed for every '
                    'modified record; a JSONL manifest records the mapping '
                    'from each source record to the output record.')
    redact.add_argument('input', help='input WARC/ARC file (opened read-only)')
    redact.add_argument('output', help='output WARC file (must differ from input)')
    redact.add_argument('-m', '--manifest',
                        help='JSONL manifest of source -> output record mapping')
    redact.add_argument('--header', action='append', dest='headers',
                        help='HTTP header name to remove (case-insensitive); '
                             'may be repeated. Defaults to: '
                             + ', '.join(DEFAULT_SENSITIVE_HEADERS))
    redact.add_argument('--form-field', action='append', dest='form_fields',
                        default=[],
                        help='form field name to remove from '
                             'application/x-www-form-urlencoded request bodies; '
                             'may be repeated')
    redact.add_argument('--gzip', action='store_true',
                        help='write the output as a per-record gzipped WARC')
    redact.add_argument('--no-fail-on-parse-error', action='store_true',
                        help='keep an unparseable form body with a warning '
                             'instead of flagging the record as failed')
    redact.add_argument('--strict', action='store_true',
                        help='exit non-zero if any record fails redaction or '
                             'a recomputed digest cannot be verified')
    redact.set_defaults(func=redactor)

    cmd = parser.parse_args(args=args)
    cmd.func(cmd)


# ============================================================================
def get_version():
    return '%(prog)s ' + version('warcio')


# ============================================================================
def indexer(cmd):
    inputs = cmd.inputs or ('-',)  # default to stdin
    _indexer = Indexer(cmd.fields, inputs, cmd.output)
    _indexer.process_all()


# ============================================================================
def checker(cmd):
    _checker = Checker(cmd)
    sys.exit(_checker.process_all())


# ============================================================================
def extractor(cmd):
    _extractor = Extractor(cmd.filename, cmd.offset)
    _extractor.extract(cmd.payload, cmd.headers)


# ============================================================================
def recompressor(cmd):
    _recompressor = Recompressor(cmd.filename, cmd.output, cmd.verbose)
    _recompressor.recompress()


# ============================================================================
def redactor(cmd):
    policy = RedactionPolicy(
        sensitive_headers=cmd.headers or list(DEFAULT_SENSITIVE_HEADERS),
        sensitive_form_fields=cmd.form_fields,
        fail_on_parse_error=not cmd.no_fail_on_parse_error,
    )

    try:
        results, introduced, pre_existing = sanitize_file(
            cmd.input,
            cmd.output,
            policy=policy,
            gzip=cmd.gzip,
            manifest_path=cmd.manifest,
            strict=cmd.strict,
        )
    except RedactionError as exc:
        sys.stderr.write('redaction failed: {0}\n'.format(exc))
        sys.exit(1)

    redacted = sum(1 for result in results if result.status == 'redacted')
    failed = sum(1 for result in results if result.status == 'failed')
    print('{0} records processed: {1} redacted, {2} retained, {3} failed'.format(
        len(results), redacted, len(results) - redacted - failed, failed))
    for problem in introduced:
        sys.stderr.write('digest verification problem: {0}\n'.format(problem))
    for problem in pre_existing:
        sys.stderr.write('pre-existing source digest issue retained: {0}\n'
                         .format(problem))
    if introduced:
        sys.exit(1)


# ============================================================================
if __name__ == "__main__":  #pragma: no cover
    main()

