from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from unittest import mock

import pytest

from queuelib import FifoDiskQueue, LifoDiskQueue
from queuelib.recovery import (
    ByteRegion,
    IssueKind,
    QueueKind,
    RecordState,
    inspect_queue,
    recover_queue,
    render_report,
)
from queuelib.recovery import main as recovery_main

SIZE_FORMAT = ">L"
SIZE_SIZE = struct.calcsize(SIZE_FORMAT)


def snapshot(path: Path) -> dict[str, str]:
    """Hash every byte beneath a queue path to prove it was not modified."""
    result = {}
    if path.is_file():
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
    for entry in sorted(path.rglob("*")):
        if entry.is_file():
            result[str(entry.relative_to(path))] = hashlib.sha256(
                entry.read_bytes()
            ).hexdigest()
    return result


def build_fifo(path: Path, items: list[bytes], chunksize: int = 100000) -> None:
    queue = FifoDiskQueue(path, chunksize=chunksize)
    for item in items:
        queue.push(item)
    queue.close()


def drain_fifo(path: Path) -> list[bytes]:
    queue = FifoDiskQueue(path)
    items = []
    while True:
        item = queue.pop()
        if item is None:
            break
        items.append(item)
    queue.close()
    return items


def drain_lifo(path: Path) -> list[bytes]:
    queue = LifoDiskQueue(path)
    items = []
    while True:
        item = queue.pop()
        if item is None:
            break
        items.append(item)
    queue.close()
    return items


def lifo_bytes(payloads: list[bytes], claimed_size: int | None = None) -> bytes:
    claimed = len(payloads) if claimed_size is None else claimed_size
    content = struct.pack(SIZE_FORMAT, claimed)
    for payload in payloads:
        content += payload + struct.pack(SIZE_FORMAT, len(payload))
    return content


# ---------------------------------------------------------------------------
# Healthy queues: inspection is fully non-destructive
# ---------------------------------------------------------------------------


class TestHealthyFifo:
    def test_report_is_consistent(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"bb", b"ccc"])
        report = inspect_queue(source)
        assert report.ok
        assert report.kind is QueueKind.FIFO
        assert len(report.records) == 3
        assert all(r.state is RecordState.REFERENCED for r in report.records)
        assert report.truncated_regions == ()
        assert report.missing_blocks == ()

    def test_inspect_does_not_touch_a_byte(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b", b"c", b"d"])
        before = snapshot(source)
        for _ in range(3):
            inspect_queue(source)
        assert snapshot(source) == before

    def test_recover_reproduces_fifo_order(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        items = [b"one", b"two", b"three", b"four"]
        build_fifo(source, items)
        before = snapshot(source)
        result = recover_queue(source, tmp_path / "restored")
        assert result.exported == 4
        assert drain_fifo(tmp_path / "restored") == items
        assert snapshot(source) == before

    def test_partially_consumed_queue(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        queue = FifoDiskQueue(source)
        for item in [b"a", b"b", b"c", b"d"]:
            queue.push(item)
        queue.pop()
        queue.pop()
        queue.close()
        report = inspect_queue(source)
        assert report.ok
        assert len(report.recoverable_records) == 2
        result = recover_queue(source, tmp_path / "restored")
        assert drain_fifo(tmp_path / "restored") == [b"c", b"d"]
        assert result.exported == 2


class TestHealthyLifo:
    def test_report_is_consistent(self, tmp_path: Path) -> None:
        source = tmp_path / "queue.bin"
        source.write_bytes(lifo_bytes([b"a", b"bb", b"ccc"]))
        report = inspect_queue(source)
        assert report.ok
        assert report.kind is QueueKind.LIFO
        assert [r.length for r in report.records] == [1, 2, 3]
        assert all(r.state is RecordState.REFERENCED for r in report.records)

    def test_inspect_does_not_touch_a_byte(self, tmp_path: Path) -> None:
        source = tmp_path / "queue.bin"
        source.write_bytes(lifo_bytes([b"a", b"b", b"c"]))
        before = snapshot(source)
        inspect_queue(source)
        assert snapshot(source) == before

    def test_recover_reproduces_lifo_order(self, tmp_path: Path) -> None:
        source = tmp_path / "queue.bin"
        source.write_bytes(lifo_bytes([b"a", b"b", b"c"]))
        before = snapshot(source)
        result = recover_queue(source, tmp_path / "restored.bin")
        assert result.exported == 3
        assert drain_lifo(tmp_path / "restored.bin") == [b"c", b"b", b"a"]
        assert snapshot(source) == before


# ---------------------------------------------------------------------------
# FIFO failure modes
# ---------------------------------------------------------------------------


class TestUnreferencedTail:
    """Complete records pushed after info.json was last written."""

    def test_unreferenced_complete_records_are_distinguished(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b", b"c"])
        queue = FifoDiskQueue(source)
        queue.push(b"d")
        queue.push(b"e")
        del queue  # crash before close(): info.json stays stale

        report = inspect_queue(source)
        assert not report.ok
        kinds = {issue.kind for issue in report.issues}
        assert IssueKind.METADATA_MISMATCH in kinds
        assert len(report.referenced_records) == 3
        assert len(report.unreferenced_records) == 2
        assert report.truncated_regions == ()

    def test_unreferenced_records_are_salvaged_in_order(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b", b"c"])
        queue = FifoDiskQueue(source)
        queue.push(b"d")
        queue.push(b"e")
        del queue
        before = snapshot(source)

        result = recover_queue(source, tmp_path / "restored")
        assert result.exported == 5
        assert drain_fifo(tmp_path / "restored") == [
            b"a",
            b"b",
            b"c",
            b"d",
            b"e",
        ]
        assert snapshot(source) == before


class TestTruncatedFifo:
    def test_truncated_payload_is_reported_not_guessed(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b", b"c"])
        chunk = source / "q00000"
        chunk.write_bytes(chunk.read_bytes() + struct.pack(SIZE_FORMAT, 5) + b"xy")

        report = inspect_queue(source)
        assert len(report.records) == 3
        assert len(report.truncated_regions) == 1
        region = report.truncated_regions[0]
        assert region.end == chunk.stat().st_size
        assert region.length == SIZE_SIZE + 2
        assert any(issue.kind is IssueKind.TRUNCATED_RECORD for issue in report.issues)

        result = recover_queue(source, tmp_path / "restored")
        assert drain_fifo(tmp_path / "restored") == [b"a", b"b", b"c"]
        assert result.exported == 3

    def test_truncated_length_header(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        chunk = source / "q00000"
        chunk.write_bytes(chunk.read_bytes() + b"\x00\x01")

        report = inspect_queue(source)
        assert len(report.records) == 1
        assert report.truncated_regions[0].length == 2
        assert drain_fifo(recover_queue(source, tmp_path / "out").destination) == [b"a"]

    def test_source_evidence_preserved_after_truncation(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b"])
        chunk = source / "q00000"
        chunk.write_bytes(chunk.read_bytes() + b"\x00\x00\x00")
        before = snapshot(source)
        inspect_queue(source)
        recover_queue(source, tmp_path / "restored")
        assert snapshot(source) == before


class TestMissingBlocks:
    def test_gap_between_chunks_is_listed(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        queue = FifoDiskQueue(source, chunksize=2)
        for item in [b"a", b"b", b"c", b"d", b"e", b"f"]:
            queue.push(item)
        queue.pop()
        queue.pop()
        queue.close()
        surviving = sorted(p.name for p in source.glob("q*"))
        assert "q00001" in surviving
        assert "q00002" in surviving

        (source / "q00002").unlink()
        report = inspect_queue(source)
        missing = {block.number for block in report.missing_blocks}
        assert 2 in missing
        assert any(issue.kind is IssueKind.MISSING_BLOCK for issue in report.issues)
        surviving_records = drain_fifo(
            recover_queue(source, tmp_path / "restored").destination
        )
        assert surviving_records == [b"c", b"d"]

    def test_metadata_referenced_missing_block(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b"])
        (source / "q00000").unlink()
        report = inspect_queue(source)
        assert report.missing_blocks[0].referenced_by_metadata
        assert any(issue.kind is IssueKind.MISSING_BLOCK for issue in report.issues)


class TestMetadataProblems:
    def test_missing_info_json_marks_everything_unreferenced(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b"])
        (source / "info.json").unlink()
        report = inspect_queue(source)
        assert any(issue.kind is IssueKind.METADATA_MISSING for issue in report.issues)
        assert all(r.state is RecordState.UNREFERENCED for r in report.records)
        assert drain_fifo(recover_queue(source, tmp_path / "restored").destination) == [
            b"a",
            b"b",
        ]

    def test_invalid_info_json(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        (source / "info.json").write_text("{not json")
        report = inspect_queue(source)
        assert any(issue.kind is IssueKind.METADATA_INVALID for issue in report.issues)

    def test_tail_offset_past_end_of_chunk(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b"])
        info = json.loads((source / "info.json").read_text())
        info["tail"] = [0, 9, 9999]
        (source / "info.json").write_text(json.dumps(info))
        report = inspect_queue(source)
        assert any(issue.kind is IssueKind.BOUNDARY_MISMATCH for issue in report.issues)

    def test_tail_offset_inside_a_record(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b", b"c"])
        info = json.loads((source / "info.json").read_text())
        info["tail"] = [0, 0, 3]
        (source / "info.json").write_text(json.dumps(info))
        report = inspect_queue(source)
        assert any(issue.kind is IssueKind.BOUNDARY_MISMATCH for issue in report.issues)

    def test_tail_count_mismatch_keeps_boundary_valid(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b", b"c"])
        info = json.loads((source / "info.json").read_text())
        info["tail"] = [0, 9, 5]
        (source / "info.json").write_text(json.dumps(info))
        report = inspect_queue(source)
        assert not any(
            issue.kind is IssueKind.BOUNDARY_MISMATCH for issue in report.issues
        )
        assert any(issue.kind is IssueKind.METADATA_MISMATCH for issue in report.issues)
        assert report.records[0].state is RecordState.CONSUMED

    def test_dangling_old_chunk_is_consumed_and_flagged(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b", b"c", b"d"], chunksize=2)
        info = json.loads((source / "info.json").read_text())
        info["tail"] = [1, 0, 0]
        (source / "info.json").write_text(json.dumps(info))

        report = inspect_queue(source)
        assert any(issue.kind is IssueKind.DANGLING_BLOCK for issue in report.issues)
        assert all(
            record.state is RecordState.CONSUMED
            for record in report.records
            if record.chunk == 0
        )
        assert drain_fifo(recover_queue(source, tmp_path / "restored").destination) == [
            b"c",
            b"d",
        ]

    def test_unexpected_subdirectory_is_reported(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        (source / "subdir").mkdir()
        report = inspect_queue(source)
        assert any(issue.kind is IssueKind.UNEXPECTED_FILE for issue in report.issues)

    def test_report_renders_missing_blocks(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b", b"c", b"d"], chunksize=2)
        (source / "q00001").unlink()
        text = render_report(inspect_queue(source))
        assert "Missing blocks:" in text
        assert "sequence gap" in text

    def test_unexpected_file_is_reported(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        (source / "notes.txt").write_text("evidence")
        report = inspect_queue(source)
        assert any(issue.kind is IssueKind.UNEXPECTED_FILE for issue in report.issues)
        assert (source / "notes.txt").read_text() == "evidence"


class TestCrossChunkOrder:
    def test_recovery_orders_by_chunk_then_offset(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        items = [b"r0", b"r1", b"r2", b"r3", b"r4", b"r5", b"r6"]
        queue = FifoDiskQueue(source, chunksize=3)
        for item in items:
            queue.push(item)
        queue.close()
        report = inspect_queue(source)
        ordered = report.pop_order()
        assert [r.chunk for r in ordered] == [0, 0, 0, 1, 1, 1, 2]
        assert (
            drain_fifo(recover_queue(source, tmp_path / "restored").destination)
            == items
        )

    def test_truncated_middle_chunk_does_not_poison_later_chunks(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "queue"
        items = [b"r0", b"r1", b"r2", b"r3", b"r4", b"r5"]
        build_fifo(source, items, chunksize=2)
        middle = source / "q00001"
        middle.write_bytes(middle.read_bytes() + struct.pack(SIZE_FORMAT, 8) + b"cut")

        report = inspect_queue(source)
        truncated_chunks = {
            Path(region.path).name for region in report.truncated_regions
        }
        assert truncated_chunks == {"q00001"}
        assert len(report.records) == 6
        assert (
            drain_fifo(recover_queue(source, tmp_path / "restored").destination)
            == items
        )

    def test_never_closed_queue_is_all_unreferenced_but_salvageable(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "queue"
        queue = FifoDiskQueue(source, chunksize=2)
        for item in [b"r0", b"r1", b"r2", b"r3", b"r4"]:
            queue.push(item)
        del queue  # power loss before the first close(): no info.json exists
        assert not (source / "info.json").exists()

        report = inspect_queue(source)
        assert any(issue.kind is IssueKind.METADATA_MISSING for issue in report.issues)
        assert len(report.unreferenced_records) == 5
        assert drain_fifo(recover_queue(source, tmp_path / "restored").destination) == [
            b"r0",
            b"r1",
            b"r2",
            b"r3",
            b"r4",
        ]

    def test_empty_payloads_round_trip(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"", b"x", b""])
        report = inspect_queue(source)
        assert report.ok
        assert [r.length for r in report.records] == [0, 1, 0]
        assert drain_fifo(recover_queue(source, tmp_path / "restored").destination) == [
            b"",
            b"x",
            b"",
        ]

    def test_recover_into_lifo_keeps_pop_order(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        items = [b"r0", b"r1", b"r2"]
        build_fifo(source, items)
        result = recover_queue(source, tmp_path / "restored.bin", queue_kind="lifo")
        assert result.queue_kind is QueueKind.LIFO
        assert drain_lifo(tmp_path / "restored.bin") == items


# ---------------------------------------------------------------------------
# LIFO failure modes
# ---------------------------------------------------------------------------


class TestTruncatedLifo:
    def test_partial_trailer_salvages_complete_prefix(self, tmp_path: Path) -> None:
        source = tmp_path / "queue.bin"
        valid = lifo_bytes([b"aa", b"bbb"], claimed_size=2)
        source.write_bytes(valid + b"x" + b"\x00\x00")  # partial new frame

        report = inspect_queue(source)
        assert len(report.records) == 2
        assert report.truncated_regions
        assert any(issue.kind is IssueKind.TRUNCATED_RECORD for issue in report.issues)
        result = recover_queue(source, tmp_path / "restored.bin")
        assert drain_lifo(tmp_path / "restored.bin") == [b"bbb", b"aa"]
        assert result.exported == 2

    def test_undecodable_file_exports_nothing(self, tmp_path: Path) -> None:
        source = tmp_path / "queue.bin"
        source.write_bytes(struct.pack(SIZE_FORMAT, 9) + b"garbage without frames")
        report = inspect_queue(source)
        assert report.records == ()
        assert report.truncated_regions
        with pytest.raises(ValueError, match="No complete record"):
            recover_queue(source, tmp_path / "restored.bin")
        assert not (tmp_path / "restored.bin").exists()

    def test_file_shorter_than_header(self, tmp_path: Path) -> None:
        source = tmp_path / "queue.bin"
        source.write_bytes(b"\x01\x02")
        report = inspect_queue(source)
        assert report.records == ()
        assert report.truncated_regions[0].length == 2

    def test_few_stray_bytes_after_header_are_corrupt_region(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "queue.bin"
        source.write_bytes(struct.pack(SIZE_FORMAT, 0) + b"\x07")
        report = inspect_queue(source)
        assert report.records == ()
        assert report.truncated_regions == (ByteRegion(str(source), 4, 5),)
        with pytest.raises(ValueError, match="No complete record"):
            recover_queue(source, tmp_path / "restored.bin")

    def test_stale_size_header_unreferenced_tail(self, tmp_path: Path) -> None:
        source = tmp_path / "queue.bin"
        source.write_bytes(lifo_bytes([b"a", b"b", b"c"], claimed_size=2))
        report = inspect_queue(source)
        assert len(report.referenced_records) == 2
        assert len(report.unreferenced_records) == 1
        assert any(issue.kind is IssueKind.METADATA_MISMATCH for issue in report.issues)
        result = recover_queue(source, tmp_path / "restored.bin")
        assert drain_lifo(tmp_path / "restored.bin") == [b"c", b"b", b"a"]
        assert result.exported == 3


# ---------------------------------------------------------------------------
# Destination guardrails
# ---------------------------------------------------------------------------


class TestGuardrails:
    def test_destination_must_differ_from_source(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        with pytest.raises(ValueError, match="differ"):
            recover_queue(source, source)

    def test_destination_inside_source_rejected(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        with pytest.raises(ValueError, match="inside the source"):
            recover_queue(source, source / "nested")

    def test_source_inside_destination_rejected(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        with pytest.raises(ValueError, match="inside the destination"):
            recover_queue(source, tmp_path)

    def test_existing_destination_rejected(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        target = tmp_path / "target"
        build_fifo(source, [b"a"])
        target.mkdir()
        with pytest.raises(ValueError, match="already exists"):
            recover_queue(source, target)

    def test_missing_source_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            inspect_queue(tmp_path / "absent")

    def test_negative_metadata_is_invalid(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        info = json.loads((source / "info.json").read_text())
        info["tail"] = [-1, 0, 0]
        (source / "info.json").write_text(json.dumps(info))
        report = inspect_queue(source)
        assert any(issue.kind is IssueKind.METADATA_INVALID for issue in report.issues)

    def test_failed_fifo_export_leaves_no_destination(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        target = tmp_path / "restored"
        build_fifo(source, [b"a", b"b"])
        with (
            mock.patch.object(FifoDiskQueue, "push", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            recover_queue(source, target)
        assert not target.exists()

    def test_failed_lifo_export_leaves_no_destination(self, tmp_path: Path) -> None:
        source = tmp_path / "queue.bin"
        target = tmp_path / "restored.bin"
        source.write_bytes(lifo_bytes([b"a", b"b"]))
        with (
            mock.patch.object(LifoDiskQueue, "push", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            recover_queue(source, target)
        assert not target.exists()


# ---------------------------------------------------------------------------
# Rendering and command line
# ---------------------------------------------------------------------------


class TestRendering:
    def test_healthy_report_text(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b"])
        text = render_report(inspect_queue(source))
        assert "Status: OK" in text
        assert "q00000" in text
        assert "recoverable: 2" in text

    def test_damaged_report_lists_uncertain_region(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        chunk = source / "q00000"
        chunk.write_bytes(chunk.read_bytes() + b"\x00\x00")
        text = render_report(inspect_queue(source))
        assert "Status: INCONSISTENT" in text
        assert "Uncertain regions" in text
        assert "truncated-record" in text


class TestCli:
    def test_inspect_healthy_exit_code_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        assert recovery_main(["inspect", str(source)]) == 0
        assert "Status: OK" in capsys.readouterr().out

    def test_inspect_damaged_exit_code_one(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        (source / "info.json").unlink()
        assert recovery_main(["inspect", str(source)]) == 1

    def test_recover_subcommand(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a", b"b"])
        target = tmp_path / "restored"
        assert recovery_main(["recover", str(source), str(target)]) == 0
        assert drain_fifo(target) == [b"a", b"b"]

    def test_recover_failure_exit_code_one(self, tmp_path: Path) -> None:
        source = tmp_path / "queue"
        build_fifo(source, [b"a"])
        assert recovery_main(["recover", str(source), str(source)]) == 1

    def test_inspect_missing_path_exit_code_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert recovery_main(["inspect", str(tmp_path / "absent")]) == 1
        assert "inspection failed" in capsys.readouterr().out
