"""
Tests for the offline seek-verification tool.

The suite deliberately exercises every failure mode in the brief and, crucially,
asserts which *family* each failure belongs to:

* a valid archive with an altered index line -> IndexInconsistentError,
* an altered archive with a truthful index    -> CorruptionError.

Archives are generated through warcio's own writer so the fixtures exercise
the real framing code, while verification itself uses an independent reader.
"""

import gzip
import json
import subprocess
import sys

import pytest

from seekverify import fixtures, rawreader
from seekverify.errors import CorruptionError, IndexInconsistentError
from seekverify.models import IndexEntry
from seekverify.report import exit_code, summarize
from seekverify.verifier import SeekVerifier, load_index


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / "bundle"
    manifest = fixtures.generate_fixture_bundle(str(directory))
    manifest["dir"] = str(directory)
    return manifest


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _verifier(manifest, which="valid_multimember_gzip"):
    path = manifest[which]
    return SeekVerifier(path, gzip=path.endswith(".gz"))


# ---------------------------------------------------------------------------
# Ground truth and framing semantics
# ---------------------------------------------------------------------------

class TestGroundTruth:
    def test_truth_index_matches_warcio_indexer(self, bundle):
        for archive, gz_key in (
            ("valid_uncompressed", False),
            ("valid_multimember_gzip", True),
        ):
            path = bundle[archive]
            output = subprocess.check_output([
                sys.executable, "-m", "warcio.cli", "index", path,
                "-f", "offset,length,warc-record-id,warc-target-uri,"
                      "content-length",
            ])
            warcio_rows = [json.loads(line)
                           for line in output.decode("utf-8").splitlines()]
            truth = fixtures.truth_index(path, gzip=gz_key)

            assert len(warcio_rows) == len(truth)
            for warcio_row, truth_row in zip(warcio_rows, truth):
                assert int(warcio_row["offset"]) == truth_row["offset"]
                assert int(warcio_row["length"]) == truth_row["length"]
                assert (warcio_row["warc-record-id"]
                        == truth_row["warc-record-id"])
                assert (warcio_row["warc-target-uri"]
                        == truth_row["warc-target-uri"])

    def test_uncompressed_offsets_point_at_warc_magic(self, bundle):
        path = bundle["valid_uncompressed"]
        with open(path, "rb") as fh:
            data = fh.read()
        for row in fixtures.truth_index(path, gzip=False):
            assert data[row["offset"]:row["offset"] + 5] == b"WARC/"

    def test_uncompressed_length_excludes_blank_lines(self, bundle):
        path = bundle["valid_uncompressed"]
        rows = fixtures.truth_index(path, gzip=False)
        first, second = rows[0], rows[1]
        # Two CRLF blank lines sit between plain WARC records.
        assert second["offset"] - first["offset"] == first["length"] + 4

    def test_gzip_offsets_are_member_boundaries(self, bundle):
        path = bundle["valid_multimember_gzip"]
        members = list(rawreader.scan_gzip_members(path))
        rows = fixtures.truth_index(path, gzip=True)
        assert len(members) == len(rows) == 4
        for (member_offset, member_length, _), row in zip(members, rows):
            assert member_offset == row["offset"]
            assert member_length == row["length"]
        with open(path, "rb") as fh:
            data = fh.read()
        for row in rows:
            assert data[row["offset"]:row["offset"] + 2] == b"\x1f\x8b"

    def test_gzip_length_spans_full_member_including_trailer(self, bundle):
        path = bundle["valid_multimember_gzip"]
        rows = fixtures.truth_index(path, gzip=True)
        first, second = rows[0], rows[1]
        # For chunked gzip there are no inter-member blank lines.
        assert second["offset"] == first["offset"] + first["length"]

    def test_digests_round_trip_against_warc_headers(self, bundle):
        path = bundle["valid_multimember_gzip"]
        for obs in rawreader.scan_all_records(path, True):
            if obs.rec_type == "revisit":
                continue
            recomputed = obs.digest_with_declared_algo(
                obs.declared_payload_digest)
            assert recomputed["payload"] == obs.declared_payload_digest
            block = obs.digest_with_declared_algo(obs.declared_block_digest)
            assert block["block"] == obs.declared_block_digest


# ---------------------------------------------------------------------------
# Happy-path verification across formats
# ---------------------------------------------------------------------------

class TestHappyPath:
    @pytest.mark.parametrize("archive_key,index_key", [
        ("valid_uncompressed", "truth_index_plain"),
        ("valid_multimember_gzip", "truth_index_gzip"),
    ])
    def test_truthful_index_verifies(self, bundle, archive_key, index_key):
        verifier = SeekVerifier(bundle[archive_key])
        results = verifier.verify_all(load_index(bundle[index_key]))
        assert [r.status for r in results] == ["ok"] * len(results)
        assert summarize(results) == {
            "ok": len(results), "index_inconsistent": 0,
            "corruption": 0, "total": len(results)}

    def test_empty_body_record(self, bundle):
        path = bundle["valid_multimember_gzip"]
        entries = load_index(bundle["truth_index_gzip"])
        empty = next(entry for entry in entries
                     if entry.url == "http://example.com/empty")
        result = SeekVerifier(path).verify_entry(empty)
        assert result.ok
        assert result.fingerprint.payload_size == 0
        assert result.fingerprint.payload_sha256.startswith("sha256:")

    @pytest.mark.parametrize("consume", [1, 7, 64, 10_000, 1_000_000])
    def test_partial_payload_consumption(self, bundle, consume):
        path = bundle["valid_multimember_gzip"]
        verifier = SeekVerifier(path)
        for entry in load_index(bundle["truth_index_gzip"]):
            result = verifier.verify_partial_consumption(entry, consume)
            assert result.ok, result.error
            assert result.checks["partial_consume_did_not_bleed"]

    def test_partial_consumption_plain_archive(self, bundle):
        path = bundle["valid_uncompressed"]
        verifier = SeekVerifier(path, gzip=False)
        entries = load_index(bundle["truth_index_plain"])
        results = verifier.verify_all(entries, partial_consume=3)
        assert all(r.ok for r in results)

    def test_warcio_reseeks_to_indexed_record(self, bundle):
        path = bundle["valid_multimember_gzip"]
        rows = _load(bundle["truth_index_gzip"])
        verifier = SeekVerifier(path)
        for row in rows:
            observed = verifier._warcio_reseek(row["offset"])
            assert observed["record_id"] == row["warc-record-id"]
            assert observed["offset"] == row["offset"]
            assert observed["length"] == row["length"]

    def test_byte_level_block_and_payload_cross_checks(self, bundle):
        path = bundle["valid_multimember_gzip"]
        verifier = SeekVerifier(path)
        for result in verifier.verify_all(
                load_index(bundle["truth_index_gzip"])):
            for key in ("warcio_block_sha256_matches",
                        "warcio_block_size_matches",
                        "warcio_payload_sha256_matches",
                        "warcio_payload_size_matches"):
                assert result.checks[key] is True

    def test_payload_digest_detects_swapped_content(self, bundle, tmp_path):
        # A truthful index against an archive whose payload bytes were
        # rewritten must be reported as CONTENT corruption: the bytes fail the
        # WARC header's own digest, even though framing and the index are fine.
        src = bundle["valid_multimember_gzip"]
        import gzip as gz
        members = list(rawreader.scan_gzip_members(src))
        altered = tmp_path / "altered.warc.gz"
        with open(altered, "wb") as out:
            for index, (_, _, decompressed) in enumerate(members):
                if index == 0:
                    # This token only occurs inside the HTTP payload.
                    decompressed = decompressed.replace(
                        b"seekverify-payload", b"xEEKVERIFY-payload", 1)
                out.write(gz.compress(decompressed))
        verifier = SeekVerifier(str(altered), gzip=True)
        # Re-index the altered file: offsets stay truthful, but the WARC
        # digest header inside the rewritten member is now stale.
        altered_rows = fixtures.truth_index(str(altered), gzip=True)
        results = verifier.verify_all(load_index(altered_rows))
        assert isinstance(results[0].error, CorruptionError)
        assert "digest" in results[0].error.message

    def test_index_only_digest_error_distinct_from_corruption(self, bundle):
        # The archive is untouched; only the index digest line is wrong.
        results = _verifier(bundle).verify_all(
            load_index(bundle["bad_index_digest"]))
        bad = next(r for r in results if r.error is not None)
        assert isinstance(bad.error, IndexInconsistentError)
        mismatch = bad.error.detail["mismatches"][0]
        assert mismatch["check"] == "payload_digest"


# ---------------------------------------------------------------------------
# Index inconsistencies: the archive stays valid
# ---------------------------------------------------------------------------

class TestIndexInconsistencies:
    @pytest.mark.parametrize("index_key", [
        "bad_index_offset", "bad_index_length", "bad_index_id",
        "bad_index_url", "bad_index_digest",
    ])
    def test_tampered_index_is_index_error(self, bundle, index_key):
        verifier = _verifier(bundle)
        results = verifier.verify_all(load_index(bundle[index_key]))
        assert summarize(results)["index_inconsistent"] == 1
        assert summarize(results)["corruption"] == 0
        bad = next(r for r in results if r.error is not None)
        assert isinstance(bad.error, IndexInconsistentError)
        assert not isinstance(bad.error, CorruptionError)

    def test_offset_plus_one_lands_inside_member(self, bundle):
        path = bundle["valid_multimember_gzip"]
        rows = _load(bundle["truth_index_gzip"])
        entry = IndexEntry(rows[1]["offset"] + 1,
                           record_id=rows[1]["warc-record-id"])
        with pytest.raises(IndexInconsistentError) as excinfo:
            SeekVerifier(path).verify_entry(entry)
        detail = excinfo.value.detail
        # The healthy full scan is the evidence this is not file damage.
        assert detail["full_scan"]["records_scanned"] == 4
        boundary = detail["nearest_boundary"]
        assert boundary[0] == rows[1]["offset"]
        assert detail["warcio_at_offset"]["outcome"] == "parse_failed"

    def test_minus_one_offset_plain_archive(self, bundle):
        path = bundle["valid_uncompressed"]
        rows = _load(bundle["truth_index_plain"])
        entry = IndexEntry(rows[2]["offset"] - 1)
        with pytest.raises(IndexInconsistentError):
            SeekVerifier(path, gzip=False).verify_entry(entry)

    def test_length_tamper_detects_neighbour_overflow(self, bundle):
        path = bundle["valid_multimember_gzip"]
        rows = _load(bundle["truth_index_gzip"])
        entry = load_index(bundle["bad_index_length"])
        result = SeekVerifier(path).verify_all(entry)
        bad = next(r for r in result if r.error is not None)
        mismatch = bad.error.detail["mismatches"][0]
        assert mismatch["check"] == "physical_length"
        assert mismatch["index_or_oracle"] != mismatch["observed"]

    def test_index_file_is_never_modified(self, bundle):
        index_path = bundle["bad_index_offset"]
        with open(index_path, "rb") as fh:
            before = fh.read()
        SeekVerifier(bundle["valid_multimember_gzip"]).verify_all(
            load_index(index_path))
        with open(index_path, "rb") as fh:
            after = fh.read()
        assert before == after


# ---------------------------------------------------------------------------
# File corruption: the index stays truthful
# ---------------------------------------------------------------------------

class TestCorruption:
    @pytest.mark.parametrize("archive_key", ["corrupt_crc", "corrupt_truncated"])
    def test_damaged_archive_is_corruption(self, bundle, archive_key):
        path = bundle[archive_key]["path"]
        verifier = SeekVerifier(path, gzip=True)
        results = verifier.verify_all(
            load_index(bundle["truth_index_gzip"]))
        assert summarize(results)["corruption"] >= 1
        assert summarize(results)["index_inconsistent"] == 0
        assert all(r.error is None or isinstance(r.error, CorruptionError)
                   for r in results)

    def test_crc_failure_is_detectable_even_where_warcio_stumbles(self, bundle):
        path = bundle["corrupt_crc"]["path"]
        # The independent reader must flag the damaged member directly.
        with pytest.raises(CorruptionError) as excinfo:
            list(rawreader.scan_all_records(path, True))
        assert "CRC or deflate error" in excinfo.value.message

    def test_truncation_leaves_earlier_records_readable(self, bundle):
        path = bundle["corrupt_truncated"]["path"]
        verifier = SeekVerifier(path, gzip=True)
        results = verifier.verify_all(
            load_index(bundle["truth_index_gzip"]))
        # First record survives; the cut member and everything after fail.
        assert results[0].ok
        assert all(not r.ok for r in results[1:])
        assert isinstance(results[1].error, CorruptionError)

    def test_payload_corruption_keeps_framing_but_fails_digest(self, bundle):
        path = bundle["corrupt_payload"]["path"]
        # Framing scan succeeds: the altered member still decompresses.
        assert sum(1 for _ in rawreader.scan_all_records(path, True)) == 4
        results = SeekVerifier(path, gzip=True).verify_all(
            load_index(bundle["truth_index_corrupt_payload"]))
        assert summarize(results)["corruption"] == 1
        assert isinstance(results[0].error, CorruptionError)

    def test_non_chunked_single_member_gzip(self, bundle):
        path = bundle["non_chunked_single_member_gzip"]
        with pytest.raises(CorruptionError) as excinfo:
            list(rawreader.scan_all_records(path, True))
        assert "non-chunked" in excinfo.value.message
        # warcio itself must reject the same shape.
        from warcio.archiveiterator import ArchiveIterator
        from warcio.exceptions import ArchiveLoadFailed
        with pytest.raises(ArchiveLoadFailed):
            with open(path, "rb") as fh:
                list(ArchiveIterator(fh))

    def test_damaged_neighbour_does_not_poison_good_record(self, bundle):
        path = bundle["corrupt_truncated"]["path"]
        verifier = SeekVerifier(path, gzip=True)
        first = load_index(bundle["truth_index_gzip"])[0]
        result = verifier.verify_entry(first)
        assert result.ok
        assert result.checks.get(
            "warcio_next_offset_skipped_damaged_neighbour") is True


# ---------------------------------------------------------------------------
# Reporting and CLI
# ---------------------------------------------------------------------------

class TestReporting:
    def test_exit_codes(self, bundle):
        good = _verifier(bundle).verify_all(
            load_index(bundle["truth_index_gzip"]))
        bad_index = _verifier(bundle).verify_all(
            load_index(bundle["bad_index_id"]))
        corrupt = SeekVerifier(
            bundle["corrupt_crc"]["path"], gzip=True).verify_all(
            load_index(bundle["truth_index_gzip"]))
        assert exit_code(good) == 0
        assert exit_code(bad_index) == 1
        assert exit_code(corrupt) == 2

    def test_cli_verify_and_scan(self, bundle):
        verify = subprocess.run(
            [sys.executable, "-m", "seekverify", "verify",
             bundle["valid_multimember_gzip"],
             bundle["truth_index_gzip"]],
            capture_output=True, text=True, check=False)
        assert verify.returncode == 0
        assert "4 ok" in verify.stdout

        scan = subprocess.run(
            [sys.executable, "-m", "seekverify", "scan",
             bundle["valid_multimember_gzip"]],
            capture_output=True, text=True, check=False)
        assert scan.returncode == 0
        assert len(scan.stdout.strip().splitlines()) == 4

    def test_cli_reports_corruption_exit_code(self, bundle):
        proc = subprocess.run(
            [sys.executable, "-m", "seekverify", "verify",
             bundle["corrupt_truncated"]["path"],
             bundle["truth_index_gzip"], "--output-format", "json"],
            capture_output=True, text=True, check=False)
        assert proc.returncode == 2
        payload = [json.loads(line) for line in proc.stdout.splitlines()
                   if line.strip()]
        assert payload[-1]["summary"]["corruption"] == 3

    def test_load_index_accepts_aliases_and_dicts(self):
        entries = load_index([
            {"offset": "10", "length": "20", "id": "<urn:x>", "uri": "u",
             "cl": "5", "digest": "sha1:AAA"},
        ])
        entry = entries[0]
        assert entry.offset == 10 and entry.length == 20
        assert entry.record_id == "<urn:x>" and entry.url == "u"
        assert entry.content_length == 5
        assert entry.payload_digest == "sha1:AAA"
