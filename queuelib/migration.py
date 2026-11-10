"""Offline migration from :class:`~queuelib.FifoDiskQueue` to
:class:`~queuelib.FifoSQLiteQueue`.

The migration never mutates the source queue: it reads the chunk files and the
``info.json`` bookkeeping directly, instead of popping items. A JSON state file
next to the target (or at a user-chosen path) makes an interrupted migration
resumable. A content hash snapshot of the source is taken when the migration
starts and checked again before success, so any change of the source while a
migration is in progress aborts the migration with :class:`SourceQueueChanged`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import struct
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from queuelib.queue import FifoDiskQueue, FifoSQLiteQueue

if TYPE_CHECKING:
    import os
    from collections.abc import Iterator

_DIGEST_ALGORITHM = "sha256"
_STATE_VERSION = 1
_STATUS_IN_PROGRESS = "in_progress"
_STATUS_COMPLETED = "completed"
_CHUNK_PATTERN = "q*"
_STATE_SUFFIX = ".migration.json"
_BUFFER_SIZE = 1 << 20


class MigrationError(Exception):
    """Base class for failures raised by the offline queue migration."""


class SourceQueueChanged(MigrationError):
    """Raised when the source queue changes while a migration is in progress.

    The source queue is snapshotted when the migration starts and the snapshot
    is rechecked on every run and before success. Any push, pop or other
    modification of the source makes this exception abort the migration.
    """


class TargetQueueNotEmpty(MigrationError):
    """Raised when the target SQLite queue already holds items.

    A migration only accepts a target that is empty, so that items from
    different tasks can never be mixed into the same queue.
    """


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of a successful migration.

    ``digest`` is the hash of every migrated item concatenated in dequeue
    order. Equal digests between the source scan and the target consumption
    prove that the item bytes and their FIFO order are identical.
    """

    source_path: Path
    target_path: Path
    item_count: int
    byte_count: int
    digest: str
    digest_algorithm: str
    order_check_passed: bool
    resumed: bool


def _hash_file(path: Path) -> str:
    digest = hashlib.new(_DIGEST_ALGORITHM)
    with path.open("rb") as f:
        for block in iter(lambda: f.read(_BUFFER_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _take_snapshot(source_path: Path) -> dict[str, Any]:
    """Return a content-based snapshot of a :class:`FifoDiskQueue` directory."""
    info_path = source_path / "info.json"
    if not info_path.is_file():
        raise MigrationError(
            f"{source_path} is not a FifoDiskQueue directory (missing info.json)"
        )
    info = cast("dict[str, Any]", json.loads(info_path.read_text()))
    files = {
        chunk.name: _hash_file(chunk)
        for chunk in sorted(source_path.glob(_CHUNK_PATTERN))
    }
    return {
        "chunksize": info["chunksize"],
        "size": info["size"],
        "head": list(info["head"]),
        "tail": list(info["tail"]),
        "info_digest": hashlib.new(
            _DIGEST_ALGORITHM, info_path.read_bytes()
        ).hexdigest(),
        "files": files,
    }


def _ensure_source_unchanged(source_path: Path, snapshot: dict[str, Any]) -> None:
    current = _take_snapshot(source_path)
    if current != snapshot:
        raise SourceQueueChanged(
            f"source queue {source_path} changed during the migration"
        )


def _write_state(state_path: Path, state: dict[str, Any]) -> None:
    tmp_path = state_path.with_name(f".{state_path.name}.tmp")
    tmp_path.write_text(json.dumps(state, sort_keys=True))
    tmp_path.replace(state_path)


def _iter_items(
    source_path: Path,
    start: tuple[int, int, int],
    head: tuple[int, int],
    chunksize: int,
) -> Iterator[tuple[bytes, int, int, int]]:
    """Yield ``(item, chunk, index, offset)`` pairs from raw chunk files.

    The items are yielded in FIFO dequeue order, starting at *start*
    ``(chunk number, item index, byte offset)`` and stopping at *head*. The
    position yielded with an item points just past that item, so it can be
    persisted as a resume position.
    """
    tnum, tcnt, toffset = start
    head_num, head_pos = head
    header = struct.Struct(FifoDiskQueue.szhdr_format)
    while tnum < head_num or (tnum == head_num and tcnt < head_pos):
        if tcnt >= chunksize:
            tnum += 1
            tcnt = 0
            toffset = 0
            continue
        chunk_path = source_path / f"q{tnum:05d}"
        try:
            with chunk_path.open("rb") as chunk_file:
                chunk_file.seek(toffset)
                while tcnt < chunksize and (
                    tnum < head_num or (tnum == head_num and tcnt < head_pos)
                ):
                    raw_header = chunk_file.read(header.size)
                    if not raw_header:
                        break
                    if len(raw_header) != header.size:
                        raise MigrationError(
                            f"truncated item header in source chunk {chunk_path}"
                        )
                    (length,) = header.unpack(raw_header)
                    item = chunk_file.read(cast("int", length))
                    if len(item) != length:
                        raise MigrationError(
                            f"truncated item body in source chunk {chunk_path}"
                        )
                    tcnt += 1
                    toffset += header.size + cast("int", length)
                    yield item, tnum, tcnt, toffset
        except FileNotFoundError as exc:
            raise SourceQueueChanged(
                f"source chunk {chunk_path} disappeared during the migration"
            ) from exc
        tnum += 1
        tcnt = 0
        toffset = 0


def _iter_target_items(target_path: Path) -> Iterator[bytes]:
    """Yield every target item in FIFO order, without deleting it."""
    with sqlite3.Connection(target_path) as conn:
        conn.text_factory = bytes
        for (item,) in conn.execute("SELECT item FROM queue ORDER BY id"):
            yield cast("bytes", item)


def _hash_target_items(target_path: Path) -> tuple[int, str]:
    digest = hashlib.new(_DIGEST_ALGORITHM)
    count = 0
    # An empty FifoSQLiteQueue deletes its file on close().
    if target_path.exists():
        for item in _iter_target_items(target_path):
            digest.update(item)
            count += 1
    return count, digest.hexdigest()


def _verify(
    source_path: Path,
    target_path: Path,
    snapshot: dict[str, Any],
    state: dict[str, Any],
) -> None:
    """Independently prove that target consumption matches the source."""
    _ensure_source_unchanged(source_path, snapshot)

    chunksize = cast("int", snapshot["chunksize"])
    head = cast("tuple[int, int]", tuple(snapshot["head"]))
    tail = cast("tuple[int, int, int]", tuple(snapshot["tail"]))

    source_digest = hashlib.new(_DIGEST_ALGORITHM)
    source_count = 0
    source_bytes = 0
    for item, _, _, _ in _iter_items(source_path, tail, head, chunksize):
        source_digest.update(item)
        source_count += 1
        source_bytes += len(item)
    if source_count != cast("int", snapshot["size"]):
        raise MigrationError(
            f"source scan found {source_count} items, "
            f"info.json records {snapshot['size']}"
        )
    if (
        source_count != cast("int", state["count"])
        or source_bytes != cast("int", state["bytes"])
        or source_digest.hexdigest() != cast("str", state["digest"])
    ):
        raise MigrationError(
            "the migrated items do not match an independent scan of the source"
        )

    expected_count = source_count
    if target_path.exists():
        # Consume a throwaway copy of the database fully, so the proof uses the
        # real pop() path while the target itself stays populated.
        with tempfile.NamedTemporaryFile(
            dir=target_path.parent, prefix=".migrate-verify-", delete=False
        ) as tmp:
            copy_path = Path(tmp.name)
        try:
            shutil.copyfile(target_path, copy_path)
            target_digest = hashlib.new(_DIGEST_ALGORITHM)
            target_count = 0
            target_bytes = 0
            verify_queue = FifoSQLiteQueue(copy_path)
            try:
                while True:
                    popped = verify_queue.pop()
                    if popped is None:
                        break
                    target_digest.update(popped)
                    target_count += 1
                    target_bytes += len(popped)
            finally:
                verify_queue.close()
        finally:
            with suppress(FileNotFoundError):
                copy_path.unlink()
        if (
            target_count != expected_count
            or target_bytes != source_bytes
            or target_digest.hexdigest() != source_digest.hexdigest()
        ):
            raise MigrationError(
                "fully consuming the target does not reproduce the source "
                "sequence of items"
            )
    elif expected_count:
        raise MigrationError(
            f"target queue {target_path} is missing but {expected_count} "
            "items were migrated"
        )


def _completed_result(
    state: dict[str, Any],
    source_path: Path,
    target_path: Path,
    snapshot: dict[str, Any],
) -> MigrationResult:
    count = cast("int", state["count"])
    target_count, target_digest = _hash_target_items(target_path)
    if target_count != count or target_digest != cast("str", state["digest"]):
        raise MigrationError(
            "the target queue no longer matches the completed migration state"
        )
    return MigrationResult(
        source_path=source_path,
        target_path=target_path,
        item_count=count,
        byte_count=cast("int", state["bytes"]),
        digest=cast("str", state["digest"]),
        digest_algorithm=_DIGEST_ALGORITHM,
        order_check_passed=True,
        resumed=True,
    )


def migrate_fifo_disk_to_sqlite(
    source_path: str | os.PathLike[str],
    target_path: str | os.PathLike[str],
    *,
    state_path: str | os.PathLike[str] | None = None,
    batch_size: int = 1000,
) -> MigrationResult:
    """Migrate a :class:`FifoDiskQueue` into a :class:`FifoSQLiteQueue`.

    The migration is offline and non-destructive:

    * Every item is copied with its original bytes and in FIFO dequeue order;
      the source queue is only read, never popped or deleted, and stays usable
      until the migration has been confirmed.
    * Progress is persisted to a JSON state file (``<target>.migration.json`` by
      default), so an interrupted run continues where it stopped instead of
      duplicating items.
    * A content hash snapshot of the source is taken up front and rechecked on
      every run and before success; a source that changed in the meantime makes
      the run fail with :class:`SourceQueueChanged`.
    * A target that already contains items is rejected with
      :class:`TargetQueueNotEmpty`, so items of different tasks can never be
      mixed. An empty (or missing) target is accepted.
    * Before returning, the source is scanned independently and a throwaway
      copy of the target is fully consumed with ``pop()``; both sequences must
      hash identically, otherwise :class:`MigrationError` is raised.

    :param source_path: directory of the existing ``FifoDiskQueue``.
    :param target_path: file the ``FifoSQLiteQueue`` is written to.
    :param state_path: optional custom path of the JSON migration state file.
    :param batch_size: items migrated between two progress state writes.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    source = Path(source_path).resolve()
    target = Path(target_path).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"source queue directory not found: {source}")
    if target == source or target.is_relative_to(source):
        raise MigrationError(
            "the target queue must not live inside the source queue directory"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    state_file = (
        Path(state_path).resolve()
        if state_path is not None
        else target.with_name(target.name + _STATE_SUFFIX)
    )
    if state_file == source or state_file.is_relative_to(source):
        raise MigrationError(
            "the migration state file must not live inside the source queue directory"
        )

    resumed = False
    if state_file.exists():
        state = cast("dict[str, Any]", json.loads(state_file.read_text()))
        if state.get("version") != _STATE_VERSION:
            raise MigrationError(f"unsupported migration state version in {state_file}")
        if (
            Path(cast("str", state["source"])) != source
            or Path(cast("str", state["target"])) != target
        ):
            raise MigrationError(
                "the migration state was written for a different source or target queue"
            )
        snapshot = cast("dict[str, Any]", state["snapshot"])
        resumed = True
    else:
        snapshot = _take_snapshot(source)
        state = {
            "version": _STATE_VERSION,
            "source": str(source),
            "target": str(target),
            "snapshot": snapshot,
            "position": list(snapshot["tail"]),
            "count": 0,
            "bytes": 0,
            "digest": hashlib.new(_DIGEST_ALGORITHM).hexdigest(),
            "status": _STATUS_IN_PROGRESS,
        }
        _write_state(state_file, state)

    # Fail fast if the source already differs from the recorded snapshot.
    _ensure_source_unchanged(source, snapshot)

    if state["status"] == _STATUS_COMPLETED:
        return _completed_result(state, source, target, snapshot)

    chunksize = cast("int", snapshot["chunksize"])
    head = cast("tuple[int, int]", tuple(snapshot["head"]))
    tail = cast("tuple[int, int, int]", tuple(snapshot["tail"]))
    position = cast("list[int]", state["position"])
    target_queue = FifoSQLiteQueue(target)
    try:
        hasher = hashlib.new(_DIGEST_ALGORITHM)
        count = 0
        byte_count = 0
        if not resumed:
            if len(target_queue) != 0:
                raise TargetQueueNotEmpty(
                    f"target queue {target} already contains items; refusing to "
                    "mix queues of different tasks"
                )
            items = _iter_items(source, tail, head, chunksize)
        else:
            # The target is the source of truth for what survived a crash:
            # every row it holds must be the next source item in FIFO order,
            # i.e. the exact prefix of the source already migrated. A foreign
            # task's queue, lost rows or reordered rows fail this check, so a
            # resume can never mix two queues.
            saved_count = cast("int", state["count"])
            source_items = _iter_items(source, tail, head, chunksize)
            for existing in _iter_target_items(target):
                try:
                    source_item, _, _, _ = next(source_items)
                except StopIteration as exc:
                    raise MigrationError(
                        "the target queue holds more items than the source "
                        "snapshot; refusing to mix queues"
                    ) from exc
                if existing != source_item:
                    raise MigrationError(
                        "the target queue does not contain the source items "
                        "already migrated; refusing to mix queues"
                    )
                hasher.update(existing)
                count += 1
                byte_count += len(existing)
            if count < saved_count:
                raise MigrationError(
                    "the target queue lost items that the migration state records"
                )
            items = source_items

        since_flush = 0
        for item, tnum, tcnt, toffset in items:
            position = [tnum, tcnt, toffset]
            target_queue.push(item)
            hasher.update(item)
            count += 1
            byte_count += len(item)
            since_flush += 1
            if since_flush >= batch_size:
                state.update(
                    {
                        "position": position,
                        "count": count,
                        "bytes": byte_count,
                        "digest": hasher.hexdigest(),
                    }
                )
                _write_state(state_file, state)
                since_flush = 0

        state.update(
            {
                "position": position,
                "count": count,
                "bytes": byte_count,
                "digest": hasher.hexdigest(),
                "status": _STATUS_IN_PROGRESS,
            }
        )
        _write_state(state_file, state)
    finally:
        target_queue.close()

    _verify(source, target, snapshot, state)
    state["status"] = _STATUS_COMPLETED
    _write_state(state_file, state)
    return MigrationResult(
        source_path=source,
        target_path=target,
        item_count=count,
        byte_count=byte_count,
        digest=hasher.hexdigest(),
        digest_algorithm=_DIGEST_ALGORITHM,
        order_check_passed=True,
        resumed=resumed,
    )
