"""
Deterministic, hand-built WARC fixtures for the layered integrity checker.

The normal WARC writer only emits well-formed records and would recompute or
"fix" lengths, so it cannot produce the contrast cases this checker has to
distinguish (an intact WARC boundary hiding a truncated or contradictory HTTP
message).  These helpers assemble raw record bytes directly, which lets a test
declare exactly which layer is broken while every other layer stays valid.

Running this module as a script writes the canonical contrast archives into
``test/data/integrity/``.
"""

import gzip
import os
import zlib


HERE = os.path.dirname(os.path.realpath(__file__))
FIXTURE_DIR = os.path.join(HERE, 'data', 'integrity')

_RECORD_COUNTER = [0]


def http_response(status_line, headers=(), body=b''):
    lines = ['HTTP/1.1 ' + status_line]
    lines.extend('{0}: {1}'.format(name, value) for name, value in headers)
    head = ('\r\n'.join(lines) + '\r\n\r\n').encode('iso-8859-1')
    return head + body


def http_request(method, target='/', headers=(), body=b''):
    lines = ['{0} {1} HTTP/1.1'.format(method, target)]
    lines.extend('{0}: {1}'.format(name, value) for name, value in headers)
    head = ('\r\n'.join(lines) + '\r\n\r\n').encode('iso-8859-1')
    return head + body


def warc_record(rec_type, target, block, extra_headers=(), record_id=None,
                declared_length=None, omit_separator=False):
    """Assemble one raw WARC record (headers + block + separator)."""
    if record_id is None:
        _RECORD_COUNTER[0] += 1
        record_id = '<urn:uuid:fixed-{0:08d}>'.format(_RECORD_COUNTER[0])
    if declared_length is None:
        declared_length = len(block)

    lines = ['WARC/1.0',
             'WARC-Type: ' + rec_type,
             'WARC-Record-ID: ' + record_id,
             'WARC-Date: 2026-11-10T00:00:00Z']
    if target is not None:
        lines.append('WARC-Target-URI: ' + target)
    lines.extend('{0}: {1}'.format(name, value) for name, value in extra_headers)
    lines.append('Content-Length: {0}'.format(declared_length))

    framing = ('\r\n'.join(lines) + '\r\n\r\n').encode('utf-8')
    separator = b'' if omit_separator else b'\r\n\r\n'
    return {
        'bytes': framing + block + separator,
        'id': record_id,
        'block': block,
        'declared_length': declared_length,
    }


def build_warc(records):
    return b''.join(record['bytes'] for record in records)


def build_warc_gz(records):
    """Record-per-member gzip, matching the WARC.warc.gz convention."""
    out = b''
    for record in records:
        compressor = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS + 16)
        out += compressor.compress(record['bytes'])
        out += compressor.flush()
    return out


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def fixture_inner_truncated():
    """Outer WARC boundary intact; the HTTP message inside is cut short."""
    response_id = '<urn:uuid:inner-cl-response>'
    cl_block = http_response(
        '200 OK',
        [('Content-Type', 'text/plain'), ('Content-Length', '100')],
        body=b'only sixty bytes are stored here 0123456789abcdefghijklm')
    cl_record = warc_record('response', 'http://example.com/cl', cl_block,
                            record_id=response_id)

    chunked_block = http_response(
        '200 OK',
        [('Transfer-Encoding', 'chunked')],
        body=b'4\r\nabcd\r\n0a\r\n12345')  # chunk says 10, only 5 present
    chunked_record = warc_record('response', 'http://example.com/chunked',
                                 chunked_block,
                                 record_id='<urn:uuid:inner-chunk-response>')

    good_block = http_response(
        '200 OK', [('Content-Length', '5')], body=b'hello')
    good_record = warc_record('response', 'http://example.com/good',
                              good_block,
                              record_id='<urn:uuid:inner-good-response>')
    return build_warc([cl_record, chunked_record, good_record])


def fixture_head():
    """HEAD request/response pair: CL is inherited, body is intentionally empty."""
    response_id = '<urn:uuid:head-response>'
    response_block = http_response(
        '200 OK',
        [('Content-Type', 'text/plain'), ('Content-Length', '55')],
        body=b'')
    response = warc_record('response', 'http://example.com/', response_block,
                           record_id=response_id)

    request_block = http_request('HEAD', '/', [('Host', 'example.com')])
    request = warc_record(
        'request', 'http://example.com/', request_block,
        extra_headers=[('WARC-Concurrent-To', response_id)],
        record_id='<urn:uuid:head-request>')
    return build_warc([response, request])


def fixture_nobody_status():
    """Body-less status codes (204, 304) with zero body octets."""
    no_content = warc_record(
        'response', 'http://example.com/no-content',
        http_response('204 No Content', [('Cache-Control', 'no-store')]),
        record_id='<urn:uuid:status-204>')
    not_modified = warc_record(
        'response', 'http://example.com/not-modified',
        http_response('304 Not Modified',
                      [('ETag', '"v1"'), ('Content-Length', '128')]),
        record_id='<urn:uuid:status-304>')
    return build_warc([no_content, not_modified])


def fixture_transfer_interrupted():
    """Three inner interruption shapes, all behind a sound WARC boundary."""
    # 1) chunked stream ends without the terminal 0-length chunk.
    unterminated = http_response(
        '200 OK', [('Transfer-Encoding', 'chunked')],
        body=b'4\r\nabcd\r\n5\r\nedcba\r\n')
    rec_unterminated = warc_record(
        'response', 'http://example.com/chunk-no-terminal', unterminated,
        record_id='<urn:uuid:interrupt-chunk>')

    # 2) a chunk header promises more octets than the block holds.
    short_chunk = http_response(
        '200 OK', [('Transfer-Encoding', 'chunked')],
        body=b'14\r\neight!!')  # declares 20, stores 8 then nothing
    rec_short_chunk = warc_record(
        'response', 'http://example.com/chunk-short', short_chunk,
        record_id='<urn:uuid:interrupt-short-chunk>')

    # 3) HTTP framing is fine (CL matches stored bytes) but the gzip
    #    content-encoding stream itself is truncated -> payload layer fails.
    #    Use incompressible data so the gzip member is long enough to cut.
    state = 0x12345678
    randomish = bytearray()
    for _ in range(4096):
        state = (state * 1103515245 + 12345) & 0x7fffffff
        randomish.append(state & 0xff)
    full_gzip = gzip.compress(bytes(randomish))
    broken_gzip = full_gzip[:len(full_gzip) // 2]
    encoded = http_response(
        '200 OK',
        [('Content-Encoding', 'gzip'),
         ('Content-Length', str(len(broken_gzip)))],
        body=broken_gzip)
    rec_encoded = warc_record(
        'response', 'http://example.com/gzip-cut', encoded,
        record_id='<urn:uuid:interrupt-gzip>')
    return build_warc([rec_unterminated, rec_short_chunk, rec_encoded])


def fixture_conflicting_lengths():
    """Contradictory length declarations -> inconclusive, never guessed."""
    both = http_response(
        '200 OK',
        [('Content-Length', '4'), ('Transfer-Encoding', 'chunked')],
        body=b'4\r\nwiki\r\n0\r\n\r\n')
    rec_both = warc_record('response', 'http://example.com/both', both,
                           record_id='<urn:uuid:conflict-both>')

    duplicate = http_response(
        '200 OK',
        [('Content-Length', '5'), ('Content-Length', '9')],
        body=b'hello')
    rec_duplicate = warc_record('response', 'http://example.com/dup-cl',
                                duplicate,
                                record_id='<urn:uuid:conflict-dup>')

    invalid = http_response(
        '200 OK', [('Content-Length', 'not-a-number')], body=b'hello')
    rec_invalid = warc_record('response', 'http://example.com/bad-cl',
                              invalid,
                              record_id='<urn:uuid:conflict-invalid>')

    # 204 promises no body but block carries octets.
    body_on_204 = http_response('204 No Content', [], body=b'surprise')
    rec_204 = warc_record('response', 'http://example.com/204-body',
                          body_on_204, record_id='<urn:uuid:conflict-204>')
    return build_warc([rec_both, rec_duplicate, rec_invalid, rec_204])


def fixture_close_delimited():
    """No CL and no chunked TE: completeness cannot be proven offline."""
    block = http_response('200 OK', [('Connection', 'close')],
                          body=b'some body bytes with no length declaration')
    record = warc_record('response', 'http://example.com/close', block,
                         record_id='<urn:uuid:close-delimited>')
    return build_warc([record])


def fixture_outer_truncated():
    """The WARC boundary itself lies: declared block exceeds the file."""
    good = warc_record(
        'warcinfo', None, b'software: test\r\nformat: WARC File Format 1.0\r\n',
        record_id='<urn:uuid:outer-good>')
    block = http_response('200 OK', [('Content-Length', '20')],
                          body=b'twenty bytes only!!')
    broken = warc_record('response', 'http://example.com/outer-cut', block,
                         record_id='<urn:uuid:outer-cut>',
                         declared_length=len(block) + 120)
    return build_warc([good, broken])


def fixture_gzip_member_truncated():
    """A sound first member, then a gzip member cut mid deflate stream."""
    good = warc_record(
        'warcinfo', None, b'software: test\r\n',
        record_id='<urn:uuid:gz-good>')
    second = warc_record(
        'response', 'http://example.com/gz-cut',
        http_response('200 OK', [('Content-Length', '10')], body=b'0123456789'),
        record_id='<urn:uuid:gz-cut>')
    out = build_warc_gz([good])
    out += build_warc_gz([second])[:-12]  # chop the member tail
    return out


def fixture_complete_control():
    """A fully valid mixed archive used as the positive control."""
    body = b'<html><body>complete control body</body></html>'
    response = warc_record(
        'response', 'http://example.com/control',
        http_response('200 OK',
                      [('Content-Type', 'text/html'),
                       ('Content-Length', str(len(body)))],
                      body=body),
        record_id='<urn:uuid:control-response>')
    request = warc_record(
        'request', 'http://example.com/control',
        http_request('GET', '/', [('Host', 'example.com')]),
        record_id='<urn:uuid:control-request>')
    return build_warc([response, request])


FIXTURES = {
    'fixture-inner-truncated.warc': fixture_inner_truncated,
    'fixture-head.warc': fixture_head,
    'fixture-nobody-status.warc': fixture_nobody_status,
    'fixture-transfer-interrupted.warc': fixture_transfer_interrupted,
    'fixture-conflicting-lengths.warc': fixture_conflicting_lengths,
    'fixture-close-delimited.warc': fixture_close_delimited,
    'fixture-outer-truncated.warc': fixture_outer_truncated,
    'fixture-gzip-member-truncated.warc.gz': fixture_gzip_member_truncated,
    'fixture-complete-control.warc': fixture_complete_control,
}


def write_fixtures(directory=FIXTURE_DIR):
    os.makedirs(directory, exist_ok=True)
    paths = []
    for name, builder in FIXTURES.items():
        path = os.path.join(directory, name)
        with open(path, 'wb') as handle:
            handle.write(builder())
        paths.append(path)
    return paths


if __name__ == '__main__':
    for path in write_fixtures():
        print(path)
