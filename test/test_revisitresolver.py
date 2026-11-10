from warcio.archiveiterator import ArchiveIterator
from warcio.revisitresolver import (RevisitResolver, RevisitResolution,
                                    WarcFileFinder)
from warcio.warcwriter import BufferWARCWriter
from warcio.statusandheaders import StatusAndHeaders

from io import BytesIO

from . import get_test_file


BODY = b'<html><body>Shared body stored once</body></html>\r\n'
PAYLOAD = (b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: '
           + str(len(BODY)).encode('ascii') + b'\r\n\r\n' + BODY)

BODY_ID = 'urn:uuid:11111111-1111-1111-1111-111111111111'
SHARED_ID = 'urn:uuid:22222222-2222-2222-2222-222222222222'
MISSING_TARGET_ID = 'urn:uuid:99999999-9999-9999-9999-999999999999'


# ============================================================================
def make_response(record_id, digest=None):
    writer = BufferWARCWriter(gzip=False)
    record = writer.create_warc_record(
        'http://example.com/', 'response',
        payload=BytesIO(PAYLOAD),
        warc_headers_dict={'WARC-Record-ID': '<' + record_id + '>'})
    if digest:
        record.rec_headers.replace_header('WARC-Payload-Digest', digest)
    return record


def make_revisit(record_id, refers_to_id=None, digest='sha1:' + 'A' * 32):
    writer = BufferWARCWriter(gzip=False)
    headers = {'WARC-Record-ID': '<' + record_id + '>'}
    if refers_to_id:
        headers['WARC-Refers-To'] = '<' + refers_to_id + '>'

    return writer.create_revisit_record(
        'http://example.com/', digest,
        'http://example.com/', '2026-01-01T00:00:00Z',
        http_headers=StatusAndHeaders('200 OK', [('Content-Type', 'text/html')],
                                      protocol='HTTP/1.1'),
        warc_headers_dict=headers)


def dict_finder(records):
    def finder(record_id):
        return records.get(record_id)
    return finder


def load_revisit_records():
    with open(get_test_file('example-revisit-refs.warc'), 'rb') as fh:
        return [record for record in ArchiveIterator(fh)
                if record.rec_type == 'revisit']


# ============================================================================
class TestRevisitResolver(object):
    def test_complete_chain_in_memory(self):
        response = make_response(BODY_ID)
        revisit = make_revisit(SHARED_ID, BODY_ID,
                               digest=response.rec_headers.get_header(
                                   'WARC-Payload-Digest'))

        resolver = RevisitResolver(dict_finder({BODY_ID: response}))
        result = resolver.resolve(revisit)

        assert result.status == RevisitResolution.COMPLETE
        assert result.is_complete
        assert result.problem_id is None
        assert result.content_record is response
        assert result.record is revisit
        assert result.chain == [revisit, response]
        assert result.lookup_count == 1

    def test_missing_reference(self):
        revisit = make_revisit(SHARED_ID, MISSING_TARGET_ID)

        resolver = RevisitResolver(dict_finder({}))
        result = resolver.resolve(revisit)

        assert result.status == RevisitResolution.MISSING
        assert not result.is_complete
        assert result.problem_id == MISSING_TARGET_ID
        assert result.content_record is None
        assert result.content_stream() is None

    def test_cycle_detected(self):
        id_a = 'urn:uuid:aaaaaaaa-0000-0000-0000-000000000000'
        id_b = 'urn:uuid:bbbbbbbb-0000-0000-0000-000000000000'
        rev_a = make_revisit(id_a, id_b)
        rev_b = make_revisit(id_b, id_a)

        resolver = RevisitResolver(dict_finder({id_a: rev_a, id_b: rev_b}))
        result = resolver.resolve(rev_a)

        assert result.status == RevisitResolution.CYCLE
        assert not result.is_complete
        assert result.problem_id == id_a
        assert result.chain == [rev_a, rev_b]
        assert result.content_record is None

    def test_digest_mismatch(self):
        response = make_response(BODY_ID)
        revisit = make_revisit(SHARED_ID, BODY_ID)

        resolver = RevisitResolver(dict_finder({BODY_ID: response}))
        result = resolver.resolve(revisit)

        assert result.status == RevisitResolution.DIGEST_MISMATCH
        assert not result.is_complete
        # mismatch located at the revisit hop
        assert result.problem_id == SHARED_ID
        # content record still available, but result is not complete
        assert result.content_record is response

    def test_max_depth_exceeded(self):
        ids = ['urn:uuid:00000000-0000-0000-0000-%012d' % i
               for i in range(4)]
        records = {}
        chain = []
        for i in range(3):
            record = make_revisit(ids[i], ids[i + 1])
            records[ids[i]] = record
            chain.append(record)
        records[ids[3]] = make_response(ids[3])

        resolver = RevisitResolver(dict_finder(records), max_depth=2)
        result = resolver.resolve(chain[0])

        assert result.status == RevisitResolution.MAX_DEPTH_EXCEEDED
        assert not result.is_complete
        assert result.problem_id == ids[3]
        # lookups bounded by max_depth, not by actual chain length
        assert result.lookup_count == 2
        assert resolver.finder_call_count == 2

    def test_revisit_without_reference(self):
        revisit = make_revisit(SHARED_ID, refers_to_id=None)

        resolver = RevisitResolver(dict_finder({}))
        result = resolver.resolve(revisit)

        assert result.status == RevisitResolution.NO_REFERENCE
        assert not result.is_complete
        assert result.problem_id == SHARED_ID

    def test_non_revisit_record_resolves_to_itself(self):
        response = make_response(BODY_ID)

        resolver = RevisitResolver(dict_finder({}))
        result = resolver.resolve(response)

        assert result.status == RevisitResolution.COMPLETE
        assert result.is_complete
        assert result.content_record is response
        assert result.lookup_count == 0
        assert resolver.finder_call_count == 0

    def test_shared_cache_limits_repeated_lookups(self):
        response = make_response(BODY_ID)
        revisit = make_revisit(SHARED_ID, BODY_ID,
                               digest=response.rec_headers.get_header(
                                   'WARC-Payload-Digest'))
        missing = make_revisit(SHARED_ID, MISSING_TARGET_ID)

        calls = []

        def counting_finder(record_id):
            calls.append(record_id)
            return {BODY_ID: response}.get(record_id)

        cache = {}
        resolver_one = RevisitResolver(counting_finder, cache=cache)
        resolver_two = RevisitResolver(counting_finder, cache=cache)

        assert resolver_one.resolve(revisit).is_complete
        assert resolver_two.resolve(revisit).is_complete
        assert calls == [BODY_ID]

        # negative results are cached as well
        assert resolver_one.resolve(missing).status == RevisitResolution.MISSING
        assert resolver_two.resolve(missing).status == RevisitResolution.MISSING
        assert calls == [BODY_ID, MISSING_TARGET_ID]


# ============================================================================
class TestWarcFileFinderDemo(object):
    """Two small archives: the body lives in example-revisit-body.warc,
    the revisit records live in example-revisit-refs.warc. One revisit
    shares that body, the other points at a record that exists nowhere.
    """

    FILES = [get_test_file('example-revisit-body.warc'),
             get_test_file('example-revisit-refs.warc')]

    def test_shared_body_complete(self):
        finder = WarcFileFinder(self.FILES)
        try:
            resolver = RevisitResolver(finder)
            revisit = load_revisit_records()[0]

            result = resolver.resolve(revisit)

            assert result.status == RevisitResolution.COMPLETE
            assert result.is_complete
            assert result.content_stream().read() == BODY

            # referring record and body-source record keep their own
            # metadata; nothing is merged into a fabricated record
            assert result.record is revisit
            assert result.record.rec_headers.get_header('WARC-Type') == 'revisit'
            assert result.record.rec_headers.get_header('WARC-Date') == '2026-01-02T00:00:00Z'
            assert result.record.rec_headers.get_header('WARC-Record-ID') == '<' + SHARED_ID + '>'

            assert result.content_record is not revisit
            assert result.content_record.rec_headers.get_header('WARC-Type') == 'response'
            assert result.content_record.rec_headers.get_header('WARC-Date') == '2026-01-01T00:00:00Z'
            assert result.content_record.rec_headers.get_header('WARC-Record-ID') == '<' + BODY_ID + '>'
        finally:
            finder.close()

    def test_missing_reference_incomplete(self):
        finder = WarcFileFinder(self.FILES)
        try:
            resolver = RevisitResolver(finder)
            revisit = load_revisit_records()[1]

            result = resolver.resolve(revisit)

            assert result.status == RevisitResolution.MISSING
            assert not result.is_complete
            assert result.problem_id == MISSING_TARGET_ID
            assert result.content_record is None
            assert result.content_stream() is None
        finally:
            finder.close()

    def test_finder_reads_local_files_only(self):
        finder = WarcFileFinder(self.FILES)
        try:
            assert finder(MISSING_TARGET_ID) is None

            record = finder(BODY_ID)
            assert record is not None
            assert record.rec_headers.get_header('WARC-Type') == 'response'
            assert record.content_stream().read() == BODY
        finally:
            finder.close()
