"""
Deterministic fixture and counterexample builders.

Two kinds of negative evidence are needed and they are built differently on
purpose:

* *index inconsistencies* keep a perfectly valid archive and only alter the
  index line that describes it;
* *file corruption* alters the archive bytes themselves while every index line
  stays truthful.

Keeping the two constructions separate is what lets the test suite prove the
verifier's error taxonomy instead of accidentally passing one error class off
as the other.
"""

import gzip
import json
import os

from io import BytesIO

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from . import rawreader


# ---------------------------------------------------------------------------
# Valid archive construction
# ---------------------------------------------------------------------------

def _http_response_headers():
    return StatusAndHeaders("200 OK", [("Content-Type", "text/plain")],
                            protocol="HTTP/1.0")


def _http_request_headers():
    return StatusAndHeaders("GET / HTTP/1.0", [], is_http_request=True)


def _record_id(name):
    return "<urn:uuid:seekverify-{0}>".format(name)


def _write_record(writer, uri, rec_type, payload, name, http_headers=None):
    stream = BytesIO(payload)
    record = writer.create_warc_record(
        uri, rec_type,
        payload=stream,
        length=len(payload),
        http_headers=http_headers,
        warc_headers_dict={"WARC-Record-ID": _record_id(name),
                           "WARC-Date": "2026-11-10T00:00:00Z"},
    )
    writer.write_record(record)


def build_valid_warc(path, gzip):
    """
    Write a 4-record WARC covering every seek scenario in the brief.

    Records:
      * a normal HTTP response with a multi-KB payload,
      * an HTTP response with an *empty* body,
      * an HTTP request paired with the first response,
      * a ``resource`` record with non-HTTP binary content.
    """
    large_payload = (b"seekverify-payload-line-%04d\n" % 0) * 400
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=gzip)
        _write_record(writer, "http://example.com/large", "response",
                      large_payload, "large", _http_response_headers())
        _write_record(writer, "http://example.com/empty", "response",
                      b"", "empty", _http_response_headers())
        _write_record(writer, "http://example.com/large", "request",
                      b"", "request", _http_request_headers())
        _write_record(writer, "urn:seekverify:binary", "resource",
                      bytes(range(256)) * 4, "resource")
    return path


def build_single_member_gzip(source_warc, path):
    """
    Gzip a whole WARC as one member.

    This is the shape warcio rejects as "non-chunked gzip": random seek is
    impossible because no record starts at its own gzip boundary. ``mtime=0``
    keeps the produced byte stream deterministic across runs.
    """
    with open(source_warc, "rb") as src, open(path, "wb") as dst:
        with gzip.GzipFile(fileobj=dst, mode="wb", mtime=0) as member:
            member.write(src.read())
    return path


def _gzip_member(data):
    """Deterministic single gzip member (zero mtime, default compression)."""
    buf = BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as member:
        member.write(data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Archive corruption (index lines remain truthful)
# ---------------------------------------------------------------------------

def corrupt_member_bytes(source_gzip, path, member_index=1):
    """
    Flip one compressed byte inside a chosen gzip member.

    The gzip header and the 8-byte trailer are left untouched, so the member
    still *looks* like gzip but fails the CRC32/ISIZE check on decompression.
    """
    members = list(rawreader.scan_gzip_members(source_gzip))
    if member_index >= len(members):
        raise IndexError("member {0} not found".format(member_index))
    offset, length, _ = members[member_index]
    with open(source_gzip, "rb") as fh:
        data = bytearray(fh.read())
    target = offset + max(12, length // 2)  # skip 10-byte header + spare
    data[target] ^= 0xFF
    with open(path, "wb") as fh:
        fh.write(bytes(data))
    return {"path": path, "member_offset": offset,
            "flipped_byte_offset": target}


def truncate_inside_member(source_gzip, path, member_index=1):
    """Cut the file off in the middle of a member, before its trailer."""
    members = list(rawreader.scan_gzip_members(source_gzip))
    if member_index >= len(members):
        raise IndexError("member {0} not found".format(member_index))
    offset, length, _ = members[member_index]
    cut = offset + length // 2
    with open(source_gzip, "rb") as fh:
        data = fh.read(cut)
    with open(path, "wb") as fh:
        fh.write(data)
    return {"path": path, "cut_offset": cut, "member_offset": offset}


def corrupt_payload(source_gzip, path, member_index=0):
    """
    Rewrite payload bytes inside one member, keeping gzip framing intact.

    The member still decompresses cleanly and every boundary is valid, but the
    payload now contradicts the record's own WARC-Payload/Block-Digest. This
    isolates *content* corruption from both framing damage and index errors.
    The file is recompressed one record per member so it remains seekable.
    """
    members = list(rawreader.scan_gzip_members(source_gzip))
    if member_index >= len(members):
        raise IndexError("member {0} not found".format(member_index))
    with open(path, "wb") as out:
        for index, (_, _, decompressed) in enumerate(members):
            if index == member_index:
                # This token occurs only in the HTTP payload, never in headers.
                marker = b"seekverify-payload"
                if marker not in decompressed:
                    raise ValueError("payload marker not found in member "
                                     + str(index))
                decompressed = decompressed.replace(
                    marker, b"xEEKVERIFY-payload", 1)
            out.write(_gzip_member(decompressed))
    offset, _, _ = members[member_index]
    return {"path": path, "member_offset": offset,
            "note": "framing valid, WARC digest headers now stale"}


# ---------------------------------------------------------------------------
# Index construction and tampering (archive stays valid)
# ---------------------------------------------------------------------------

def truth_index(archive_path, gzip=None):
    """Ground-truth index built from raw bytes only (never from the input)."""
    if gzip is None:
        gzip = rawreader.is_gzip_file(archive_path)
    rows = []
    for obs in rawreader.scan_all_records(archive_path, gzip):
        rows.append({
            "offset": obs.offset,
            "length": obs.physical_length,
            "warc-record-id": obs.record_id,
            "warc-target-uri": obs.url,
            "content-length": obs.declared_content_length,
            "warc-payload-digest": obs.declared_payload_digest,
            "warc-block-digest": obs.declared_block_digest,
        })
    return rows


def write_jsonl(rows, path):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def tamper_offset(rows, path, entry_index=1, delta=1):
    """Shift an offset by one byte: the classic offset-unit mistake."""
    rows = [dict(row) for row in rows]
    rows[entry_index]["offset"] += delta
    return write_jsonl(rows, path)


def tamper_length(rows, path, entry_index=1, delta=4):
    """Make the physical length claim reach into a neighbouring record."""
    rows = [dict(row) for row in rows]
    rows[entry_index]["length"] += delta
    return write_jsonl(rows, path)


def tamper_record_id(rows, path, entry_index=1):
    rows = [dict(row) for row in rows]
    rows[entry_index]["warc-record-id"] = _record_id("does-not-exist")
    return write_jsonl(rows, path)


def tamper_url(rows, path, entry_index=0):
    rows = [dict(row) for row in rows]
    rows[entry_index]["warc-target-uri"] = "http://example.invalid/rewritten"
    return write_jsonl(rows, path)


def tamper_payload_digest(rows, path, entry_index=0):
    rows = [dict(row) for row in rows]
    original = rows[entry_index].get("warc-payload-digest")
    if original and ":" in original:
        algo, value = original.split(":", 1)
        flipped = ("A" if value[0] != "A" else "B") + value[1:]
        rows[entry_index]["warc-payload-digest"] = algo + ":" + flipped
    else:
        rows[entry_index]["warc-payload-digest"] = "sha1:" + "A" * 32
    return write_jsonl(rows, path)


def generate_fixture_bundle(directory):
    """
    Materialize the complete, reproducible counterexample bundle.

    Returns a manifest dict describing every generated file and what it
    proves. The same call always produces structurally identical files
    (fixed record ids/dates), so results can be diffed across machines.
    """
    os.makedirs(directory, exist_ok=True)
    manifest = {}

    plain = os.path.join(directory, "valid.warc")
    gz = os.path.join(directory, "valid.warc.gz")
    build_valid_warc(plain, gzip=False)
    build_valid_warc(gz, gzip=True)
    manifest["valid_uncompressed"] = plain
    manifest["valid_multimember_gzip"] = gz

    single = os.path.join(directory, "single-member.warc.gz")
    build_single_member_gzip(plain, single)
    manifest["non_chunked_single_member_gzip"] = single

    truth_gz = truth_index(gz, gzip=True)
    truth_plain = truth_index(plain, gzip=False)
    manifest["truth_index_gzip"] = write_jsonl(
        truth_gz, os.path.join(directory, "index-gzip.jsonl"))
    manifest["truth_index_plain"] = write_jsonl(
        truth_plain, os.path.join(directory, "index-plain.jsonl"))

    manifest["bad_index_offset"] = tamper_offset(
        truth_gz, os.path.join(directory, "index-bad-offset.jsonl"))
    manifest["bad_index_length"] = tamper_length(
        truth_gz, os.path.join(directory, "index-bad-length.jsonl"))
    manifest["bad_index_id"] = tamper_record_id(
        truth_gz, os.path.join(directory, "index-bad-id.jsonl"))
    manifest["bad_index_url"] = tamper_url(
        truth_gz, os.path.join(directory, "index-bad-url.jsonl"))
    manifest["bad_index_digest"] = tamper_payload_digest(
        truth_gz, os.path.join(directory, "index-bad-digest.jsonl"))

    manifest["corrupt_crc"] = corrupt_member_bytes(
        gz, os.path.join(directory, "corrupt-crc.warc.gz"))
    manifest["corrupt_truncated"] = truncate_inside_member(
        gz, os.path.join(directory, "corrupt-truncated.warc.gz"))

    payload_path = os.path.join(directory, "corrupt-payload.warc.gz")
    manifest["corrupt_payload"] = corrupt_payload(gz, payload_path)
    # Framing is intact, so this recompressed file gets its own truthful
    # index; verification must still flag the stale WARC digest as corruption.
    manifest["truth_index_corrupt_payload"] = write_jsonl(
        truth_index(payload_path, gzip=True),
        os.path.join(directory, "index-corrupt-payload.jsonl"))
    return manifest
