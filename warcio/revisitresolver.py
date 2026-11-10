"""
Resolve WARC ``WARC-Refers-To`` reference chains (used by revisit records)
across one or more archives.

A revisit record stores only HTTP headers; its payload body lives in an
earlier record, possibly in a different WARC file, so iterating a single
archive file-by-file cannot yield the complete capture.

The caller supplies a *finder* -- a callable mapping a WARC-Record-ID to
a record (backed by their own index, database, or local files). The
library never performs network access on its own; all lookup behaviour
is caller-provided.

The resolution result keeps the revisit record and the body-source
record as separate objects, each with their own metadata. They are never
merged into a fabricated "original" response record.
"""

import six

from warcio.archiveiterator import ArchiveIterator


# ============================================================================
class RevisitResolution(object):
    """Result of resolving a record's ``WARC-Refers-To`` reference chain.

    ``record`` is the record the resolution started from (usually a
    revisit); ``content_record`` is the record that actually carries the
    body, if one was found. Both keep their own ``rec_headers`` and
    ``http_headers`` untouched.
    """

    #: reference chain resolved to a body-bearing record, digests consistent
    COMPLETE = 'complete'
    #: the finder could not locate a referenced record
    MISSING = 'missing'
    #: the reference chain loops back on itself
    CYCLE = 'cycle'
    #: body found, but a revisit's payload digest does not match it
    DIGEST_MISMATCH = 'digest-mismatch'
    #: reference chain exceeded the resolver's max_depth
    MAX_DEPTH_EXCEEDED = 'max-depth-exceeded'
    #: a revisit record with no WARC-Refers-To header to follow
    NO_REFERENCE = 'no-reference'

    def __init__(self, record, status, chain, content_record=None,
                 problem_id=None, lookup_count=0):
        #: the record resolution started from (metadata preserved as-is)
        self.record = record
        #: one of the status constants above
        self.status = status
        #: every record visited, in order, starting with ``record``
        self.chain = chain
        #: the record carrying the body (None when not found); its
        #: metadata is kept separate from ``record``
        self.content_record = content_record
        #: normalized record id locating the failure (missing target,
        #: cycle entry point, or digest-mismatched hop), if any
        self.problem_id = problem_id
        #: how many finder calls this resolution performed
        self.lookup_count = lookup_count

    @property
    def is_complete(self):
        """True only when the chain fully resolved to consistent content."""
        return self.status == self.COMPLETE

    @property
    def revisit_record(self):
        """Alias for ``record`` -- the revisit (referring) record."""
        return self.record

    def content_stream(self):
        """Stream of the body from the content record, or None if the
        resolution did not locate one."""
        if not self.content_record:
            return None

        return self.content_record.content_stream()

    def __repr__(self):
        return ('RevisitResolution(status={0}, chain_length={1}, '
                'problem_id={2})').format(self.status, len(self.chain),
                                          self.problem_id)


# ============================================================================
class RevisitResolver(object):
    """Follows ``WARC-Refers-To`` chains using a caller-provided finder.

    :param finder: callable(normalized_record_id) -> ArcWarcRecord or None
    :param max_depth: maximum number of reference hops followed per resolve
    :param cache: optional mutable mapping for record-id -> record (or None)
        results; pass a shared mapping to reuse lookups across resolves.
        Negative (missing) results are cached as well.
    """

    def __init__(self, finder, max_depth=8, cache=None):
        self.finder = finder
        self.max_depth = max_depth
        self.cache = cache if cache is not None else {}
        self.finder_call_count = 0

    @staticmethod
    def normalize_record_id(record_id):
        if not record_id:
            return None

        record_id = record_id.strip()
        if record_id.startswith('<') and record_id.endswith('>'):
            record_id = record_id[1:-1]

        return record_id.strip()

    def _lookup(self, record_id):
        if record_id in self.cache:
            return self.cache[record_id]

        self.finder_call_count += 1
        record = self.finder(record_id)
        self.cache[record_id] = record
        return record

    def resolve(self, record):
        """Resolve the reference chain starting at ``record``.

        Returns a RevisitResolution; inspect ``result.status`` and
        ``result.is_complete`` to distinguish complete captures from
        incomplete ones, and ``result.problem_id`` to locate failures.
        """
        chain = [record]
        lookup_count = 0

        seen = set()
        own_id = self.normalize_record_id(
            record.rec_headers.get_header('WARC-Record-ID'))
        if own_id:
            seen.add(own_id)

        current = record

        while True:
            refers_to = current.rec_headers.get_header('WARC-Refers-To')

            if not refers_to:
                if current.rec_type == 'revisit':
                    # a revisit with nothing to point at is incomplete
                    problem_id = self.normalize_record_id(
                        current.rec_headers.get_header('WARC-Record-ID'))
                    return RevisitResolution(record,
                                             RevisitResolution.NO_REFERENCE,
                                             chain,
                                             problem_id=problem_id,
                                             lookup_count=lookup_count)

                # body-bearing record reached: verify digests of any
                # revisit hops against the content record
                return self._check_digests(record, chain, current,
                                           lookup_count)

            target_id = self.normalize_record_id(refers_to)

            if target_id in seen:
                return RevisitResolution(record,
                                         RevisitResolution.CYCLE,
                                         chain,
                                         problem_id=target_id,
                                         lookup_count=lookup_count)

            if len(chain) > self.max_depth:
                return RevisitResolution(record,
                                         RevisitResolution.MAX_DEPTH_EXCEEDED,
                                         chain,
                                         problem_id=target_id,
                                         lookup_count=lookup_count)

            seen.add(target_id)

            calls_before = self.finder_call_count
            next_record = self._lookup(target_id)
            lookup_count += self.finder_call_count - calls_before

            if next_record is None:
                return RevisitResolution(record,
                                         RevisitResolution.MISSING,
                                         chain,
                                         problem_id=target_id,
                                         lookup_count=lookup_count)

            chain.append(next_record)
            current = next_record

    def _check_digests(self, record, chain, content_record, lookup_count):
        content_digest = content_record.rec_headers.get_header(
            'WARC-Payload-Digest')

        for hop in chain:
            hop_digest = hop.rec_headers.get_header('WARC-Payload-Digest')
            if (hop_digest and content_digest and
                    hop_digest.strip().lower() != content_digest.strip().lower()):
                problem_id = self.normalize_record_id(
                    hop.rec_headers.get_header('WARC-Record-ID'))
                return RevisitResolution(record,
                                         RevisitResolution.DIGEST_MISMATCH,
                                         chain,
                                         content_record=content_record,
                                         problem_id=problem_id,
                                         lookup_count=lookup_count)

        return RevisitResolution(record,
                                 RevisitResolution.COMPLETE,
                                 chain,
                                 content_record=content_record,
                                 lookup_count=lookup_count)


# ============================================================================
class WarcFileFinder(object):
    """A finder backed by local WARC/ARC files.

    Indexes records by WARC-Record-ID on first use and re-parses
    individual records on demand. Reads local files only -- it never
    performs any network access.
    """

    def __init__(self, paths):
        if isinstance(paths, six.string_types):
            paths = [paths]

        self.paths = list(paths)
        self._index = None
        self._files = {}

    def __call__(self, record_id):
        if self._index is None:
            self._build_index()

        entry = self._index.get(record_id)
        if not entry:
            return None

        path, offset, length = entry
        fh = self._open(path)
        fh.seek(offset)

        iterator = ArchiveIterator(fh)
        for record in iterator:
            return record

        return None

    def close(self):
        for fh in self._files.values():
            fh.close()

        self._files = {}

    def _open(self, path):
        fh = self._files.get(path)
        if not fh:
            fh = open(path, 'rb')
            self._files[path] = fh

        return fh

    def _build_index(self):
        self._index = {}

        for path in self.paths:
            fh = self._open(path)
            fh.seek(0)

            iterator = ArchiveIterator(fh)
            for record in iterator:
                record_id = RevisitResolver.normalize_record_id(
                    record.rec_headers.get_header('WARC-Record-ID'))
                if record_id:
                    self._index[record_id] = (path,
                                              iterator.get_record_offset(),
                                              iterator.get_record_length())
