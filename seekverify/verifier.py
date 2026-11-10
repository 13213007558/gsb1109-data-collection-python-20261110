"""
The verifier: re-locate every indexed record and prove the offset units.

For each index entry two independent reads are performed:

1. A byte-level read (:mod:`seekverify.rawreader`) that understands gzip
   member boundaries and WARC framing without warcio. This decides whether the
   *file* is damaged.
2. A warcio re-seek (``fh.seek(offset)`` + :class:`ArchiveIterator`), which is
   exactly what the production backend does. This decides whether *warcio*
   lands on the record the index promised.

Only when both readers agree with each other and with every index field is an
entry reported OK. Failures are split into :class:`IndexInconsistentError`
(the bytes are fine, the index lies) and :class:`CorruptionError` (the bytes
themselves cannot be parsed). The supplied index is never modified.
"""

import hashlib
import json

from warcio.archiveiterator import ArchiveIterator
from warcio.exceptions import ArchiveLoadFailed

from .errors import CorruptionError, IndexInconsistentError
from .models import IndexEntry, RecordFingerprint, VerificationResult
from . import rawreader

# Aliases tolerated when loading CDX/CDXJ style index files.
_FIELD_ALIASES = {
    "offset": ("offset",),
    "length": ("length",),
    "record_id": ("warc-record-id", "record-id", "id"),
    "url": ("warc-target-uri", "url", "uri"),
    "content_length": ("content-length", "cl"),
    "payload_digest": ("warc-payload-digest", "digest"),
    "block_digest": ("warc-block-digest",),
}


def _pick(line, canonical):
    for name in _FIELD_ALIASES[canonical]:
        if name in line and line[name] is not None:
            return line[name]
    return None


def load_index(source):
    """
    Load JSON-lines index output (e.g. ``warcio index -f offset,length,...``).

    Accepts a path or an iterable of already-decoded dicts / JSON lines.
    """
    if hasattr(source, "read"):
        lines = source.read().splitlines()
    elif isinstance(source, str):
        with open(source, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        lines = list(source)

    entries = []
    for line in lines:
        if isinstance(line, dict):
            row = line
        else:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
        entries.append(IndexEntry(
            offset=_pick(row, "offset"),
            length=_pick(row, "length"),
            record_id=_pick(row, "record_id"),
            url=_pick(row, "url"),
            content_length=_pick(row, "content_length"),
            payload_digest=_pick(row, "payload_digest"),
            block_digest=_pick(row, "block_digest"),
            raw=row,
        ))
    return entries


def build_truth_index(path, gzip=None):
    """
    Build a ground-truth index straight from the raw bytes.

    This is deliberately separate from :func:`load_index`: it never touches a
    user-supplied index and exists only to generate baselines and fixtures.
    """
    if gzip is None:
        gzip = rawreader.is_gzip_file(path)
    entries = []
    for obs in rawreader.scan_all_records(path, gzip):
        digests = obs.digests("sha256")
        entries.append({
            "offset": obs.offset,
            "length": obs.physical_length,
            "warc-record-id": obs.record_id,
            "warc-target-uri": obs.url,
            "content-length": obs.declared_content_length,
            "warc-payload-digest": obs.declared_payload_digest,
            "warc-block-digest": obs.declared_block_digest,
            "payload_sha256": digests["payload"],
            "block_sha256": digests["block"],
            "next_record_offset": obs.next_record_offset,
            "gzip_member": obs.gzip_member,
        })
    return entries


class SeekVerifier(object):
    def __init__(self, path, gzip=None):
        self.path = path
        self.gzip = rawreader.is_gzip_file(path) if gzip is None else gzip

    # ------------------------------------------------------------------
    # warcio-side re-seek
    # ------------------------------------------------------------------
    def _warcio_reseek(self, offset, consume=0):
        """
        Seek via warcio and report what it finds.

        The *complete* content block is always drained and hashed so it can be
        compared byte-for-byte with the independent oracle. ``consume`` > 0
        additionally runs a pass that reads only that many payload bytes before
        moving on, mirroring backends that abandon a record early.
        """
        with open(self.path, "rb") as fh:
            fh.seek(offset)
            try:
                it = ArchiveIterator(fh)
                record = next(it)
            except ArchiveLoadFailed as exc:
                raise CorruptionError(
                    "warcio cannot parse a record at indexed offset",
                    {"offset": offset, "warcio_error": exc.msg},
                )
            except StopIteration:
                raise CorruptionError(
                    "no record produced by warcio at indexed offset",
                    {"offset": offset},
                )

            # warcio has already consumed the HTTP header section, so
            # raw_stream now yields the payload only. Rebuild the full block
            # as (http headers, when present) + (remaining stream) so it can
            # be compared byte-for-byte with the independent oracle's block.
            payload_seen = record.raw_stream.read()
            header_bytes = (record.http_headers.to_bytes()
                            if record.http_headers else b"")
            block_rebuilt = header_bytes + payload_seen
            block_digest = hashlib.sha256(block_rebuilt).hexdigest()
            payload_digest = hashlib.sha256(payload_seen).hexdigest()
            it.read_to_end()
            warcio_offset = it.get_record_offset()
            warcio_length = it.get_record_length()

            # Reaching the *following* record is best-effort: a damaged later
            # member must not poison the verdict of the record under test.
            next_offset = None
            next_read_error = None
            try:
                following = next(it)
                next_offset = it.get_record_offset()
                assert following is not None
            except StopIteration:
                next_offset = None
            except ArchiveLoadFailed as exc:
                next_read_error = exc.msg

            partial = None
            if consume:
                partial = self._warcio_partial_pass(offset, consume)

            return {
                "record_id": record.rec_headers.get_header("WARC-Record-ID"),
                "url": record.rec_headers.get_header("WARC-Target-URI"),
                "rec_type": record.rec_type,
                "content_length": record.length,
                "offset": warcio_offset,
                "length": warcio_length,
                "block_sha256_hex": block_digest,
                "block_size": len(block_rebuilt),
                "payload_sha256_hex": payload_digest,
                "payload_size": len(payload_seen),
                "partial": partial,
                "next_record_offset": next_offset,
                "next_read_error": next_read_error,
            }

    def _warcio_partial_pass(self, offset, consume):
        """Re-seek and read only ``consume`` payload bytes, then move on."""
        with open(self.path, "rb") as fh:
            fh.seek(offset)
            it = ArchiveIterator(fh)
            record = next(it)
            seen = record.raw_stream.read(consume)
            it.read_to_end()
            return {
                "requested": consume,
                "seen_size": len(seen),
                "record_offset": it.get_record_offset(),
                "record_length": it.get_record_length(),
                "seen_prefix_hex": seen[:16].hex(),
            }

    def _warcio_probe(self, offset):
        """Non-raising description of warcio's behaviour at a bad offset."""
        try:
            observed = self._warcio_reseek(offset)
            return {"outcome": "parsed_record",
                    "record_id": observed["record_id"],
                    "record_offset": observed["offset"]}
        except CorruptionError as exc:
            return {"outcome": "parse_failed",
                    "warcio_error": exc.detail.get("warcio_error",
                                                   exc.message)}

    def _neighbour_health(self, offset):
        """Check whether the record beginning at ``offset`` is raw-readable."""
        if offset is None:
            return {"boundary": None, "readable": True}
        try:
            neighbour = rawreader.read_record(self.path, offset, self.gzip)
            return {"boundary": offset, "readable": True,
                    "record_id": neighbour.record_id}
        except CorruptionError as exc:
            return {"boundary": offset, "readable": False,
                    "error": exc.message}

    # ------------------------------------------------------------------
    # fingerprint from the raw oracle
    # ------------------------------------------------------------------
    def _fingerprint(self, obs):
        declared_algo = "sha256"
        for header in (obs.declared_payload_digest, obs.declared_block_digest):
            if header and ":" in header:
                declared_algo = header.split(":", 1)[0]
                break
        # Cache the non-sha256 recomputation, tagged with its algorithm so a
        # hypothetical mixed-algo record can never pull the wrong digest.
        algo_digests = None
        if declared_algo != "sha256":
            algo_digests = (declared_algo, obs.digest_with_declared_algo(
                obs.declared_payload_digest or obs.declared_block_digest))
        sha = obs.digests("sha256")
        payload = obs.payload_bytes()
        return RecordFingerprint(
            offset=obs.offset,
            physical_length=obs.physical_length,
            record_id=obs.record_id,
            url=obs.url,
            rec_type=obs.rec_type,
            declared_content_length=obs.declared_content_length,
            payload_digest=obs.declared_payload_digest,
            block_digest=obs.declared_block_digest,
            payload_sha256=sha["payload"],
            block_sha256=sha["block"],
            payload_size=sha["payload_size"],
            payload_prefix=payload[:16],
            next_record_offset=obs.next_record_offset,
            gzip_member=obs.gzip_member,
        ), algo_digests

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def verify_entry(self, entry, partial_consume=0):
        """
        Verify one :class:`IndexEntry`.

        Raises :class:`CorruptionError` or :class:`IndexInconsistentError`;
        on success returns a :class:`VerificationResult`.
        """
        # Step 1: raw oracle at the indexed offset. A failure here is not yet
        # declared corruption: first prove whether the file as a whole is
        # damaged, because a wrong (but parseable-file) offset is an index bug.
        try:
            obs = rawreader.read_record(self.path, entry.offset, self.gzip)
        except CorruptionError as targeted:
            healthy, scan_info = rawreader.full_scan_healthy(
                self.path, self.gzip)
            if not healthy:
                raise CorruptionError(
                    "archive damaged; record cannot be read at indexed offset",
                    {"offset": entry.offset,
                     "targeted_error": targeted.to_dict(),
                     "full_scan_error": scan_info.to_dict()},
                )
            detail = {"offset": entry.offset,
                      "reason": targeted.message,
                      "targeted_detail": targeted.detail,
                      "full_scan": scan_info}
            try:
                detail["nearest_boundary"] = (
                    rawreader.find_gzip_member(self.path, entry.offset)
                    if self.gzip
                    else rawreader.find_warc_record(
                        self.path, self.gzip, entry.offset))
            except CorruptionError:
                pass
            detail["warcio_at_offset"] = self._warcio_probe(entry.offset)
            raise IndexInconsistentError(
                "indexed offset is not a record boundary, but the archive "
                "itself is intact", detail)

        fingerprint, algo_digests = self._fingerprint(obs)

        # Step 2: warcio re-seek, optionally consuming only part of payload.
        try:
            warcio = self._warcio_reseek(entry.offset,
                                         consume=partial_consume)
        except CorruptionError as exc:
            # Raw bytes parsed cleanly yet warcio failed: the difference is
            # evidence of a framing problem, but since the bytes are readable
            # keep this distinct from raw corruption by reporting both views.
            raise CorruptionError(
                "raw record parsed but warcio re-seek failed",
                {"offset": entry.offset, "warcio_error": exc.detail,
                 "raw_record_id": obs.record_id})

        checks = {}
        mismatches = []

        def check(name, expected, actual):
            # Fields absent from the index (None) simply are not asserted.
            if expected is None:
                return None
            passed = expected == actual
            checks[name] = passed
            if not passed:
                mismatches.append({"check": name, "index_or_oracle": expected,
                                   "observed": actual})
            return passed

        # --- warcio must land exactly where the raw oracle did ---
        check("warcio_offset_matches", obs.offset, warcio["offset"])
        check("warcio_length_matches", obs.physical_length, warcio["length"])
        check("warcio_id_matches", obs.record_id, warcio["record_id"])
        check("warcio_url_matches", obs.url, warcio["url"])
        check("warcio_content_length_matches", obs.declared_content_length,
              warcio["content_length"])

        # --- byte-level block/payload cross-check: the anti-bleed guarantee ---
        # warcio's rebuilt block and its payload must be byte-identical to the
        # raw oracle's. A wrong offset unit that still parses would differ
        # here, and any read into the neighbouring record changes a digest.
        oracle_payload = obs.payload_bytes()
        check("warcio_block_size_matches", len(obs.block),
              warcio["block_size"])
        check("warcio_block_sha256_matches",
              hashlib.sha256(obs.block).hexdigest(),
              warcio["block_sha256_hex"])
        check("warcio_payload_size_matches", len(oracle_payload),
              warcio["payload_size"])
        check("warcio_payload_sha256_matches",
              hashlib.sha256(oracle_payload).hexdigest(),
              warcio["payload_sha256_hex"])

        # The raw oracle owns adjacent-boundary truth. warcio's ability to
        # reach the following record is best-effort: a damaged later member
        # must not poison the verdict of the record under test.
        if warcio["next_read_error"] is not None:
            checks["warcio_next_offset_skipped_damaged_neighbour"] = True
        elif warcio["next_record_offset"] is not None:
            check("warcio_next_offset_matches", obs.next_record_offset,
                  warcio["next_record_offset"])
        elif obs.next_record_offset is not None:
            neighbour = self._neighbour_health(obs.next_record_offset)
            if neighbour["readable"]:
                # Healthy neighbour that warcio failed to reach for no raw
                # reason: surface it rather than silently dropping the check.
                check("warcio_next_offset_matches", obs.next_record_offset,
                      warcio["next_record_offset"])
            else:
                checks["warcio_next_offset_skipped_damaged_neighbour"] = True

        # --- the index entry must describe this exact record ---
        check("record_id", entry.record_id, obs.record_id)
        check("url", entry.url, obs.url)
        check("content_length", entry.content_length,
              obs.declared_content_length)
        check("physical_length", entry.length, obs.physical_length)

        # --- payload / block digests carried by the index ---
        # revisit records carry no payload; their WARC-Payload-Digest is a
        # reference to the revisited record, so it must not be recomputed
        # against the (empty) revisit block (mirrors warcio semantics).
        if obs.rec_type != "revisit":
            content_error = self._check_digest(
                entry, obs, "payload", algo_digests, mismatches, checks)
            if content_error is not None:
                raise content_error
        else:
            checks["payload_digest_reference_only_revisit"] = True
        content_error = self._check_digest(
            entry, obs, "block", algo_digests, mismatches, checks)
        if content_error is not None:
            raise content_error

        # --- adjacent-record safety for the partial-read path ---
        if partial_consume:
            partial = warcio["partial"]
            # An early payload read must not move the record's offset/length
            # bookkeeping, nor may it read more payload bytes than exist.
            stable = (partial["record_offset"] == obs.offset
                      and partial["record_length"] == obs.physical_length
                      and partial["seen_size"]
                      == min(partial_consume, len(oracle_payload)))
            if (warcio["next_read_error"] is None
                    and warcio["next_record_offset"] is not None):
                stable = stable and (
                    warcio["next_record_offset"] == obs.next_record_offset)
            checks["partial_consume_did_not_bleed"] = stable
            if not stable:
                mismatches.append({
                    "check": "partial_consume_did_not_bleed",
                    "detail": "partial payload read changed offset "
                              "bookkeeping or over-read the block",
                    "partial": partial,
                })

        if mismatches:
            raise IndexInconsistentError(
                "index entry does not match the record at its offset",
                {"offset": entry.offset, "mismatches": mismatches},
            )
        return VerificationResult(entry, fingerprint, checks=checks)

    @staticmethod
    def _digest_for_header(obs, header_digest, kind, algo_digests):
        algo = header_digest.split(":", 1)[0]
        if algo == "sha256":
            return obs.digests("sha256")[kind]
        if algo_digests is not None and algo_digests[0] == algo:
            return algo_digests[1][kind]
        recomputed = obs.digest_with_declared_algo(header_digest)
        return recomputed[kind] if recomputed else None

    def _check_digest(self, entry, obs, kind, algo_digests, mismatches,
                      checks):
        """
        Compare actual bytes against the WARC header digest and index digest.

        Returns a :class:`CorruptionError` when the bytes contradict their own
        WARC digest header (content damage); appends an index mismatch when
        only the index-supplied digest is wrong. ``kind`` is ``"payload"`` or
        ``"block"``.
        """
        header_digest = (obs.declared_payload_digest if kind == "payload"
                         else obs.declared_block_digest)
        index_digest = (entry.payload_digest if kind == "payload"
                        else entry.block_digest)
        check_name = kind + "_digest"
        reference = header_digest or index_digest
        if not reference:
            return None

        recomputed = self._digest_for_header(obs, reference, kind,
                                             algo_digests)
        if recomputed is None:
            return None

        # The WARC header is the archive's own claim. Bytes that fail it are
        # content corruption even when the index copied the header faithfully.
        if header_digest is not None and recomputed != header_digest:
            checks[check_name] = False
            return CorruptionError(
                "record {0} bytes fail their WARC {0} digest (content "
                "corruption)".format(kind),
                {"offset": obs.offset, "warc_" + check_name: header_digest,
                 "recomputed_" + check_name: recomputed},
            )

        if index_digest is not None:
            passed = index_digest == recomputed
            checks[check_name] = passed
            if not passed:
                mismatches.append({"check": check_name,
                                   "index_or_oracle": index_digest,
                                   "observed": recomputed})
        return None

    def verify_all(self, entries, partial_consume=0):
        """
        Verify every entry; never raises on the first failure.

        Returns a list of :class:`VerificationResult`, with errors attached to
        the individual result so one bad entry cannot mask the others.
        """
        results = []
        for entry in entries:
            try:
                result = self.verify_entry(entry, partial_consume)
            except (CorruptionError, IndexInconsistentError) as exc:
                result = VerificationResult(entry, error=exc)
            results.append(result)
        return results

    def verify_partial_consumption(self, entry, consume_bytes):
        """Convenience wrapper naming the partial-payload scenario explicitly."""
        return self.verify_entry(entry, partial_consume=consume_bytes)
