"""
Offline, layered integrity checking for WARC archives.

Reading a record successfully only proves that the outer WARC framing could be
walked.  It says nothing about whether the HTTP message stored inside that
block is itself complete.  This module checks three layers independently and
never lets an outer layer vouch for an inner one:

1. ``warc``    -- WARC record boundary: declared ``Content-Length`` vs the
                  block octets actually available, the ``CRLF CRLF`` record
                  separator, the next ``WARC/`` magic, and (for record-per-
                  member gzip) the gzip member CRC32/ISIZE trailer.
2. ``http``    -- HTTP message framing: ``Content-Length`` octets vs the body
                  octets actually present, or strict ``Transfer-Encoding:
                  chunked`` delimiters.  HEAD responses, body-less status
                  codes (1xx/204/304), interrupted transfers and contradictory
                  length declarations are classified separately.
3. ``payload`` -- the decoded body: de-chunked/decoded octet counts, clean
                  end of any ``Content-Encoding`` stream, and an optional
                  cross-check against the WARC payload digest.

Every length is labelled with the layer whose octets it counts:

- WARC ``Content-Length`` counts *record block* octets (HTTP headers + body).
- HTTP ``Content-Length`` counts *wire body* octets (still content-encoded,
  after any transfer coding such as chunked has been removed).
- The decoded payload length counts octets after content decoding
  (gzip/deflate/brotli), which is what ``WARC-Payload-Digest`` covers.

Verdicts are ``complete``, ``incomplete`` (a delimiter or byte count proves
that octets are missing or a stream was interrupted) and ``inconclusive``
(the archive offers no trustworthy inner length to check against, or the
length declarations contradict one another).

The input file is only ever opened read-only; nothing here writes to it.
"""

import json
import zlib
from io import BytesIO

from warcio.digestverifyingreader import _compare_digest_rfc_3548
from warcio.limitreader import LimitReader
from warcio.statusandheaders import StatusAndHeadersParser
from warcio.utils import Digester, fsspec_open, to_native_str


VERDICT_COMPLETE = 'complete'
VERDICT_INCOMPLETE = 'incomplete'
VERDICT_INCONCLUSIVE = 'inconclusive'
VERDICT_NOT_APPLICABLE = 'not-applicable'

_VERDICT_RANK = {
    VERDICT_NOT_APPLICABLE: 0,
    VERDICT_COMPLETE: 1,
    VERDICT_INCONCLUSIVE: 2,
    VERDICT_INCOMPLETE: 3,
}

HTTP_VERBS = ('GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'TRACE',
              'OPTIONS', 'CONNECT', 'PATCH')


# ============================================================================
def worst_verdict(verdicts):
    verdict = VERDICT_COMPLETE
    for candidate in verdicts:
        if _VERDICT_RANK[candidate] > _VERDICT_RANK[verdict]:
            verdict = candidate
    return verdict


def _read_exact(stream, length):
    """Read exactly ``length`` octets, returning the data and any shortfall."""
    chunks = []
    remaining = length
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b''.join(chunks)
    return data, length - len(data)


def _parse_header_lines(lines):
    """Parse raw ``Name: Value`` header lines, preserving duplicates/order."""
    headers = []
    for line in lines:
        text = to_native_str(line, 'iso-8859-1').rstrip('\r\n')
        if ':' not in text:
            headers.append((text, None))
            continue
        name, value = text.split(':', 1)
        headers.append((name.strip(), value.strip()))
    return headers


def _get_headers(headers, name):
    name = name.lower()
    return [value for header_name, value in headers
            if header_name.lower() == name and value is not None]


# ============================================================================
class LayerResult(object):
    """Findings for one integrity layer of one record."""

    def __init__(self, name, verdict=VERDICT_NOT_APPLICABLE):
        self.name = name
        self.verdict = verdict
        self.metrics = {}
        self.reasons = []

    def fail(self, verdict, reason, **metrics):
        if _VERDICT_RANK[verdict] > _VERDICT_RANK[self.verdict]:
            self.verdict = verdict
        self.reasons.append(reason)
        self.metrics.update(metrics)

    def note(self, reason, **metrics):
        self.reasons.append(reason)
        self.metrics.update(metrics)

    def complete(self, **metrics):
        if self.verdict == VERDICT_NOT_APPLICABLE:
            self.verdict = VERDICT_COMPLETE
        self.metrics.update(metrics)

    def to_dict(self):
        return {
            'layer': self.name,
            'verdict': self.verdict,
            'metrics': self.metrics,
            'reasons': list(self.reasons),
        }


# ============================================================================
class RecordReport(object):
    """Traceable, per-record integrity verdict."""

    def __init__(self, file_name, record_index, offset):
        self.file_name = file_name
        self.record_index = record_index
        self.offset = offset
        self.warc_type = None
        self.warc_record_id = None
        self.target_uri = None
        self.warc_headers = []
        self.layers = {
            'warc': LayerResult('warc'),
            'http': LayerResult('http'),
            'payload': LayerResult('payload'),
        }
        self.evidence = []

    @property
    def verdict(self):
        return worst_verdict(layer.verdict for layer in self.layers.values())

    def add_evidence(self, message):
        self.evidence.append(message)

    def to_dict(self):
        return {
            'file': self.file_name,
            'record_index': self.record_index,
            'offset': self.offset,
            'warc_type': self.warc_type,
            'warc_record_id': self.warc_record_id,
            'target_uri': self.target_uri,
            'verdict': self.verdict,
            'layers': {name: layer.to_dict()
                       for name, layer in self.layers.items()},
            'evidence': list(self.evidence),
        }


# ============================================================================
class _GzipMemberReader(object):
    """Stream record-per-member gzip archives one member at a time.

    Only the current member's decompressed bytes are buffered, so memory use
    stays bounded by the largest single record rather than the whole file.
    Each yielded tuple is ``(offset, data, meta, error)``; when ``error`` is
    set the other fields are ``None`` and iteration stops.
    """

    CHUNK_SIZE = 65536

    def __init__(self, stream):
        self.stream = stream

    def members(self):
        while True:
            try:
                member_start = self.stream.tell()
            except (OSError, AttributeError):
                member_start = None
            offset, data, meta, error = self._read_one(member_start)
            if error == _EOF:
                return
            yield offset, data, meta, error
            if error is not None:
                return

    def _read_exact(self, length):
        chunks = []
        remaining = length
        while remaining:
            chunk = self.stream.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    def _read_one(self, member_start):
        header = self._read_exact(10)
        if not header:
            return member_start, None, None, _EOF
        if header[:2] != b'\x1f\x8b':
            return member_start, None, None, 'invalid gzip magic bytes'
        if header[2] != 8:
            return member_start, None, None, \
                'unsupported gzip compression method'

        flags = header[3]
        try:
            if flags & 4:  # FEXTRA
                xlen_bytes = self._read_exact(2)
                if len(xlen_bytes) < 2:
                    raise _TruncatedGzipHeader()
                xlen = int.from_bytes(xlen_bytes, 'little')
                if len(self._read_exact(xlen)) < xlen:
                    raise _TruncatedGzipHeader()
            if flags & 8:  # FNAME
                self._read_until_nul()
            if flags & 16:  # FCOMMENT
                self._read_until_nul()
            if flags & 2:  # FHCRC
                if len(self._read_exact(2)) < 2:
                    raise _TruncatedGzipHeader()
        except _TruncatedGzipHeader:
            return member_start, None, None, \
                'truncated gzip optional header section'

        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        output = []
        running_crc = 0
        inflated_len = 0
        pending = b''

        while True:
            chunk = self.stream.read(self.CHUNK_SIZE)
            if not chunk:
                return member_start, None, None, (
                    'gzip member deflate stream is truncated '
                    '(no end-of-block marker)')
            try:
                inflated = decompressor.decompress(pending + chunk)
            except zlib.error as exc:
                return member_start, None, None, \
                    'gzip deflate stream error: {0}'.format(exc)

            output.append(inflated)
            running_crc = zlib.crc32(inflated, running_crc)
            inflated_len += len(inflated)

            if decompressor.eof:
                pending = decompressor.unused_data
                break
            pending = b''

        trailer = pending
        while len(trailer) < 8:
            more = self.stream.read(8 - len(trailer))
            if not more:
                return member_start, None, None, \
                    'truncated gzip CRC/ISIZE trailer'
            trailer += more

        expected_crc = int.from_bytes(trailer[:4], 'little')
        expected_size = int.from_bytes(trailer[4:8], 'little')
        actual_crc = running_crc & 0xffffffff
        actual_size = inflated_len & 0xffffffff

        # Any bytes beyond the trailer belong to the next member; push them
        # back onto the stream so the outer loop starts at the next magic.
        extra = trailer[8:]
        if extra:
            self.stream.seek(-len(extra), 1)

        if actual_crc != expected_crc or actual_size != expected_size:
            return member_start, None, None, (
                'gzip trailer mismatch (crc {0:#010x}!={1:#010x} or '
                'isize {2}!={3})').format(actual_crc, expected_crc,
                                          actual_size, expected_size)

        data = b''.join(output)
        compressed_bytes = None
        if member_start is not None:
            try:
                compressed_bytes = self.stream.tell() - member_start
            except (OSError, AttributeError):
                compressed_bytes = None
        meta = {
            'compressed_bytes': compressed_bytes,
            'decompressed_bytes': inflated_len,
        }
        return member_start, data, meta, None

    def _read_until_nul(self):
        while True:
            byte = self.stream.read(1)
            if not byte:
                raise _TruncatedGzipHeader()
            if byte == b'\x00':
                return


class _TruncatedGzipHeader(Exception):
    pass


_EOF = '__eof__'


# ============================================================================
class IntegrityChecker(object):
    """Check WARC files without modifying them."""

    GZIP_MAGIC = b'\x1f\x8b'

    def __init__(self):
        self.http_parser = StatusAndHeadersParser(
            ['HTTP/1.1', 'HTTP/1.0'], verify=False)
        self.http_request_parser = StatusAndHeadersParser(HTTP_VERBS,
                                                          verify=False)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def check_file(self, file_name, stream):
        """Return ``(reports, file_error)`` for an open read-only stream."""
        signature = stream.read(5)
        try:
            stream.seek(0)
        except (OSError, AttributeError):
            buffered = BytesIO(signature + stream.read())
            stream = buffered

        if signature[:2] == self.GZIP_MAGIC:
            reports, file_error = self._check_gzip_stream(file_name, stream)
        elif signature.startswith(b'WARC/'):
            reports, file_error = self._check_plain_warc(file_name, stream)
        else:
            report = RecordReport(file_name, 0, 0)
            report.warc_type = '(file)'
            report.layers['warc'].fail(
                VERDICT_INCONCLUSIVE,
                'not a WARC file (no WARC/ magic and not gzip); ARC and '
                'other formats are not checked by this tool')
            reports = [report]
            file_error = None

        self._refine_head_responses(reports)
        return reports, file_error

    # ------------------------------------------------------------------
    # Layer 1, gzip path: record-per-member WARC.gz envelope
    # ------------------------------------------------------------------
    def _check_gzip_stream(self, file_name, stream):
        reports = []
        file_error = None
        record_index = 0

        for member_start, inflated, meta, error in \
                _GzipMemberReader(stream).members():
            if error is not None:
                report = RecordReport(file_name, record_index, member_start)
                report.warc_type = '(gzip-member)'
                report.layers['warc'].fail(VERDICT_INCOMPLETE, error)
                report.add_evidence(
                    'warc: gzip member at compressed offset {0}: {1}'.format(
                        member_start, error))
                reports.append(report)
                file_error = error
                break

            reports.append(self._check_member_bytes(
                file_name, record_index, member_start, inflated, meta))
            record_index += 1

        return reports, file_error

    # ------------------------------------------------------------------
    # Layer 1, plain path: WARC record framing
    # ------------------------------------------------------------------
    def _check_plain_warc(self, file_name, stream):
        reports = []
        file_error = None
        record_index = 0

        while True:
            offset = stream.tell()
            magic = stream.readline()
            if not magic:
                break
            if not magic.startswith(b'WARC/'):
                report = RecordReport(file_name, record_index, offset)
                report.warc_type = '(file)'
                reason = ('expected WARC/ record magic at offset {0}, '
                          'found: {1!r}').format(offset, magic[:40])
                report.layers['warc'].fail(VERDICT_INCOMPLETE, reason)
                report.add_evidence('warc: ' + reason)
                reports.append(report)
                file_error = reason
                break

            report = RecordReport(file_name, record_index, offset)
            header_lines = self._read_warc_header_lines(stream, report)
            if header_lines is None:
                reports.append(report)
                file_error = 'truncated WARC header block at offset {0}'.format(
                    offset)
                break

            report.warc_headers = _parse_header_lines(header_lines)
            self._populate_identity(report)

            block_stream, block_len, framing_error = self._open_block(
                stream, report)
            self._check_block(block_stream, block_len, report)
            reports.append(report)
            record_index += 1

            if framing_error is not None:
                file_error = framing_error
                break

            boundary_error = self._check_boundary(stream, report)
            if boundary_error is not None:
                file_error = boundary_error
                break

        return reports, file_error

    def _check_member_bytes(self, file_name, record_index, member_offset,
                            member_bytes, gzip_meta):
        stream = BytesIO(member_bytes)
        magic = stream.readline()
        report = RecordReport(file_name, record_index, member_offset)

        if not magic.startswith(b'WARC/'):
            reason = 'gzip member does not start with a WARC/ record magic'
            report.layers['warc'].fail(VERDICT_INCOMPLETE, reason)
            report.add_evidence('warc: ' + reason)
            return report

        header_lines = self._read_warc_header_lines(stream, report)
        if header_lines is None:
            report.layers['warc'].fail(
                VERDICT_INCOMPLETE,
                'gzip member ends before the WARC header block is complete')
            return report

        report.warc_headers = _parse_header_lines(header_lines)
        self._populate_identity(report)
        envelope_metrics = {
            'gzip_decompressed_bytes': gzip_meta['decompressed_bytes']}
        if gzip_meta['compressed_bytes'] is not None:
            envelope_metrics['gzip_compressed_bytes'] = \
                gzip_meta['compressed_bytes']
        report.layers['warc'].note(
            'gzip member envelope intact (crc32/isize verified)',
            **envelope_metrics)

        block_stream, block_len, framing_error = self._open_block(
            stream, report)
        self._check_block(block_stream, block_len, report)
        if framing_error is not None:
            return report

        remainder = member_bytes[stream.tell():]
        if remainder.strip(b'\r\n'):
            reason = ('non-blank trailing octets inside gzip member after '
                      'the record block')
            report.layers['warc'].fail(VERDICT_INCONCLUSIVE, reason)
            report.add_evidence('warc: ' + reason)
        elif remainder:
            report.layers['warc'].note(
                'record separator present', separator_bytes=len(remainder))

        return report

    def _read_warc_header_lines(self, stream, report):
        lines = []
        while True:
            line = stream.readline()
            if not line:
                reason = 'stream ended in the middle of WARC headers'
                report.layers['warc'].fail(VERDICT_INCOMPLETE, reason)
                report.add_evidence('warc: ' + reason)
                return None
            if line in (b'\r\n', b'\n'):
                return lines
            lines.append(line)

    def _populate_identity(self, report):
        report.warc_type = (_get_headers(report.warc_headers, 'WARC-Type')
                            or [None])[0]
        report.warc_record_id = (
            _get_headers(report.warc_headers, 'WARC-Record-ID') or [None])[0]
        report.target_uri = (
            _get_headers(report.warc_headers, 'WARC-Target-URI') or [None])[0]

    def _open_block(self, stream, report):
        """Bound the block by the declared WARC Content-Length.

        Returns a rewindable stream over whatever block octets are actually
        available, plus a fatal framing error when the block was short.
        """
        declared_values = _get_headers(report.warc_headers, 'Content-Length')
        if len(declared_values) != 1:
            reason = ('WARC Content-Length is {0}; record boundary cannot '
                      'be located reliably').format(
                          'missing' if not declared_values
                          else 'declared {0} times'.format(
                              len(declared_values)))
            report.layers['warc'].fail(VERDICT_INCONCLUSIVE, reason)
            report.add_evidence('warc: ' + reason)
            return BytesIO(b''), 0, reason

        try:
            declared_length = int(declared_values[0])
            if declared_length < 0:
                raise ValueError()
        except ValueError:
            reason = ('WARC Content-Length is not a non-negative integer: '
                      '{0!r}').format(declared_values[0])
            report.layers['warc'].fail(VERDICT_INCONCLUSIVE, reason)
            report.add_evidence('warc: ' + reason)
            return BytesIO(b''), 0, reason

        limited = LimitReader(stream, declared_length)
        actual_block = self._drain(limited)
        actual_length = len(actual_block)
        block_stream = BytesIO(actual_block)

        report.layers['warc'].metrics.update({
            'declared_block_length': declared_length,
            'actual_block_bytes': actual_length,
        })

        if actual_length < declared_length:
            reason = ('record block truncated: WARC Content-Length declares '
                      '{0} block octets, only {1} available, {2} missing').format(
                          declared_length, actual_length,
                          declared_length - actual_length)
            report.layers['warc'].fail(VERDICT_INCOMPLETE, reason)
            report.add_evidence('warc: ' + reason)
            return block_stream, actual_length, reason

        report.layers['warc'].complete()
        report.add_evidence(
            'warc: block boundary holds -- declared {0} block octets, '
            '{0} octets present'.format(declared_length))
        return block_stream, actual_length, None

    def _check_boundary(self, stream, report):
        """Validate the CRLF CRLF separator, resynchronizing if misaligned.

        A declared Content-Length that is too short leaves body octets before
        the separator, so a non-blank line here means the boundary cannot be
        trusted.  Rather than abandoning the rest of the file, scan forward
        to the next WARC record magic and resume there.
        """
        separator_bytes = 0
        while True:
            line = stream.readline()
            if not line:
                if separator_bytes:
                    report.layers['warc'].note(
                        'record separator present (end of file)',
                        separator_bytes=separator_bytes)
                return None
            if line in (b'\r\n', b'\n'):
                separator_bytes += len(line)
                continue

            if line.startswith(b'WARC/') and separator_bytes:
                stream.seek(-len(line), 1)
                report.layers['warc'].note(
                    'record separator present',
                    separator_bytes=separator_bytes)
                return None

            skipped = len(line)
            while True:
                candidate = stream.readline()
                if not candidate:
                    reason = ('record boundary is misaligned and no further '
                              'WARC record magic could be found')
                    report.layers['warc'].fail(VERDICT_INCONCLUSIVE, reason)
                    report.add_evidence('warc: ' + reason)
                    return None
                if candidate.startswith(b'WARC/'):
                    stream.seek(-len(candidate), 1)
                    reason = ('declared WARC Content-Length ends before the '
                              'real record boundary; resynchronized after '
                              '{0} unexpected octet(s) to the next WARC '
                              'magic').format(skipped)
                    report.layers['warc'].fail(VERDICT_INCONCLUSIVE, reason)
                    report.add_evidence('warc: ' + reason)
                    return None
                skipped += len(candidate)

    # ------------------------------------------------------------------
    # Block contents: WARC digests, then HTTP layers
    # ------------------------------------------------------------------
    def _check_block(self, block_stream, block_len, report):
        self._verify_warc_block_digest(block_stream, report)
        block_stream.seek(0)

        truncation = (_get_headers(report.warc_headers, 'WARC-Truncated')
                      or [None])[0]
        if truncation:
            reason = ('WARC-Truncated declares capture-time truncation: '
                      '{0}').format(truncation)
            report.layers['warc'].fail(VERDICT_INCOMPLETE, reason)
            report.add_evidence('warc: ' + reason)

        if report.warc_type not in ('response', 'request', 'revisit'):
            report.add_evidence(
                'http: no HTTP message in a {0} record; the block is opaque '
                'payload, fully defined by the WARC boundary'.format(
                    report.warc_type))
            return

        if not report.target_uri or not report.target_uri.startswith(
                ('http:', 'https:')):
            report.add_evidence(
                'http: non-HTTP target URI; inner HTTP check skipped')
            return

        self._check_http_message(block_stream, block_len, report)

    def _verify_warc_block_digest(self, block_stream, report):
        block_digest = (_get_headers(report.warc_headers, 'WARC-Block-Digest')
                        or [None])[0]
        if not block_digest:
            return
        # A revisit record's digests identify the referenced original
        # capture rather than the (header-only) block stored here, so they
        # cannot be recomputed from this record's octets.
        if report.warc_type == 'revisit':
            report.layers['warc'].note(
                'WARC-Block-Digest not checked on a revisit record; it '
                'identifies the referenced original capture')
            return
        try:
            algorithm = block_digest.partition(':')[0]
            digester = Digester(algorithm)
        except ValueError:
            report.layers['warc'].note(
                'WARC-Block-Digest uses an unknown algorithm: {0}'.format(
                    block_digest))
            return

        block_stream.seek(0)
        for chunk in iter(lambda: block_stream.read(65536), b''):
            digester.update(chunk)

        match = _compare_digest_rfc_3548(digester, block_digest)
        report.layers['warc'].metrics['block_digest_match'] = bool(match)
        if match is False:
            reason = 'WARC-Block-Digest mismatch: header {0}, computed {1}'.format(
                block_digest, digester)
            report.layers['warc'].fail(VERDICT_INCOMPLETE, reason)
            report.add_evidence('warc: ' + reason)
        elif match is True:
            report.add_evidence(
                'warc: WARC-Block-Digest corroborates the block octets')

    def _verify_stored_body_digest(self, stored_body, report):
        payload_digest = (
            _get_headers(report.warc_headers, 'WARC-Payload-Digest')
            or [None])[0]
        if not payload_digest:
            return
        # A revisit record stores no body; its payload digest identifies the
        # referenced original capture and is not recomputable from this block.
        if report.warc_type == 'revisit':
            report.layers['http'].note(
                'WARC-Payload-Digest not checked on a revisit record; it '
                'identifies the referenced original capture')
            return
        try:
            algorithm = payload_digest.partition(':')[0]
            digester = Digester(algorithm)
        except ValueError:
            report.layers['http'].note(
                'WARC-Payload-Digest uses an unknown algorithm: {0}'.format(
                    payload_digest))
            return
        digester.update(stored_body)
        match = _compare_digest_rfc_3548(digester, payload_digest)
        layer = report.layers['http']
        layer.metrics['stored_body_digest_match'] = bool(match)
        layer.metrics['stored_body_digest_header'] = payload_digest
        if match is False:
            reason = ('WARC-Payload-Digest mismatch over the {0} stored '
                      'wire body octets: header {1}, computed {2}').format(
                          len(stored_body), payload_digest, digester)
            layer.fail(VERDICT_INCOMPLETE, reason)
            report.add_evidence('http: ' + reason)
        elif match is True:
            report.add_evidence(
                'http: WARC-Payload-Digest corroborates the {0} stored wire '
                'body octets (pre transfer/content decoding)'.format(
                    len(stored_body)))

    def _check_http_message(self, block_stream, block_len, report):
        parser = (self.http_request_parser if report.warc_type == 'request'
                  else self.http_parser)
        try:
            http_headers = parser.parse(block_stream)
        except EOFError:
            reason = ('HTTP header block is truncated: not even a complete '
                      'status/request line is present among the {0} block '
                      'octets').format(block_len)
            report.layers['http'].fail(VERDICT_INCOMPLETE, reason)
            report.add_evidence('http: ' + reason)
            return

        header_bytes = http_headers.total_len
        if header_bytes <= 0:
            reason = 'no parseable HTTP status/request line in block'
            report.layers['http'].fail(VERDICT_INCONCLUSIVE, reason)
            report.add_evidence('http: ' + reason)
            return

        body_budget = block_len - header_bytes
        if body_budget < 0:
            reason = ('HTTP headers claim {0} octets but the WARC block only '
                      'holds {1} octets').format(header_bytes, block_len)
            report.layers['http'].fail(VERDICT_INCOMPLETE, reason)
            report.add_evidence('http: ' + reason)
            return

        # The stored body is every block octet after the HTTP header block,
        # still carrying any transfer coding (chunk markers) and content
        # coding (gzip/deflate/br).  This -- not the decoded body -- is what
        # a WARC-Payload-Digest is computed over in practice (and what
        # warcio verifies), so check it here, before any decoding.
        stored_body = block_stream.read()
        if len(stored_body) != body_budget:
            body_budget = len(stored_body)
        self._verify_stored_body_digest(stored_body, report)
        body_stream = BytesIO(stored_body)

        status_code = None
        if report.warc_type in ('response', 'revisit'):
            status_code = http_headers.get_statuscode()

        report.layers['http'].metrics.update({
            'http_header_bytes': header_bytes,
            'body_octets_available_in_block': body_budget,
            'status_code': status_code,
            'method': http_headers.protocol
            if report.warc_type == 'request' else None,
        })

        # A revisit record stores HTTP headers only; there is no stored body
        # to length-check, regardless of any inherited Content-Length.
        if report.warc_type == 'revisit' and body_budget == 0:
            report.layers['http'].complete(framing='revisit-headers-only')
            report.add_evidence(
                'http: revisit record carries HTTP headers only; no body '
                'octets are expected')
            return

        self._check_http_framing(body_stream, body_budget,
                                 http_headers.headers, status_code, report)

    def _check_http_framing(self, block_stream, body_budget, headers,
                            status_code, report):
        content_lengths = _get_headers(headers, 'Content-Length')
        transfer_encodings = _get_headers(headers, 'Transfer-Encoding')
        is_chunked = any(
            value.lower().split(',')[-1].strip() == 'chunked'
            for value in transfer_encodings)
        unsupported_te = [value for value in transfer_encodings
                          if 'chunked' not in value.lower()]

        body_less = bool(status_code and (
            status_code in ('204', '304') or status_code.startswith('1')))

        if body_less:
            layer = report.layers['http']
            layer.complete(framing='no-body-status-{0}'.format(status_code))
            if body_budget:
                reason = ('status {0} must not carry a body but the block '
                          'contains {1} body octets').format(
                              status_code, body_budget)
                layer.fail(VERDICT_INCONCLUSIVE, reason)
                report.add_evidence('http: ' + reason)
            else:
                report.add_evidence(
                    'http: status {0} defines no message body; zero body '
                    'octets is correct'.format(status_code))
            self._check_payload(block_stream, body_budget, headers, report,
                                VERDICT_COMPLETE)
            return

        if report.warc_type == 'request' and not content_lengths and \
                not is_chunked and body_budget == 0:
            report.layers['http'].complete(framing='empty-request')
            report.add_evidence(
                'http: request has no body and no length declaration')
            return

        if unsupported_te:
            reason = 'unsupported Transfer-Encoding value: {0}'.format(
                ','.join(unsupported_te))
            report.layers['http'].fail(VERDICT_INCONCLUSIVE, reason)
            report.add_evidence('http: ' + reason)
            return

        conflict = self._length_conflicts(content_lengths, is_chunked,
                                          report)

        if is_chunked:
            self._check_chunked_framing(block_stream, body_budget, headers,
                                        report, conflict)
            return

        if len(set(content_lengths)) > 1:
            reason = 'contradictory Content-Length headers: {0}'.format(
                content_lengths)
            report.layers['http'].fail(VERDICT_INCONCLUSIVE, reason,
                                       framing='conflicting-content-length')
            report.add_evidence('http: ' + reason)
            return

        if content_lengths:
            self._check_content_length(block_stream, body_budget,
                                       content_lengths[0], headers, report,
                                       conflict)
            return

        reason = ('no Content-Length and no chunked Transfer-Encoding; an '
                  'HTTP/1.x body of this kind is delimited by connection '
                  'close, and the WARC block end cannot vouch for it')
        report.layers['http'].fail(VERDICT_INCONCLUSIVE, reason,
                                   framing='close-delimited')
        report.layers['http'].metrics['body_octets_observed'] = body_budget
        report.add_evidence(
            'http: {0}; {1} body octets observed but their completeness is '
            'unprovable'.format(reason, body_budget))
        self._check_payload(block_stream, body_budget, headers, report,
                            VERDICT_INCONCLUSIVE)

    def _length_conflicts(self, content_lengths, is_chunked, report):
        if not (is_chunked and content_lengths):
            return False
        reason = ('both Content-Length ({0}) and Transfer-Encoding: chunked '
                  'declared; RFC 7230 forbids this, chunked framing takes '
                  'precedence').format(content_lengths)
        report.layers['http'].fail(VERDICT_INCONCLUSIVE, reason,
                                   framing='conflicting-transfer-length')
        report.add_evidence('http: ' + reason)
        return True

    def _check_content_length(self, block_stream, body_budget, raw_value,
                              headers, report, conflict):
        layer = report.layers['http']
        try:
            declared = int(raw_value)
            if declared < 0:
                raise ValueError()
        except ValueError:
            reason = ('Content-Length is not a non-negative integer: '
                      '{0!r}').format(raw_value)
            layer.fail(VERDICT_INCONCLUSIVE, reason,
                       framing='invalid-content-length')
            report.add_evidence('http: ' + reason)
            return

        layer.metrics.update({
            'framing': 'content-length' if not conflict
                       else 'conflicting-transfer-length',
            'content_length_declared': declared,
            'wire_body_bytes_actual': body_budget,
        })

        if body_budget < declared:
            reason = ('HTTP body interrupted: Content-Length declares {0} '
                      'wire body octets, only {1} present in the block, '
                      '{2} missing').format(
                          declared, body_budget, declared - body_budget)
            layer.fail(VERDICT_INCOMPLETE, reason)
            report.add_evidence(
                'http: {0}; the WARC record boundary is intact, so the loss '
                'is inside the HTTP message'.format(reason))
            payload_verdict = VERDICT_INCOMPLETE
        elif body_budget > declared:
            reason = ('block contains {0} body octets but Content-Length '
                      'declares {1}; {2} unexplained trailing octets').format(
                          body_budget, declared, body_budget - declared)
            layer.fail(VERDICT_INCONCLUSIVE, reason)
            report.add_evidence('http: ' + reason)
            payload_verdict = VERDICT_INCONCLUSIVE
        else:
            layer.complete()
            report.add_evidence(
                'http: Content-Length {0} matches the {0} wire body octets '
                'in the block'.format(declared))
            payload_verdict = VERDICT_COMPLETE

        self._check_payload(block_stream, min(body_budget, declared),
                            headers, report, payload_verdict)

    def _check_chunked_framing(self, block_stream, body_budget, headers,
                               report, conflict):
        layer = report.layers['http']
        layer.metrics['framing'] = (
            'chunked' if not conflict else 'conflicting-transfer-length')

        decoded_parts = []
        raw_consumed = 0
        chunk_count = 0

        while True:
            size_line = block_stream.readline()
            if not size_line:
                reason = ('chunked transfer interrupted before the terminal '
                          '0-length chunk; consumed {0} chunked octets across '
                          '{1} chunk(s)').format(raw_consumed, chunk_count)
                layer.fail(VERDICT_INCOMPLETE, reason,
                           chunked_raw_bytes=raw_consumed,
                           chunk_count=chunk_count)
                report.add_evidence('http: ' + reason)
                self._finish_chunked_payload(decoded_parts, headers, report,
                                             complete=False)
                return
            raw_consumed += len(size_line)

            if not size_line.endswith(b'\r\n'):
                reason = 'malformed chunk: size line lacks CRLF: {0!r}'.format(
                    size_line[:40])
                layer.fail(VERDICT_INCONCLUSIVE, reason,
                           chunked_raw_bytes=raw_consumed)
                report.add_evidence('http: ' + reason)
                self._finish_chunked_payload(decoded_parts, headers, report,
                                             complete=False)
                return

            token = size_line[:-2].split(b';')[0].strip()
            try:
                chunk_size = int(token, 16)
                if chunk_size < 0 or chunk_size > 2 ** 31:
                    raise ValueError()
            except ValueError:
                reason = 'malformed chunk: invalid hex size token: {0!r}'.format(
                    token[:40])
                layer.fail(VERDICT_INCONCLUSIVE, reason,
                           chunked_raw_bytes=raw_consumed)
                report.add_evidence('http: ' + reason)
                self._finish_chunked_payload(decoded_parts, headers, report,
                                             complete=False)
                return

            if chunk_size == 0:
                trailer_bytes, trailer_ok = self._drain_chunk_trailers(
                    block_stream)
                raw_consumed += trailer_bytes
                if not trailer_ok:
                    reason = ('chunked transfer interrupted after the '
                              'terminal chunk while reading trailers')
                    layer.fail(VERDICT_INCOMPLETE, reason,
                               chunked_raw_bytes=raw_consumed,
                               chunk_count=chunk_count)
                    report.add_evidence('http: ' + reason)
                    self._finish_chunked_payload(decoded_parts, headers,
                                                 report, complete=False)
                    return
                break

            data, missing = _read_exact(block_stream, chunk_size)
            raw_consumed += len(data)
            if missing:
                reason = ('chunked transfer interrupted inside chunk {0}: '
                          'declared {1} octets, {2} present, {3} missing').format(
                              chunk_count, chunk_size, len(data), missing)
                layer.fail(VERDICT_INCOMPLETE, reason,
                           chunked_raw_bytes=raw_consumed,
                           chunk_count=chunk_count)
                report.add_evidence('http: ' + reason)
                decoded_parts.append(data)
                self._finish_chunked_payload(decoded_parts, headers, report,
                                             complete=False)
                return

            terminator = block_stream.read(2)
            raw_consumed += len(terminator)
            if terminator != b'\r\n':
                reason = ('malformed chunk {0}: missing CRLF terminator '
                          '(got {1!r})').format(chunk_count, terminator)
                layer.fail(VERDICT_INCONCLUSIVE, reason,
                           chunked_raw_bytes=raw_consumed)
                report.add_evidence('http: ' + reason)
                decoded_parts.append(data)
                self._finish_chunked_payload(decoded_parts, headers, report,
                                             complete=False)
                return

            decoded_parts.append(data)
            chunk_count += 1

        trailing = body_budget - raw_consumed
        layer.metrics.update({
            'chunked_raw_bytes': raw_consumed,
            'chunked_decoded_bytes': sum(len(part) for part in decoded_parts),
            'chunk_count': chunk_count,
            'wire_body_bytes_actual': body_budget,
        })

        framing_clean = trailing == 0
        if trailing != 0:
            reason = ('{0} octets remain in the block after the chunked '
                      'message terminated').format(trailing)
            layer.fail(VERDICT_INCONCLUSIVE, reason)
            report.add_evidence('http: ' + reason)
        elif not conflict:
            layer.complete()
            report.add_evidence(
                'http: chunked framing complete -- {0} chunk(s), {1} '
                'chunked wire octets, terminal 0-length chunk present'.format(
                    chunk_count, raw_consumed))

        # The payload only depends on the chunk delimiters being intact, not
        # on the contradictory Content-Length, so a conflicting declaration
        # leaves the payload complete while the http layer stays inconclusive.
        self._finish_chunked_payload(decoded_parts, headers, report,
                                     complete=framing_clean)

    def _drain_chunk_trailers(self, block_stream):
        # After the 0-size line (already consumed), read either the blank
        # terminating CRLF or trailer fields followed by a blank CRLF.
        consumed = 0
        while True:
            line = block_stream.readline()
            if not line:
                return consumed, False
            consumed += len(line)
            if line in (b'\r\n', b'\n'):
                return consumed, True

    def _finish_chunked_payload(self, decoded_parts, headers, report,
                                complete):
        decoded = b''.join(decoded_parts)
        verdict = VERDICT_COMPLETE if complete else VERDICT_INCOMPLETE
        self._check_payload(BytesIO(decoded), len(decoded), headers, report,
                            verdict)

    # ------------------------------------------------------------------
    # Layer 3: content decoding and payload digest
    # ------------------------------------------------------------------
    def _check_payload(self, block_stream, encoded_length, headers, report,
                       payload_verdict):
        encoding_values = _get_headers(headers, 'Content-Encoding')
        encoding = encoding_values[-1].lower() if encoding_values else None
        if encoding == 'x-gzip':
            encoding = 'gzip'

        encoded = block_stream.read()
        layer = report.layers['payload']
        layer.metrics['content_encoding'] = encoding
        layer.metrics['encoded_wire_body_bytes'] = len(encoded)

        if not encoding:
            layer.metrics['decoded_payload_bytes'] = len(encoded)
            self._finalize_payload(layer, encoded, report, payload_verdict)
            return

        decoded, error = self._decode_payload(encoding, encoded)
        layer.metrics['decoded_payload_bytes'] = len(decoded)
        if error is not None:
            layer.fail(payload_verdict
                       if payload_verdict == VERDICT_INCONCLUSIVE
                       else VERDICT_INCOMPLETE, error)
            report.add_evidence('payload: ' + error)
            return

        if len(encoded) != encoded_length:
            report.add_evidence(
                'payload: decoder consumed {0} encoded octets; the transfer '
                'layer exposed {1}'.format(len(encoded), encoded_length))
        self._finalize_payload(layer, decoded, report, payload_verdict)

    def _finalize_payload(self, layer, decoded, report, verdict):
        if verdict == VERDICT_COMPLETE:
            layer.complete()
            report.add_evidence(
                'payload: decoded body is {0} octet(s); this length counts '
                'post-decoding bytes, distinct from wire body and WARC block '
                'lengths'.format(len(decoded)))
        else:
            layer.fail(verdict, 'carried over from HTTP framing')

    def _decode_payload(self, encoding, encoded):
        if encoding == 'gzip':
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                decoded = decompressor.decompress(encoded)
            except zlib.error as exc:
                return b'', ('gzip content-encoding stream is corrupt: '
                             '{0}'.format(exc))
            if not decompressor.eof:
                return decoded, ('gzip content-encoding stream is truncated: '
                                 'no end-of-stream marker')
            return decoded, None

        if encoding == 'deflate':
            last_error = None
            for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
                decompressor = zlib.decompressobj(wbits)
                try:
                    decoded = decompressor.decompress(encoded)
                except zlib.error as exc:
                    last_error = exc
                    continue
                if not decompressor.eof:
                    return decoded, ('deflate content-encoding stream is '
                                     'truncated: no end-of-stream marker')
                return decoded, None
            return b'', ('deflate content-encoding stream is truncated or '
                         'corrupt: {0}'.format(last_error))

        if encoding == 'br':
            try:
                import brotlicffi
            except ImportError:
                return b'', ('brotli content-encoding cannot be checked: '
                             'brotlicffi is not installed')
            try:
                return brotlicffi.Decompressor().decompress(encoded), None
            except Exception as exc:
                return b'', ('brotli content-encoding stream is truncated or '
                             'corrupt: {0}'.format(exc))

        return b'', 'unsupported Content-Encoding: {0}'.format(encoding)

    # ------------------------------------------------------------------
    # HEAD responses are judged against the paired request, not the CL
    # ------------------------------------------------------------------
    def _refine_head_responses(self, reports):
        method_by_response_id = {}
        has_requests = False
        for report in reports:
            if report.warc_type != 'request':
                continue
            has_requests = True
            concurrent = (_get_headers(report.warc_headers,
                                       'WARC-Concurrent-To') or [None])[0]
            method = report.layers['http'].metrics.get('method')
            if concurrent and method:
                method_by_response_id[concurrent] = method

        for report in reports:
            if report.warc_type not in ('response', 'revisit'):
                continue
            http_layer = report.layers['http']
            body_bytes = http_layer.metrics.get(
                'wire_body_bytes_actual',
                http_layer.metrics.get('body_octets_available_in_block', 0))
            declared_cl = http_layer.metrics.get('content_length_declared')
            if not (body_bytes == 0 and declared_cl):
                continue

            method = method_by_response_id.get(report.warc_record_id)
            if method == 'HEAD':
                http_layer.verdict = VERDICT_COMPLETE
                http_layer.metrics['framing'] = 'head-response'
                http_layer.reasons = [
                    item for item in http_layer.reasons
                    if 'HTTP body interrupted' not in item]
                http_layer.note(
                    'HEAD response: Content-Length describes the omitted GET '
                    'representation; zero body octets is correct')
                report.add_evidence(
                    'http: paired request (WARC-Concurrent-To) method is '
                    'HEAD; zero body octets is complete')
                report.layers['payload'].verdict = VERDICT_NOT_APPLICABLE
            elif method is None and not has_requests:
                reason = ('zero body octets despite Content-Length {0}; a '
                          'HEAD response would explain this but no paired '
                          'request record is present').format(declared_cl)
                http_layer.verdict = VERDICT_INCONCLUSIVE
                http_layer.reasons = [
                    item for item in http_layer.reasons
                    if 'HTTP body interrupted' not in item]
                http_layer.note(reason)
                report.add_evidence('http: ' + reason)
            # method == 'GET' (or another body-bearing verb): keep the
            # incomplete verdict, the body really was interrupted.

    # ------------------------------------------------------------------
    def _drain(self, stream):
        chunks = []
        for chunk in iter(lambda: stream.read(65536), b''):
            chunks.append(chunk)
        return b''.join(chunks)


# ============================================================================
def check_path(file_name):
    """Convenience wrapper used by the CLI.  Opens strictly read-only."""
    checker = IntegrityChecker()
    with fsspec_open(file_name, 'rb') as stream:
        reports, file_error = checker.check_file(file_name, stream)
    return reports, file_error


# ============================================================================
class IntegrityCheckRunner(object):
    """CLI driver: layered integrity reports with traceable evidence."""

    def __init__(self, cmd):
        self.inputs = cmd.inputs
        self.json_output = getattr(cmd, 'json', False)
        self.only = getattr(cmd, 'only', None)
        self.quiet = getattr(cmd, 'quiet', False)
        self.exit_value = 0
        self._all_reports = []
        self._file_errors = []

    def process_all(self):
        for file_name in self.inputs:
            try:
                reports, file_error = check_path(file_name)
            except Exception as exc:  # unreadable file, bad gzip container...
                self.exit_value = 1
                self._file_errors.append({
                    'file': file_name,
                    'error': '{0}: {1}'.format(type(exc).__name__, exc),
                })
                continue

            self._all_reports.extend(reports)
            if file_error:
                self.exit_value = 1
                self._file_errors.append({
                    'file': file_name,
                    'error': file_error,
                })
            if any(report.verdict == VERDICT_INCOMPLETE for report in reports):
                self.exit_value = 1

        self._emit()
        return self.exit_value

    def _selected(self, reports):
        if not self.only:
            return reports
        return [report for report in reports
                if report.verdict == self.only]

    def _emit(self):
        reports = self._selected(self._all_reports)
        if self.json_output:
            payload = {
                'reports': [report.to_dict() for report in reports],
                'file_errors': list(self._file_errors),
                'summary': self._summary(self._all_reports),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return

        self._emit_text(reports)

    def _emit_text(self, reports):
        current_file = None
        for report in reports:
            if report.file_name != current_file:
                current_file = report.file_name
                print(current_file)
            print(self._format_report(report))

        for error in self._file_errors:
            print(error['file'])
            print('  FILE ERROR: {0}'.format(error['error']))

        if not self.quiet:
            print(self._format_summary(self._all_reports))

    def _format_report(self, report):
        lines = [
            '  #{index} offset {offset} {warc_type} {verdict}'.format(
                index=report.record_index,
                offset=report.offset,
                warc_type=report.warc_type or '?',
                verdict=report.verdict.upper()),
        ]
        if report.warc_record_id:
            lines.append('    WARC-Record-ID: {0}'.format(
                report.warc_record_id))
        if report.target_uri:
            lines.append('    WARC-Target-URI: {0}'.format(
                report.target_uri))

        for name in ('warc', 'http', 'payload'):
            layer = report.layers[name]
            lines.append('    {0:7s} [{1}] {2}'.format(
                name, layer.verdict, self._format_metrics(layer)))
            for reason in layer.reasons:
                lines.append('             - {0}'.format(reason))

        for evidence in report.evidence:
            lines.append('    evidence: {0}'.format(evidence))
        return '\n'.join(lines)

    @staticmethod
    def _format_metrics(layer):
        if not layer.metrics:
            return ''
        return ', '.join('{0}={1}'.format(key, value)
                         for key, value in sorted(layer.metrics.items()))

    def _summary(self, reports):
        summary = {'total_records': len(reports)}
        for verdict in (VERDICT_COMPLETE, VERDICT_INCOMPLETE,
                        VERDICT_INCONCLUSIVE):
            summary[verdict] = sum(1 for report in reports
                                   if report.verdict == verdict)
        return summary

    def _format_summary(self, reports):
        summary = self._summary(reports)
        return ('SUMMARY: {total_records} record(s) -- {complete} complete, '
                '{incomplete} incomplete, {inconclusive} inconclusive').format(
                    **summary)
