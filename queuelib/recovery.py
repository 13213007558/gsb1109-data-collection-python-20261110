"""Offline, read-only inspection and recovery for crashed disk queues.

``FifoDiskQueue`` and ``LifoDiskQueue`` keep their bookkeeping in memory until
``close()``. A power loss or a partial write can therefore leave chunk files
that no longer match ``info.json`` (FIFO) or the size header (LIFO), possibly
with a half-written record at the tail.

The tools in this module never write to the source queue. :func:`inspect_queue`
opens every file read-only and reports only what it can prove, while
:func:`recover_queue` copies records whose framing is fully intact into a
brand new queue. Bytes that cannot be decoded into a complete record are never
guessed at: their exact byte ranges are reported as uncertain regions instead.
"""

from __future__ import annotations

import argparse
import json
import struct
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Literal, cast

from queuelib.queue import FifoDiskQueue, LifoDiskQueue

SIZE_FORMAT = ">L"
SIZE_SIZE = struct.calcsize(SIZE_FORMAT)
_CHUNK_DIGITS = 5
_INFO_NAME = "info.json"


class QueueKind(str, Enum):
    """On-disk queue layout understood by this module."""

    FIFO = "fifo"
    LIFO = "lifo"


class RecordState(str, Enum):
    """How a fully decodable record relates to the queue metadata."""

    REFERENCED = "referenced"
    """Complete record that the metadata accounts for."""

    UNREFERENCED = "unreferenced"
    """Complete tail record written after the metadata was last saved."""

    CONSUMED = "consumed"
    """Complete record lying before the metadata read position, already popped."""


class IssueKind(str, Enum):
    """Category of an inconsistency found during inspection."""

    METADATA_MISSING = "metadata-missing"
    METADATA_INVALID = "metadata-invalid"
    METADATA_MISMATCH = "metadata-mismatch"
    MISSING_BLOCK = "missing-block"
    TRUNCATED_RECORD = "truncated-record"
    BOUNDARY_MISMATCH = "boundary-mismatch"
    DANGLING_BLOCK = "dangling-block"
    UNEXPECTED_FILE = "unexpected-file"
    CORRUPT_REGION = "corrupt-region"


@dataclass(frozen=True)
class Issue:
    """A single inconsistency, with a human-readable explanation."""

    kind: IssueKind
    message: str
    path: str | None = None


@dataclass(frozen=True)
class ByteRegion:
    """A half-open byte range ``[start, end)`` that could not be decoded."""

    path: str
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class MissingBlock:
    """A chunk file that references imply should exist but is absent."""

    number: int
    name: str
    referenced_by_metadata: bool


@dataclass(frozen=True)
class ChunkInfo:
    """Summary of one FIFO chunk file scan."""

    number: int
    name: str
    size: int
    record_count: int
    clean_end: bool


@dataclass(frozen=True)
class Record:
    """A record whose length header and full payload are present on disk.

    ``offset`` points at the length header (FIFO) or the first payload byte
    (LIFO); ``length`` is the payload length in bytes.
    """

    state: RecordState
    chunk: int | None
    offset: int
    length: int


@dataclass(frozen=True)
class QueueReport:
    """Result of a read-only queue inspection."""

    kind: QueueKind
    source: Path
    metadata: dict[str, object] | None
    chunks: tuple[ChunkInfo, ...]
    records: tuple[Record, ...]
    issues: tuple[Issue, ...]
    truncated_regions: tuple[ByteRegion, ...]
    missing_blocks: tuple[MissingBlock, ...]

    @property
    def ok(self) -> bool:
        """Whether the queue is internally consistent."""
        return not self.issues

    @property
    def referenced_records(self) -> tuple[Record, ...]:
        return tuple(r for r in self.records if r.state is RecordState.REFERENCED)

    @property
    def unreferenced_records(self) -> tuple[Record, ...]:
        return tuple(r for r in self.records if r.state is RecordState.UNREFERENCED)

    @property
    def consumed_records(self) -> tuple[Record, ...]:
        return tuple(r for r in self.records if r.state is RecordState.CONSUMED)

    @property
    def recoverable_records(self) -> tuple[Record, ...]:
        """Complete records that still represent queued tasks.

        Consumed records are excluded; referenced and unreferenced complete
        records are both kept, each keeping its own state for the report.
        """
        return tuple(r for r in self.records if r.state is not RecordState.CONSUMED)

    def pop_order(self) -> list[Record]:
        """Recoverable records in the order the damaged queue would pop them."""
        recoverable = list(self.recoverable_records)
        if self.kind is QueueKind.LIFO:
            recoverable.reverse()
            return recoverable
        return sorted(recoverable, key=lambda r: (r.chunk or 0, r.offset))


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of exporting recoverable records into a new queue."""

    report: QueueReport
    destination: Path
    queue_kind: QueueKind
    exported: int


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def inspect_queue(path: str | Path) -> QueueReport:
    """Inspect a disk queue without modifying anything beneath ``path``.

    Directories are inspected as ``FifoDiskQueue`` storage and regular files
    as ``LifoDiskQueue`` storage. Every file is opened read-only.
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Queue path does not exist: {source}")
    if source.is_dir():
        return _inspect_fifo(source)
    return _inspect_lifo(source)


def _inspect_fifo(directory: Path) -> QueueReport:
    issues: list[Issue] = []
    chunk_paths = _find_fifo_chunks(directory, issues)
    chunks: list[ChunkInfo] = []
    scanned: list[tuple[int, list[tuple[int, int]]]] = []
    truncated: list[ByteRegion] = []

    for number, chunk_path in chunk_paths:
        frames, region = _scan_fifo_chunk(chunk_path)
        chunks.append(
            ChunkInfo(
                number=number,
                name=chunk_path.name,
                size=chunk_path.stat().st_size,
                record_count=len(frames),
                clean_end=region is None,
            )
        )
        scanned.append((number, frames))
        if region is not None:
            truncated.append(region)
            issues.append(
                Issue(
                    IssueKind.TRUNCATED_RECORD,
                    (
                        f"Chunk {chunk_path.name} ends with {region.length} "
                        f"undecodable byte(s) at offsets "
                        f"[{region.start}, {region.end}); no record is guessed "
                        "from them"
                    ),
                    str(chunk_path),
                )
            )

    present_numbers = [number for number, _ in chunk_paths]
    missing = _missing_fifo_blocks(directory, present_numbers, issues)
    metadata = _load_fifo_metadata(directory, issues)
    if metadata is None:
        records = [
            Record(RecordState.UNREFERENCED, number, offset, length)
            for number, frames in scanned
            for offset, length in frames
        ]
    else:
        records = _reconcile_fifo(directory, metadata, scanned, missing, issues)

    return QueueReport(
        kind=QueueKind.FIFO,
        source=directory,
        metadata=metadata,
        chunks=tuple(chunks),
        records=tuple(records),
        issues=tuple(issues),
        truncated_regions=tuple(truncated),
        missing_blocks=tuple(missing),
    )


def _find_fifo_chunks(directory: Path, issues: list[Issue]) -> list[tuple[int, Path]]:
    chunks: list[tuple[int, Path]] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            issues.append(
                Issue(
                    IssueKind.UNEXPECTED_FILE,
                    "Unexpected non-file entry in the queue directory",
                    str(entry),
                )
            )
            continue
        digits = entry.name[1:] if entry.name.startswith("q") else ""
        if entry.name.startswith("q") and digits.isdigit():
            chunks.append((int(digits), entry))
        elif entry.name != _INFO_NAME:
            issues.append(
                Issue(
                    IssueKind.UNEXPECTED_FILE,
                    f"Unexpected file that is neither a chunk nor {_INFO_NAME}",
                    str(entry),
                )
            )
    chunks.sort(key=lambda item: item[0])
    return chunks


def _scan_fifo_chunk(
    chunk_path: Path,
) -> tuple[list[tuple[int, int]], ByteRegion | None]:
    """Parse one chunk strictly, frame after frame.

    Returns ``(frames, region)`` where frames are ``(header_offset, length)``
    pairs and ``region`` is the undecodable tail when the file ends
    mid-record. No resynchronisation is attempted: after a truncated frame the
    remaining bytes are reported and never interpreted.
    """
    frames: list[tuple[int, int]] = []
    size = chunk_path.stat().st_size
    offset = 0
    with chunk_path.open("rb") as handle:
        while offset < size:
            remaining = size - offset
            if remaining < SIZE_SIZE:
                return frames, ByteRegion(str(chunk_path), offset, size)
            header = handle.read(SIZE_SIZE)
            (payload_length,) = struct.unpack(SIZE_FORMAT, header)
            if remaining - SIZE_SIZE < payload_length:
                return frames, ByteRegion(str(chunk_path), offset, size)
            handle.seek(payload_length, 1)
            frames.append((offset, payload_length))
            offset += SIZE_SIZE + payload_length
    return frames, None


def _missing_fifo_blocks(
    directory: Path,
    present_numbers: list[int],
    issues: list[Issue],
) -> list[MissingBlock]:
    missing: list[MissingBlock] = []
    if len(present_numbers) < 2:
        return missing
    for number in range(present_numbers[0], present_numbers[-1] + 1):
        if number not in present_numbers:
            name = _chunk_name(number)
            missing.append(MissingBlock(number, name, referenced_by_metadata=False))
            issues.append(
                Issue(
                    IssueKind.MISSING_BLOCK,
                    f"Chunk {name} is absent between two surviving chunks; "
                    "records it held cannot be accounted for",
                    str(directory / name),
                )
            )
    return missing


def _load_fifo_metadata(
    directory: Path, issues: list[Issue]
) -> dict[str, object] | None:
    info_path = directory / _INFO_NAME
    if not info_path.exists():
        issues.append(
            Issue(
                IssueKind.METADATA_MISSING,
                f"{_INFO_NAME} is absent; every complete record is reported as "
                "unreferenced",
                str(info_path),
            )
        )
        return None
    try:
        raw = json.loads(info_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        issues.append(
            Issue(
                IssueKind.METADATA_INVALID,
                f"{_INFO_NAME} cannot be parsed: {exc}",
                str(info_path),
            )
        )
        return None
    if not isinstance(raw, dict) or not _valid_fifo_metadata(raw):
        issues.append(
            Issue(
                IssueKind.METADATA_INVALID,
                f"{_INFO_NAME} is missing one of chunksize/size/tail/head or "
                "uses the wrong field types",
                str(info_path),
            )
        )
        return None
    return cast("dict[str, object]", raw)


def _valid_fifo_metadata(metadata: dict[str, object]) -> bool:
    tail = metadata.get("tail")
    head = metadata.get("head")
    return (
        isinstance(metadata.get("chunksize"), int)
        and isinstance(metadata.get("size"), int)
        and isinstance(tail, list)
        and len(tail) == 3
        and all(isinstance(value, int) and value >= 0 for value in tail)
        and isinstance(head, list)
        and len(head) == 2
        and all(isinstance(value, int) and value >= 0 for value in head)
        and cast("int", metadata["chunksize"]) > 0
    )


def _reconcile_fifo(
    directory: Path,
    metadata: dict[str, object],
    scanned: list[tuple[int, list[tuple[int, int]]]],
    missing: list[MissingBlock],
    issues: list[Issue],
) -> list[Record]:
    tail_num, tail_count, tail_offset = cast("list[int]", metadata["tail"])
    head_num, head_pos = cast("list[int]", metadata["head"])
    chunk_numbers = {number for number, _ in scanned}
    chunk_sizes = {
        number: (directory / _chunk_name(number)).stat().st_size
        for number, _ in scanned
    }

    for referenced in (tail_num, head_num):
        if referenced not in chunk_numbers and not any(
            block.number == referenced for block in missing
        ):
            name = _chunk_name(referenced)
            missing.append(MissingBlock(referenced, name, referenced_by_metadata=True))
            issues.append(
                Issue(
                    IssueKind.MISSING_BLOCK,
                    f"Metadata references chunk {name}, which is absent",
                    str(directory / name),
                )
            )

    boundary_ok = True
    if tail_num in chunk_sizes:
        boundary_ok = _check_tail_boundary(
            directory,
            tail_num,
            tail_count,
            tail_offset,
            dict(scanned),
            chunk_sizes[tail_num],
            issues,
        )

    records: list[Record] = []
    live_count = 0
    dangling_reported: set[int] = set()
    for number, frames in scanned:
        for index, (offset, length) in enumerate(frames):
            if number < tail_num:
                state = RecordState.CONSUMED
                if number not in dangling_reported:
                    dangling_reported.add(number)
                    issues.append(
                        Issue(
                            IssueKind.DANGLING_BLOCK,
                            f"Chunk {_chunk_name(number)} predates the metadata "
                            f"tail chunk {_chunk_name(tail_num)} but was not "
                            "deleted; its records are already consumed",
                            str(directory / _chunk_name(number)),
                        )
                    )
            elif number == tail_num and boundary_ok and offset < tail_offset:
                state = RecordState.CONSUMED
            else:
                live_count += 1
                if number < head_num or (number == head_num and index < head_pos):
                    state = RecordState.REFERENCED
                else:
                    state = RecordState.UNREFERENCED
            records.append(Record(state, number, offset, length))

    if cast("int", metadata["size"]) != live_count:
        issues.append(
            Issue(
                IssueKind.METADATA_MISMATCH,
                f"info.json reports size={metadata['size']} but {live_count} "
                "complete live record(s) are provable from the chunk files",
                str(directory / _INFO_NAME),
            )
        )

    if head_num in chunk_numbers:
        head_records = dict(scanned)[head_num]
        if len(head_records) != head_pos:
            issues.append(
                Issue(
                    IssueKind.METADATA_MISMATCH,
                    f"info.json head position claims {head_pos} record(s) in "
                    f"{_chunk_name(head_num)} but the chunk holds "
                    f"{len(head_records)} complete record(s)",
                    str(directory / _INFO_NAME),
                )
            )
    return records


def _chunk_name(number: int) -> str:
    return f"q{number:0{_CHUNK_DIGITS}d}"


def _check_tail_boundary(
    directory: Path,
    tail_num: int,
    tail_count: int,
    tail_offset: int,
    frames_by_chunk: dict[int, list[tuple[int, int]]],
    file_size: int,
    issues: list[Issue],
) -> bool:
    starts = [offset for offset, _ in frames_by_chunk.get(tail_num, [])]
    if tail_offset > file_size:
        issues.append(
            Issue(
                IssueKind.BOUNDARY_MISMATCH,
                f"Metadata tail offset {tail_offset} is past the end of "
                f"{_chunk_name(tail_num)} ({file_size} bytes)",
                str(directory / _chunk_name(tail_num)),
            )
        )
        return False
    if tail_offset not in starts and tail_offset not in (0, file_size):
        issues.append(
            Issue(
                IssueKind.BOUNDARY_MISMATCH,
                f"Metadata tail offset {tail_offset} in {_chunk_name(tail_num)} "
                "does not land on a record boundary",
                str(directory / _chunk_name(tail_num)),
            )
        )
        return False
    consumed = sum(1 for offset in starts if offset < tail_offset)
    if consumed != tail_count:
        issues.append(
            Issue(
                IssueKind.METADATA_MISMATCH,
                f"Metadata tail count is {tail_count} but {consumed} record(s) "
                f"precede offset {tail_offset} in {_chunk_name(tail_num)}",
                str(directory / _INFO_NAME),
            )
        )
    return True


def _inspect_lifo(path: Path) -> QueueReport:
    issues: list[Issue] = []
    file_size = path.stat().st_size

    if file_size < SIZE_SIZE:
        short_region = ByteRegion(str(path), 0, file_size)
        issues.append(
            Issue(
                IssueKind.CORRUPT_REGION,
                f"File is {file_size} byte(s) long, shorter than the 4-byte "
                "size header; nothing can be confirmed",
                str(path),
            )
        )
        return QueueReport(
            kind=QueueKind.LIFO,
            source=path,
            metadata=None,
            chunks=(),
            records=(),
            issues=tuple(issues),
            truncated_regions=(short_region,),
            missing_blocks=(),
        )

    with path.open("rb") as handle:
        header = handle.read(SIZE_SIZE)
        (claimed_size,) = struct.unpack(SIZE_FORMAT, header)
        frames, region = _scan_lifo_frames(path, handle, file_size, issues)

    records: list[Record] = []
    for index, (offset, length) in enumerate(frames):
        pop_index = len(frames) - 1 - index
        state = (
            RecordState.REFERENCED
            if pop_index < claimed_size
            else RecordState.UNREFERENCED
        )
        records.append(Record(state, None, offset, length))

    if region is not None:
        issues.append(
            Issue(
                IssueKind.TRUNCATED_RECORD,
                (
                    f"Undecodable tail region [{region.start}, {region.end}) "
                    f"({region.length} byte(s)); the last write did not finish "
                    "and no record is guessed from it"
                ),
                str(path),
            )
        )

    if claimed_size != len(frames):
        issues.append(
            Issue(
                IssueKind.METADATA_MISMATCH,
                f"Size header claims {claimed_size} record(s) but {len(frames)} "
                "complete record(s) are provable from the file",
                str(path),
            )
        )

    chunk = ChunkInfo(
        number=0,
        name=path.name,
        size=file_size,
        record_count=len(frames),
        clean_end=region is None,
    )
    return QueueReport(
        kind=QueueKind.LIFO,
        source=path,
        metadata={"size": claimed_size},
        chunks=(chunk,),
        records=tuple(records),
        issues=tuple(issues),
        truncated_regions=(region,) if region is not None else (),
        missing_blocks=(),
    )


def _scan_lifo_frames(
    path: Path,
    handle: BinaryIO,
    file_size: int,
    issues: list[Issue],
) -> tuple[list[tuple[int, int]], ByteRegion | None]:
    """Validate the LIFO frame chain from the end of the file.

    LIFO storage is ``size_header`` followed by ``payload, length`` frames.
    A frame is accepted only when the whole chain of length trailers reaches
    the byte right after the size header. When that fails, the only salvage
    attempt models a power loss during the last write, whose surviving suffix
    is at most ``SIZE_SIZE - 1`` bytes: each such suffix length is skipped
    once, and a candidate is accepted only if a complete frame chain still
    validates back to the size header. No payload byte is ever reinterpreted
    or scanned for candidate headers, and anything that cannot be validated
    exports nothing.
    """
    frames, cursor = _decode_lifo_chain(handle, file_size)
    if cursor == SIZE_SIZE:
        return frames, None

    for shift in range(1, SIZE_SIZE):
        candidate_end = file_size - shift
        if candidate_end <= SIZE_SIZE:
            break
        candidate, candidate_cursor = _decode_lifo_chain(handle, candidate_end)
        if candidate and candidate_cursor == SIZE_SIZE:
            return candidate, ByteRegion(str(path), candidate_end, file_size)

    issues.append(
        Issue(
            IssueKind.CORRUPT_REGION,
            "Frame chain cannot be validated back to the size header; no "
            "record boundary can be confirmed",
            str(path),
        )
    )
    return [], ByteRegion(str(path), SIZE_SIZE, file_size)


def _decode_lifo_chain(handle: BinaryIO, end: int) -> tuple[list[tuple[int, int]], int]:
    """Decode frames backwards from ``end``.

    Returns frames in file order (oldest first) as ``(payload_offset,
    length)`` and the cursor where decoding stopped; the chain is valid only
    when that cursor equals :data:`SIZE_SIZE`.
    """
    reversed_frames: list[tuple[int, int]] = []
    cursor = end
    while cursor > SIZE_SIZE:
        if cursor < 2 * SIZE_SIZE:
            break
        handle.seek(cursor - SIZE_SIZE)
        trailer = handle.read(SIZE_SIZE)
        if len(trailer) != SIZE_SIZE:
            break
        (payload_length,) = struct.unpack(SIZE_FORMAT, trailer)
        frame_start = cursor - SIZE_SIZE - payload_length
        if frame_start < SIZE_SIZE:
            break
        reversed_frames.append((frame_start, payload_length))
        cursor = frame_start
    reversed_frames.reverse()
    return reversed_frames, cursor


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def recover_queue(
    source: str | Path,
    destination: str | Path,
    queue_kind: Literal["fifo", "lifo", "auto"] = "auto",
) -> RecoveryResult:
    """Export every provably complete record into a new queue.

    The source is opened read-only and never modified. ``destination`` must
    not exist yet. By default the destination uses the same queue type as the
    source, and the recovered queue pops its items in the same order the
    damaged queue would have.
    """
    report = inspect_queue(source)
    target_kind = report.kind if queue_kind == "auto" else QueueKind(queue_kind)
    destination_path = _safe_destination(report.source, Path(destination))

    ordered = report.pop_order()
    if not ordered:
        raise ValueError(
            "No complete record can be confirmed in the source; refusing to "
            "create an empty destination"
        )

    if target_kind is QueueKind.FIFO:
        destination_path.mkdir(parents=True)
        queue: FifoDiskQueue | LifoDiskQueue = FifoDiskQueue(destination_path)
        push_order = ordered
    else:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        queue = LifoDiskQueue(destination_path)
        push_order = list(reversed(ordered))

    try:
        for record in push_order:
            queue.push(_read_payload(report, record))
    except BaseException:
        with suppress(Exception):
            queue.close()
        _discard_destination(destination_path, target_kind)
        raise
    queue.close()

    return RecoveryResult(
        report=report,
        destination=destination_path,
        queue_kind=target_kind,
        exported=len(ordered),
    )


def _discard_destination(path: Path, kind: QueueKind) -> None:
    """Remove a destination whose export did not finish, to leave no partial queue."""
    if kind is QueueKind.FIFO and path.is_dir():
        for entry in path.iterdir():
            entry.unlink()
        path.rmdir()
    elif kind is QueueKind.LIFO and path.is_file():
        path.unlink()


def _safe_destination(source: Path, destination: Path) -> Path:
    resolved_source = source.resolve()
    resolved_destination = destination.resolve()
    if resolved_destination == resolved_source:
        raise ValueError("Destination must differ from the source queue")
    if _is_relative_to(resolved_destination, resolved_source):
        raise ValueError("Destination must not live inside the source directory")
    if _is_relative_to(resolved_source, resolved_destination):
        raise ValueError("Source must not live inside the destination directory")
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    return destination


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True


def _read_payload(report: QueueReport, record: Record) -> bytes:
    if report.kind is QueueKind.LIFO:
        with report.source.open("rb") as handle:
            handle.seek(record.offset)
            return handle.read(record.length)
    assert record.chunk is not None
    chunk_path = report.source / _chunk_name(record.chunk)
    with chunk_path.open("rb") as handle:
        handle.seek(record.offset + SIZE_SIZE)
        return handle.read(record.length)


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def render_report(report: QueueReport) -> str:
    """Render an inspection report as human-readable plain text."""
    lines = [
        f"Queue: {report.source}",
        f"Type: {report.kind.value}",
        f"Status: {'OK' if report.ok else 'INCONSISTENT'}",
        f"Metadata: {_render_metadata(report)}",
    ]
    lines.extend(_render_chunks(report))
    lines.extend(_render_records(report))
    lines.extend(_render_regions(report))
    lines.extend(_render_missing(report))
    lines.extend(_render_issues(report))
    return "\n".join(lines)


def _render_metadata(report: QueueReport) -> str:
    if report.metadata is None:
        return "unavailable"
    return json.dumps(report.metadata, sort_keys=True)


def _render_chunks(report: QueueReport) -> list[str]:
    if not report.chunks:
        return []
    lines = ["Chunks:"]
    for chunk in report.chunks:
        ending = "clean end" if chunk.clean_end else "TRUNCATED"
        lines.append(
            f"  {chunk.name}: {chunk.size} byte(s), "
            f"{chunk.record_count} complete record(s), {ending}"
        )
    return lines


def _render_records(report: QueueReport) -> list[str]:
    return [
        "Records:",
        f"  complete: {len(report.records)} "
        f"(referenced: {len(report.referenced_records)}, "
        f"unreferenced tail: {len(report.unreferenced_records)}, "
        f"consumed: {len(report.consumed_records)})",
        f"  recoverable: {len(report.recoverable_records)}",
    ]


def _render_regions(report: QueueReport) -> list[str]:
    if not report.truncated_regions:
        return []
    lines = ["Uncertain regions (never interpreted):"]
    lines.extend(
        f"  {region.path} [{region.start}, {region.end}) ({region.length} byte(s))"
        for region in report.truncated_regions
    )
    return lines


def _render_missing(report: QueueReport) -> list[str]:
    if not report.missing_blocks:
        return []
    lines = ["Missing blocks:"]
    for block in report.missing_blocks:
        note = "metadata-referenced" if block.referenced_by_metadata else "sequence gap"
        lines.append(f"  {block.name} ({note})")
    return lines


def _render_issues(report: QueueReport) -> list[str]:
    if not report.issues:
        return []
    lines = [f"Issues ({len(report.issues)}):"]
    for number, issue in enumerate(report.issues, start=1):
        location = f" [{issue.path}]" if issue.path else ""
        lines.append(f"  {number}. {issue.kind.value}: {issue.message}{location}")
    return lines


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m queuelib",
        description="Read-only inspection and recovery for queuelib disk queues",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="report block, boundary and metadata inconsistencies"
    )
    inspect_parser.add_argument("path", help="queue directory (FIFO) or file (LIFO)")

    recover_parser = subparsers.add_parser(
        "recover", help="export provably complete records into a new queue"
    )
    recover_parser.add_argument("source", help="damaged queue directory or file")
    destination_help = "new queue path; must not exist"
    recover_parser.add_argument("destination", help=destination_help)
    recover_parser.add_argument(
        "--queue-type",
        choices=("fifo", "lifo", "auto"),
        default="auto",
        help="destination queue type (default: match the source)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        try:
            report = inspect_queue(args.path)
        except OSError as exc:
            print(f"inspection failed: {exc}")
            return 1
        print(render_report(report))
        return 0 if report.ok else 1
    try:
        result = recover_queue(args.source, args.destination, args.queue_type)
    except (OSError, ValueError) as exc:
        print(f"recovery failed: {exc}")
        return 1
    print(render_report(result.report))
    print(
        f"Exported {result.exported} record(s) to {result.destination} "
        f"as a {result.queue_kind.value} queue"
    )
    return 0
