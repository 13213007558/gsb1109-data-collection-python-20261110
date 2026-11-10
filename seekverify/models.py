"""
Plain data objects shared by the verifier, fixtures and report layer.

Nothing in here performs any IO; these types only describe:

* what an index entry claims (:class:`IndexEntry`),
* what an independent re-seek actually found (:class:`RecordFingerprint`),
* how the two compare (:class:`VerificationResult`).
"""

from collections import OrderedDict


class IndexEntry(object):
    """
    A single line of a CDX-style index.

    ``offset`` and ``length`` are always byte offsets/lengths measured in the
    physical on-disk file (never in decompressed content). For a multi-member
    gzip WARC, ``length`` spans one complete gzip member, including its
    envelope and the trailing CRLF CRLF that lives inside that member.
    """

    def __init__(self, offset, length=None, record_id=None, url=None,
                 content_length=None, payload_digest=None, block_digest=None,
                 raw=None):
        self.offset = int(offset)
        self.length = None if length is None else int(length)
        self.record_id = record_id
        self.url = url
        # Content-Length declared in the WARC/ARC record headers (logical
        # block length), distinct from the physical index ``length``.
        self.content_length = (None if content_length is None
                               else int(content_length))
        self.payload_digest = payload_digest
        self.block_digest = block_digest
        # Preserve the original parsed dict so nothing the index carried is
        # silently dropped when emitting reports.
        self.raw = raw if raw is not None else {}

    def __repr__(self):
        return ("IndexEntry(offset={0}, length={1}, id={2!r}, url={3!r})"
                .format(self.offset, self.length, self.record_id, self.url))


class RecordFingerprint(object):
    """
    Everything observed by re-opening the archive at an index offset.

    All digests use the same notation warcio emits
    (``"<algo>:<base32>"``), so they can be compared verbatim against the
    ``WARC-Payload-Digest`` / ``WARC-Block-Digest`` header values.
    """

    def __init__(self, offset, physical_length, record_id=None, url=None,
                 rec_type=None, declared_content_length=None,
                 payload_digest=None, block_digest=None,
                 payload_sha256=None, block_sha256=None,
                 payload_size=0, payload_prefix=b"",
                 next_record_offset=None, gzip_member=False):
        self.offset = offset
        self.physical_length = physical_length
        self.record_id = record_id
        self.url = url
        self.rec_type = rec_type
        self.declared_content_length = declared_content_length
        self.payload_digest = payload_digest
        self.block_digest = block_digest
        self.payload_sha256 = payload_sha256
        self.block_sha256 = block_sha256
        self.payload_size = payload_size
        self.payload_prefix = payload_prefix
        self.next_record_offset = next_record_offset
        self.gzip_member = gzip_member

    def to_dict(self):
        return OrderedDict([
            ("offset", self.offset),
            ("physical_length", self.physical_length),
            ("record_id", self.record_id),
            ("url", self.url),
            ("rec_type", self.rec_type),
            ("declared_content_length", self.declared_content_length),
            ("payload_digest_header", self.payload_digest),
            ("block_digest_header", self.block_digest),
            ("payload_sha256", self.payload_sha256),
            ("block_sha256", self.block_sha256),
            ("payload_size", self.payload_size),
            ("payload_prefix_hex", self.payload_prefix[:16].hex()),
            ("next_record_offset", self.next_record_offset),
            ("gzip_member", self.gzip_member),
        ])


class VerificationResult(object):
    """Outcome of checking one index entry."""

    def __init__(self, entry, fingerprint=None, error=None, checks=None):
        self.entry = entry
        self.fingerprint = fingerprint
        self.error = error
        # Ordered mapping check-name -> bool; absent when an error fired
        # before the record could be read.
        self.checks = checks if checks is not None else OrderedDict()

    @property
    def ok(self):
        if self.error is not None:
            return False
        return all(self.checks.values())

    @property
    def status(self):
        if self.error is not None:
            return self.error.kind
        return "ok" if self.ok else "index_inconsistent"

    def to_dict(self):
        out = OrderedDict()
        out["status"] = self.status
        out["index"] = OrderedDict([
            ("offset", self.entry.offset),
            ("length", self.entry.length),
            ("record_id", self.entry.record_id),
            ("url", self.entry.url),
            ("content_length", self.entry.content_length),
        ])
        out["checks"] = OrderedDict(self.checks)
        if self.fingerprint is not None:
            out["observed"] = self.fingerprint.to_dict()
        if self.error is not None:
            out["error"] = self.error.to_dict()
        return out
