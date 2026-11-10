import zlib

from socket import gethostname

from warcio.utils import Digester
from warcio.recordbuilder import RecordBuilder

from warcio.statusandheaders import StatusAndHeadersParser


# ============================================================================
class BaseWARCWriter(RecordBuilder):

    # record types eligible for duplicate payload detection
    DEDUP_RECORD_TYPES = ('response', 'resource')

    def __init__(self, gzip=True, *args, **kwargs):
        super(BaseWARCWriter, self).__init__(warc_version=kwargs.get('warc_version'),
                                             header_filter=kwargs.get('header_filter'),
                                             digest_algorithm=kwargs.get('digest_algorithm', 'sha1'))
        self.gzip = gzip
        self.hostname = gethostname()

        self.parser = StatusAndHeadersParser([], verify=False)

        # optional duplicate payload handling:
        # when enabled, a record whose payload digest matches an already
        # written record is stored as a revisit record instead
        self.dedup = kwargs.get('dedup', False)

        # if True (default), only records for the same WARC-Target-URI are
        # considered duplicates; if False, identical payloads are shared
        # across different URIs as well
        self.dedup_per_url = kwargs.get('dedup_per_url', True)

        # maps dedup key -> (WARC-Target-URI, WARC-Date) of the original
        # record, populated only after the original was written successfully
        self._dedup_index = {}

    def write_request_response_pair(self, req, resp, params=None):
        url = resp.rec_headers.get_header('WARC-Target-URI')
        dt = resp.rec_headers.get_header('WARC-Date')

        req.rec_headers.replace_header('WARC-Target-URI', url)
        req.rec_headers.replace_header('WARC-Date', dt)

        resp_id = resp.rec_headers.get_header('WARC-Record-ID')
        if resp_id:
            req.rec_headers.add_header('WARC-Concurrent-To', resp_id)

        self._do_write_req_resp(req, resp, params)

    def write_record(self, record, params=None):  #pragma: no cover
        raise NotImplemented()

    def _do_write_req_resp(self, req, resp, params):  #pragma: no cover
        raise NotImplemented()

    def _dedup_key(self, record, digest):
        if self.dedup_per_url:
            return (record.rec_headers.get_header('WARC-Target-URI'), digest)
        return digest

    def _apply_dedup(self, record):
        """
        Check record against the dedup index.

        Equality scope: the payload digest (computed with
        self.digest_algorithm over the payload bytes only, excluding HTTP
        status line and headers), optionally scoped to the record's
        WARC-Target-URI (see dedup_per_url).

        Returns (record_to_write, dedup_key). If a duplicate is found,
        record_to_write is a revisit record referring to the original and
        dedup_key is None. Otherwise record_to_write is the original record
        and dedup_key must be registered in the index after the record has
        been written successfully.
        """
        if record.rec_type not in self.DEDUP_RECORD_TYPES:
            return record, None

        # empty payloads are not worth a revisit record
        if not record.payload_length:
            return record, None

        digest = record.rec_headers.get_header('WARC-Payload-Digest')
        if digest and not digest.startswith(self.digest_algorithm + ':'):
            # existing digest uses a different algorithm, recompute
            record.rec_headers.remove_header('WARC-Payload-Digest')
            digest = None

        if not digest:
            self.ensure_digest(record, block=False, payload=True)
            digest = record.rec_headers.get_header('WARC-Payload-Digest')

        if not digest:
            return record, None

        key = self._dedup_key(record, digest)
        refers_to = self._dedup_index.get(key)
        if refers_to is None:
            return record, key

        # duplicate payload: write a revisit record instead, keeping this
        # capture's HTTP status line, headers and WARC-Date
        warc_headers_dict = {}
        date = record.rec_headers.get_header('WARC-Date')
        if date:
            warc_headers_dict['WARC-Date'] = date

        revisit = self.create_revisit_record(
                record.rec_headers.get_header('WARC-Target-URI'),
                digest,
                refers_to_uri=refers_to[0],
                refers_to_date=refers_to[1],
                http_headers=record.http_headers,
                warc_headers_dict=warc_headers_dict)

        return revisit, None

    def _write_record_dedup(self, out, record):
        dedup_key = None
        if self.dedup:
            record, dedup_key = self._apply_dedup(record)

        self._write_warc_record(out, record)

        # register only after a successful write, so that a failed write
        # can never leave a later revisit referring to a missing payload
        if dedup_key is not None:
            self._dedup_index[dedup_key] = (
                    record.rec_headers.get_header('WARC-Target-URI'),
                    record.rec_headers.get_header('WARC-Date'))

    def _write_warc_record(self, out, record):
        if self.gzip:
            out = GzippingWrapper(out)

        if record.http_headers:
            record.http_headers.compute_headers_buffer(self.header_filter)

        # Content-Length is None/unknown
        # Fix record by: buffering and recomputing all digests and length
        # (since no length, can't trust existing digests)
        # Also remove content-type for consistent header ordering
        if record.length is None:
            record.rec_headers.remove_header('WARC-Block-Digest')
            if record.rec_type != 'revisit':
                record.rec_headers.remove_header('WARC-Payload-Digest')
            record.rec_headers.remove_header('Content-Type')

            self.ensure_digest(record, block=True, payload=True)

            record.length = record.payload_length

        # ensure digests are set
        else:
            self.ensure_digest(record, block=True, payload=True)

        if record.content_type != None:
            # ensure proper content type
            record.rec_headers.replace_header('Content-Type', record.content_type)

        if record.rec_type == 'revisit':
            http_headers_only = True
        else:
            http_headers_only = False

        # compute Content-Length
        if record.http_headers and record.payload_length >= 0:
            actual_len = 0

            if record.http_headers:
                actual_len = len(record.http_headers.headers_buff)

            if not http_headers_only:
                actual_len += record.payload_length

            record.length = actual_len

        record.rec_headers.replace_header('Content-Length', str(record.length))

        # write record headers -- encoded as utf-8
        # WARC headers can be utf-8 per spec
        out.write(record.rec_headers.to_bytes(encoding='utf-8'))

        # write headers buffer, if any
        if record.http_headers:
            out.write(record.http_headers.headers_buff)

        if not http_headers_only:
            try:
                for buf in self._iter_stream(record.raw_stream):
                    out.write(buf)
            finally:
                if hasattr(record, '_orig_stream'):
                    record.raw_stream.close()
                    record.raw_stream = record._orig_stream

        # add two lines
        out.write(b'\r\n\r\n')

        out.flush()


# ============================================================================
class GzippingWrapper(object):
    def __init__(self, out):
        self.compressor = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS + 16)
        self.out = out

    def write(self, buff):
        #if isinstance(buff, str):
        #    buff = buff.encode('utf-8')
        buff = self.compressor.compress(buff)
        self.out.write(buff)

    def flush(self):
        buff = self.compressor.flush()
        self.out.write(buff)
        self.out.flush()


# ============================================================================
class WARCWriter(BaseWARCWriter):
    def __init__(self, filebuf, *args, **kwargs):
        super(WARCWriter, self).__init__(*args, **kwargs)
        self.out = filebuf

    def write_record(self, record, params=None):
        self._write_record_dedup(self.out, record)

    def _do_write_req_resp(self, req, resp, params):
        self._write_record_dedup(self.out, resp)
        self._write_warc_record(self.out, req)


# ============================================================================
class BufferWARCWriter(WARCWriter):
    def __init__(self, *args, **kwargs):
        out = self._create_temp_file()
        super(BufferWARCWriter, self).__init__(out, *args, **kwargs)

    def get_contents(self):
        pos = self.out.tell()
        self.out.seek(0)
        buff = self.out.read()
        self.out.seek(pos)
        return buff

    def get_stream(self):
        self.out.seek(0)
        return self.out


