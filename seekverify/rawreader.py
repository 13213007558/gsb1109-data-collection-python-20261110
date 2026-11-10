"""
Byte-level record reader used as an *independent* oracle.

warcio is the system under test, so verification must not trust it alone.
The helpers here locate records using only the raw file bytes:

* gzip member boundaries are found with a bare ``zlib`` decompressor (which
  also validates CRC32 / ISIZE and surfaces truncation),
* WARC header blocks and Content-Length are parsed directly,
* block / payload digests are recomputed with hashlib.

The warcio-level re-seek in :mod:`seekverify.verifier` is then cross-checked
against every value observed here.
"""

import hashlib
import base64
import zlib

from .errors import CorruptionError

GZIP_MAGIC = b"\x1f\x8b"
WARC_MAGIC = b"WARC/"
HEADER_TERMINATOR = b"\r\n\r\n"
HTTP_TYPES = ("response", "request", "revisit")


def _b32_digest(algo, data):
    """Emit digests in warcio notation: ``"<algo>:<base32>"``."""
    digest = hashlib.new(algo, data).digest()
    return algo + ":" + base64.b32encode(digest).decode("ascii")


def is_gzip_file(path):
    with open(path, "rb") as fh:
        return fh.read(2) == GZIP_MAGIC


def file_size(path):
    with open(path, "rb") as fh:
        return fh.seek(0, 2)


def scan_gzip_members(path, start=0):
    """
    Walk concatenated gzip members using zlib only.

    Yields ``(offset, raw_length, decompressed_bytes)`` for every member at or
    after ``start``. Raises :class:`CorruptionError` on a bad magic, a CRC
    failure or a member cut off before its stream end.
    """
    size = file_size(path)
    offset = start
    with open(path, "rb") as fh:
        while offset < size:
            fh.seek(offset)
            if fh.read(2) != GZIP_MAGIC:
                fh.seek(offset)
                raise CorruptionError(
                    "expected gzip member magic at boundary",
                    {"offset": offset, "found_hex": fh.read(4).hex()},
                )
            fh.seek(offset)
            decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
            chunks = []
            try:
                while True:
                    buf = fh.read(16384)
                    if not buf:
                        break
                    chunks.append(decomp.decompress(buf))
                    if decomp.unused_data:
                        break
            except zlib.error as exc:
                raise CorruptionError(
                    "gzip member failed to decompress (CRC or deflate error)",
                    {"offset": offset, "zlib_error": str(exc)},
                )

            if not decomp.eof:
                raise CorruptionError(
                    "gzip member truncated before stream end",
                    {"offset": offset},
                )

            # Absolute member end = current file position minus the bytes
            # zlib reported as belonging to the next member(s).
            member_end = fh.tell() - len(decomp.unused_data)
            length = member_end - offset
            yield offset, length, b"".join(chunks)
            offset = member_end


def parse_warc_headers(header_bytes):
    """Split a raw WARC header section into a case-insensitive dict."""
    lines = header_bytes.split(b"\r\n")
    first = lines[0] if lines else b""
    if not first.startswith(b"WARC/"):
        raise CorruptionError(
            "record does not start with a WARC status line",
            {"first_line": first[:40].decode("latin-1", "replace")},
        )
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(b":")
        if not sep:
            raise CorruptionError(
                "malformed WARC header line",
                {"line": line[:80].decode("latin-1", "replace")})
        headers[name.strip().lower().decode("latin-1")] = (
            value.strip().decode("utf-8", "replace"))
    return headers


def _split_payload(rec_type, block):
    """
    Return the payload bytes of a WARC block.

    For HTTP-bearing record types the payload is everything after the HTTP
    header section; every other WARC type uses the whole block as payload.
    """
    if rec_type in HTTP_TYPES:
        idx = block.find(HEADER_TERMINATOR)
        if idx >= 0:
            return block[idx + 4:]
    return block


class RawObservation(object):
    """Result of independently reading one record from raw bytes."""

    def __init__(self, offset, physical_length, headers, block,
                 next_record_offset, gzip_member):
        self.offset = offset
        self.physical_length = physical_length
        self.headers = headers
        self.block = block
        self.next_record_offset = next_record_offset
        self.gzip_member = gzip_member

    @property
    def record_id(self):
        return self.headers.get("warc-record-id")

    @property
    def url(self):
        return self.headers.get("warc-target-uri")

    @property
    def rec_type(self):
        return self.headers.get("warc-type")

    @property
    def declared_content_length(self):
        value = self.headers.get("content-length")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            raise CorruptionError("non-integer Content-Length",
                                  {"content-length": value})

    @property
    def declared_payload_digest(self):
        return self.headers.get("warc-payload-digest")

    @property
    def declared_block_digest(self):
        return self.headers.get("warc-block-digest")

    def payload_bytes(self):
        return _split_payload(self.rec_type, self.block)

    def digests(self, algo="sha256"):
        payload = self.payload_bytes()
        return {
            "payload": _b32_digest(algo, payload),
            "block": _b32_digest(algo, self.block),
            "payload_size": len(payload),
            "block_size": len(self.block),
        }

    def digest_with_declared_algo(self, declared):
        """Recompute using the algorithm named in a WARC digest header."""
        if not declared or ":" not in declared:
            return None
        algo = declared.split(":", 1)[0]
        try:
            hashlib.new(algo)
        except ValueError:
            return None
        payload = self.payload_bytes()
        return {
            "payload": _b32_digest(algo, payload),
            "block": _b32_digest(algo, self.block),
        }


def _parse_warc_record_bytes(data, base_offset):
    """Parse one WARC record out of an in-memory byte buffer."""
    term = data.find(HEADER_TERMINATOR)
    if term < 0:
        raise CorruptionError("WARC header terminator not found",
                              {"offset": base_offset})
    headers = parse_warc_headers(data[:term])
    block_start = term + len(HEADER_TERMINATOR)
    raw_cl = headers.get("content-length")
    try:
        content_length = int(raw_cl)
    except (TypeError, ValueError):
        raise CorruptionError("missing or invalid Content-Length",
                              {"offset": base_offset, "value": raw_cl})
    block_end = block_start + content_length
    if block_end > len(data):
        raise CorruptionError(
            "record block extends past available bytes (truncated record)",
            {"offset": base_offset, "declared": content_length,
             "available": len(data) - block_start},
        )
    return headers, data[block_start:block_end], block_end


def _skip_blank_lines(data, position):
    """Return position after the CRLF blank lines following a record."""
    while data[position:position + 2] == b"\r\n":
        position += 2
    return position


def read_uncompressed_record(path, offset):
    """Read a single WARC record from an uncompressed archive."""
    size = file_size(path)
    if offset >= size:
        raise CorruptionError("offset is at or past end of file",
                              {"offset": offset, "file_size": size})
    with open(path, "rb") as fh:
        fh.seek(offset)
        first = fh.read(len(WARC_MAGIC))
        if first != WARC_MAGIC:
            raise CorruptionError(
                "offset does not point at a WARC record start",
                {"offset": offset, "first_bytes": first.hex()},
            )
        fh.seek(offset)
        data = fh.read()

    term_end = data.find(HEADER_TERMINATOR) + len(HEADER_TERMINATOR)
    headers, block, block_end = _parse_warc_record_bytes(data, offset)
    # Uncompressed length excludes the inter-record CRLF blank lines, matching
    # the offset delta warcio reports for plain WARC files.
    physical_length = term_end + len(block)

    next_offset = None
    if physical_length < len(data):
        if data[physical_length:physical_length + 2] != b"\r\n":
            raise CorruptionError(
                "record not followed by CRLF blank line",
                {"offset": offset,
                 "bytes": data[physical_length:physical_length + 8].hex()},
            )
        boundary = _skip_blank_lines(data, physical_length)
        # Files normally end with the trailing blank lines of the last record.
        if boundary < len(data):
            if data[boundary:boundary + len(WARC_MAGIC)] != WARC_MAGIC:
                raise CorruptionError(
                    "non-blank bytes between records",
                    {"offset": offset + boundary,
                     "bytes": data[boundary:boundary + 8].hex()},
                )
            next_offset = offset + boundary
    return RawObservation(offset, physical_length, headers, block,
                          next_offset, gzip_member=False)


def read_gzip_record(path, offset):
    """Read the single gzip member that begins at ``offset``."""
    for member_offset, raw_length, decompressed in scan_gzip_members(path, offset):
        if member_offset != offset:
            raise CorruptionError(
                "offset lands inside a gzip member, not on a boundary",
                {"offset": offset, "nearest_member": member_offset},
            )
        headers, block, block_end = _parse_warc_record_bytes(decompressed, offset)
        # The CRLF CRLF trailer lives *inside* the gzip member; confirm it.
        trailer = decompressed[block_end:block_end + 4]
        if trailer != b"\r\n\r\n":
            raise CorruptionError(
                "gzip member missing CRLF CRLF trailer after record block",
                {"offset": offset, "trailer": trailer.hex()},
            )
        # A seekable WARC gzip holds exactly one record per member. Anything
        # left after the trailer is the classic "non-chunked" gzip shape:
        # valid compression, but no record starts at its own boundary.
        remainder = decompressed[block_end + 4:]
        if remainder:
            raise CorruptionError(
                "gzip member contains multiple WARC records (non-chunked gzip, "
                "not randomly seekable)",
                {"offset": offset, "extra_bytes": len(remainder),
                 "extra_prefix_hex": remainder[:8].hex()},
            )
        next_offset = offset + raw_length
        if next_offset >= file_size(path):
            next_offset = None
        return RawObservation(offset, raw_length, headers, block,
                              next_offset, gzip_member=True)
    raise CorruptionError("no gzip member found at offset", {"offset": offset})


def read_record(path, offset, gzip):
    if gzip:
        return read_gzip_record(path, offset)
    return read_uncompressed_record(path, offset)


def scan_all_records(path, gzip):
    """Full sequential integrity scan, yielding every :class:`RawObservation`."""
    if gzip:
        size = file_size(path)
        offset = 0
        while offset < size:
            obs = read_gzip_record(path, offset)
            yield obs
            if obs.next_record_offset is None:
                break
            offset = obs.next_record_offset
    else:
        offset = 0
        size = file_size(path)
        while offset < size:
            obs = read_uncompressed_record(path, offset)
            yield obs
            if obs.next_record_offset is None:
                break
            offset = obs.next_record_offset


def count_gzip_members(path):
    """Total member count; used to detect non-chunked (single-member) gzip."""
    return sum(1 for _ in scan_gzip_members(path))


def find_gzip_member(path, offset):
    """
    Locate the gzip member physically containing ``offset``.

    Used after a failed targeted read to describe what an erroneous index
    offset actually points at. Raises :class:`CorruptionError` only when the
    archive itself cannot be scanned, or the offset lies outside every member.
    """
    previous = None
    for member_offset, length, _ in scan_gzip_members(path, 0):
        member_end = member_offset + length
        if member_offset <= offset < member_end:
            return member_offset, member_end
        if member_offset > offset:
            break
        previous = (member_offset, member_end)
    raise CorruptionError("offset lies outside every gzip member",
                          {"offset": offset,
                           "nearest_previous_member": previous})


def find_warc_record(path, gzip, offset):
    """
    Locate the record boundary nearest to ``offset`` on a healthy archive.

    Returns ``(record_offset, next_record_offset)`` for classification detail;
    raises :class:`CorruptionError` if the archive cannot be fully scanned.
    """
    previous = None
    for obs in scan_all_records(path, gzip):
        if obs.offset <= offset and (
                obs.next_record_offset is None or offset < obs.next_record_offset):
            return obs.offset, obs.next_record_offset
        if obs.offset > offset:
            break
        previous = (obs.offset, obs.next_record_offset)
    raise CorruptionError("offset lies outside every record",
                          {"offset": offset,
                           "nearest_previous_record": previous})


def full_scan_healthy(path, gzip):
    """
    Return ``(healthy, error)`` from a complete sequential scan.

    A healthy full scan proves a targeted-read failure cannot be file damage
    and must therefore be an index problem.
    """
    count = 0
    try:
        for obs in scan_all_records(path, gzip):
            # Force payload materialization so block framing is exercised too.
            obs.payload_bytes()
            count += 1
    except CorruptionError as exc:
        return False, exc
    return True, {"records_scanned": count}
