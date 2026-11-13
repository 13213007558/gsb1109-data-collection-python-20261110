from argparse import ArgumentParser, RawTextHelpFormatter

from warcio.indexer import Indexer
from warcio.checker import Checker
from warcio.extractor import Extractor
from warcio.recompressor import Recompressor
from warcio.subset import SubsetError, SubsetExporter, SubsetPolicy


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

    subset = subparsers.add_parser(
        'subset', help='Export a reproducible subset of WARC/ARC records',
        formatter_class=RawTextHelpFormatter)
    subset.add_argument('inputs', nargs='+',
                        help='input file(s), or "-" for stdin')
    subset.add_argument('output',
                        help='output WARC; use .gz for record-compressed gzip')
    subset.add_argument('-m', '--manifest',
                        help='write a JSON manifest of included records '
                             'and unresolved dependencies')
    subset.add_argument('-r', '--record-id', action='append', default=[],
                        metavar='ID',
                        help='seed by WARC-Record-ID (repeatable)')
    subset.add_argument('-u', '--url', action='append', default=[],
                        metavar='URL',
                        help='seed by exact WARC-Target-URI (repeatable)')
    subset.add_argument('--seed', action='append', default=[],
                        metavar='ID|URL',
                        help='seed; record IDs are detected by urn:/<...>')
    subset.add_argument('-f', '--follow', default='all',
                        help='comma-separated relation policies to follow: '
                             'concurrent,refers-to,payload,warcinfo (all|none)')
    subset.add_argument('--no-warcinfo', action='store_true',
                        help='do not include warcinfo records')
    subset.add_argument('-n', '--max-records', type=int,
                        help='fail instead of writing more than this many records')
    subset.add_argument('--no-gzip', action='store_true',
                        help='write an uncompressed WARC')
    subset.add_argument('--verify', action='store_true',
                        help='verify the written archive and exit non-zero '
                             'on unresolved references')
    subset.set_defaults(func=subsetter)

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
def subsetter(cmd):
    try:
        policy = SubsetPolicy(
            follow=cmd.follow,
            include_warcinfo=False if cmd.no_warcinfo else None)
    except ValueError as e:
        sys.exit(str(e))

    exporter = SubsetExporter(cmd.inputs, output=cmd.output,
                              manifest=cmd.manifest, policy=policy,
                              seed_ids=cmd.record_id, seed_uris=cmd.url,
                              max_records=cmd.max_records,
                              gzip_output=False if cmd.no_gzip else None)

    for seed in cmd.seed:
        exporter.add_seed(seed)

    try:
        exporter.export()
    except SubsetError as e:
        sys.exit(str(e))

    if cmd.manifest:
        data = exporter.manifest_data()
        if data['complete']:
            sys.stderr.write('Subset complete: {0} records\n'.format(
                data['record_count']))
        else:
            sys.stderr.write('Subset incomplete: {0} unresolved dependencies\n'.format(
                len(data['missing'])))

    if cmd.verify:
        problems = exporter.verify()
        if problems:
            for problem in problems:
                sys.stderr.write(problem + '\n')
            sys.exit(1)
        sys.stderr.write('Verification passed\n')


# ============================================================================
if __name__ == "__main__":  #pragma: no cover
    main()

