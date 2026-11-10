from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from queuelib import (
    FifoDiskQueue,
    FifoSQLiteQueue,
    MigrationError,
    SourceQueueChanged,
    TargetQueueNotEmpty,
    migrate_fifo_disk_to_sqlite,
)
from queuelib import migration as migration_module
from queuelib.tests import QueuelibTestCase


def build_source(
    path: Path, items: list[bytes], chunksize: int = 3, pops: int = 0
) -> list[bytes]:
    """Create a FifoDiskQueue with *items*, pop *pops* of them, close it."""
    queue = FifoDiskQueue(path, chunksize=chunksize)
    for item in items:
        queue.push(item)
    for _ in range(pops):
        queue.pop()
    queue.close()
    return items[pops:]


def write_empty_source(path: Path, *, with_chunk: bool = False) -> None:
    """Write the on-disk layout of a closed, empty FifoDiskQueue."""
    path.mkdir(parents=True)
    (path / "info.json").write_text(
        json.dumps(
            {
                "chunksize": 100000,
                "size": 0,
                "tail": [0, 0, 0],
                "head": [0, 0],
            }
        )
    )
    if with_chunk:
        (path / "q00000").write_bytes(b"")


def drain_target(path: Path) -> list[bytes]:
    """Pop every item from the target, using its public FIFO API."""
    queue = FifoSQLiteQueue(path)
    out: list[bytes] = []
    try:
        while True:
            item = queue.pop()
            if item is None:
                break
            out.append(item)
    finally:
        queue.close()
    return out


def drain_source(path: Path) -> list[bytes]:
    """Pop every item from a source queue, proving it stays readable.

    Note that fully draining a FifoDiskQueue removes its chunks, like normal
    queue use; use :func:`assert_source_readable` while a migration still
    needs the source on disk.
    """
    queue = FifoDiskQueue(path)
    out: list[bytes] = []
    try:
        while True:
            item = queue.pop()
            if item is None:
                break
            out.append(item)
    finally:
        queue.close()
    return out


def assert_source_readable(path: Path, expected: list[bytes]) -> None:
    """Non-destructively prove the source is still openable and readable."""
    queue = FifoDiskQueue(path)
    try:
        assert len(queue) == len(expected)
        assert queue.peek() == (expected[0] if expected else None)
    finally:
        queue.close()


def digest_of(items: list[bytes]) -> str:
    return hashlib.sha256(b"".join(items)).hexdigest()


class FailingSQLiteQueue(FifoSQLiteQueue):
    """A FifoSQLiteQueue whose push() fails after a number of pushes."""

    fail_after: int | None = None
    pushes = 0

    def push(self, item: bytes) -> None:
        super().push(item)
        self.pushes += 1
        if self.fail_after is not None and self.pushes >= self.fail_after:
            raise OSError("simulated crash during migration")


def run_crashing_migration(
    source: Path, target: Path, state: Path, batch_size: int
) -> None:
    """Run a migration whose target queue raises after its push budget."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(migration_module, "FifoSQLiteQueue", FailingSQLiteQueue)
        migrate_fifo_disk_to_sqlite(
            source, target, state_path=state, batch_size=batch_size
        )


class MigrationTestCase(QueuelibTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = Path(self.tmpdir) / "source-queue"
        self.target = Path(self.mkdtemp()) / "target.sqlite3"
        self.state = Path(self.mkdtemp()) / "migration.json"

    def migrate(self, **kwargs: Any) -> Any:
        kwargs.setdefault("state_path", self.state)
        return migrate_fifo_disk_to_sqlite(self.source, self.target, **kwargs)


class BasicMigrationTest(MigrationTestCase):
    def test_migrates_items_bytes_and_order(self):
        items = [b"alpha", b"", b"\x00\x01\x02binary", b"beta" * 100, b"g"]
        expected = build_source(self.source, items, chunksize=2)

        result = self.migrate(batch_size=2)

        assert result.item_count == len(expected)
        assert result.byte_count == sum(map(len, expected))
        assert result.digest == digest_of(expected)
        assert result.digest_algorithm == "sha256"
        assert result.order_check_passed is True
        assert result.resumed is False
        assert drain_target(self.target) == expected

    def test_multi_chunk_queue_with_pops(self):
        items = [f"item-{i}".encode() for i in range(10)]
        expected = build_source(self.source, items, chunksize=2, pops=3)
        assert len(list(self.source.glob("q*"))) >= 2

        result = self.migrate(batch_size=3)

        assert result.item_count == 7
        assert drain_target(self.target) == expected
        # The source is untouched and still fully readable after success.
        assert drain_source(self.source) == expected

    def test_empty_queue_without_chunks(self):
        write_empty_source(self.source)

        result = self.migrate()

        assert result.item_count == 0
        assert result.byte_count == 0
        assert result.digest == digest_of([])
        assert result.order_check_passed is True
        # Closing an empty SQLite queue removes its file.
        assert not self.target.exists()
        assert drain_source(self.source) == []

    def test_empty_queue_with_empty_chunk(self):
        write_empty_source(self.source, with_chunk=True)

        result = self.migrate()

        assert result.item_count == 0
        assert result.order_check_passed is True
        assert drain_source(self.source) == []

    def test_empty_migration_is_idempotent_after_file_cleanup(self):
        write_empty_source(self.source)

        first = self.migrate()
        assert first.item_count == 0
        assert not self.target.exists()

        second = self.migrate()
        assert second.item_count == 0
        assert second.order_check_passed is True
        assert second.resumed is True

    def test_default_state_file_next_to_target(self):
        build_source(self.source, [b"a", b"b"])
        result = migrate_fifo_disk_to_sqlite(self.source, self.target)
        default_state = self.target.with_name(self.target.name + ".migration.json")
        assert default_state.exists()
        assert json.loads(default_state.read_text())["status"] == "completed"
        assert result.item_count == 2

    def test_source_remains_readable_and_unchanged(self):
        items = [b"one", b"two", b"three", b"four"]
        expected = build_source(self.source, items, chunksize=2, pops=1)
        before = {
            chunk.name: hashlib.sha256(chunk.read_bytes()).hexdigest()
            for chunk in sorted(self.source.glob("q*"))
        }

        self.migrate()

        after = {
            chunk.name: hashlib.sha256(chunk.read_bytes()).hexdigest()
            for chunk in sorted(self.source.glob("q*"))
        }
        assert after == before
        assert drain_source(self.source) == expected


class ResumeMigrationTest(MigrationTestCase):
    def test_repeated_resumes(self):
        items = [b"x" * (i + 1) for i in range(7)]
        expected = build_source(self.source, items, chunksize=2)

        for fail_after in (2, 5):
            FailingSQLiteQueue.fail_after = fail_after
            FailingSQLiteQueue.pushes = 0
            with pytest.raises(OSError, match="simulated crash"):
                run_crashing_migration(self.source, self.target, self.state, 2)

        result = self.migrate(batch_size=2)
        assert result.resumed is True
        assert drain_target(self.target) == expected

    def test_completed_state_is_idempotent(self):
        expected = build_source(self.source, [b"a", b"b", b"c"], chunksize=2)
        first = self.migrate()
        second = self.migrate()

        assert second.resumed is True
        assert second.item_count == first.item_count
        assert second.digest == first.digest
        assert drain_target(self.target) == expected

    def test_state_for_other_paths_is_rejected(self):
        build_source(self.source, [b"a"])
        self.state.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": str(Path(self.qdir) / "somewhere-else"),
                    "target": str(self.target),
                    "snapshot": {},
                    "position": [0, 0, 0],
                    "count": 0,
                    "bytes": 0,
                    "digest": digest_of([]),
                    "status": "in_progress",
                }
            )
        )
        with pytest.raises(MigrationError, match="different source"):
            self.migrate()

    def test_unsupported_state_version(self):
        build_source(self.source, [b"a"])
        state = json.loads(self._fresh_state_text())
        state["version"] = 99
        self.state.write_text(json.dumps(state))
        with pytest.raises(MigrationError, match="version"):
            self.migrate()

    def _fresh_state_text(self) -> str:
        migrate_fifo_disk_to_sqlite(
            self.source,
            self.target,
            state_path=self.state,
        )
        return self.state.read_text()


class TargetGuardsTest(MigrationTestCase):
    def test_non_empty_target_is_rejected(self):
        build_source(self.source, [b"a", b"b"])
        foreign = FifoSQLiteQueue(self.target)
        foreign.push(b"foreign-task-item")
        foreign.close()

        with pytest.raises(TargetQueueNotEmpty):
            self.migrate()

        # Nothing was pushed, no success was recorded, foreign item survives.
        assert drain_target(self.target) == [b"foreign-task-item"]
        assert json.loads(self.state.read_text())["status"] == "in_progress"
        assert drain_source(self.source) == [b"a", b"b"]

    def test_target_inside_source_directory_is_rejected(self):
        build_source(self.source, [b"a"])
        with pytest.raises(MigrationError, match="must not live inside"):
            migrate_fifo_disk_to_sqlite(
                self.source,
                self.source / "nested" / "target.sqlite3",
            )

    def test_missing_source_raises(self):
        with pytest.raises(FileNotFoundError):
            self.migrate()

    def test_invalid_batch_size(self):
        build_source(self.source, [b"a"])
        with pytest.raises(ValueError, match="batch_size"):
            self.migrate(batch_size=0)

    def test_target_losing_items_is_detected(self):
        items = [f"p{i}".encode() for i in range(6)]
        build_source(self.source, items, chunksize=2)
        FailingSQLiteQueue.fail_after = 4
        FailingSQLiteQueue.pushes = 0
        with pytest.raises(OSError, match="simulated crash"):
            run_crashing_migration(self.source, self.target, self.state, 2)

        # Delete rows behind the migration state's back.
        with sqlite3.Connection(self.target) as conn:
            conn.execute("DELETE FROM queue")
        with pytest.raises(MigrationError, match="lost items"):
            self.migrate()

    def test_foreign_rows_on_resume_are_rejected(self):
        items = [f"p{i}".encode() for i in range(6)]
        build_source(self.source, items, chunksize=2)
        FailingSQLiteQueue.fail_after = 3
        FailingSQLiteQueue.pushes = 0
        with pytest.raises(OSError, match="simulated crash"):
            run_crashing_migration(self.source, self.target, self.state, 2)

        # Replace the migrated prefix with rows from a different task.
        with sqlite3.Connection(self.target) as conn:
            conn.execute("DELETE FROM queue")
            conn.execute("INSERT INTO queue (item) VALUES (?)", (b"foreign",))
        with pytest.raises(MigrationError, match="refusing to mix queues"):
            self.migrate()
        assert json.loads(self.state.read_text())["status"] == "in_progress"


class SourceChangeDetectionTest(MigrationTestCase):
    def _crash_once(self) -> None:
        build_source(self.source, [f"p{i}".encode() for i in range(6)], chunksize=2)
        FailingSQLiteQueue.fail_after = 3
        FailingSQLiteQueue.pushes = 0
        with pytest.raises(OSError, match="simulated crash"):
            run_crashing_migration(self.source, self.target, self.state, 2)

    def test_source_push_is_detected(self):
        self._crash_once()
        queue = FifoDiskQueue(self.source)
        queue.push(b"new-item-during-migration")
        queue.close()

        with pytest.raises(SourceQueueChanged):
            self.migrate()
        assert json.loads(self.state.read_text())["status"] == "in_progress"

    def test_source_pop_is_detected(self):
        self._crash_once()
        queue = FifoDiskQueue(self.source)
        assert queue.pop() is not None
        queue.close()

        with pytest.raises(SourceQueueChanged):
            self.migrate()

    def test_chunk_tampering_is_detected(self):
        self._crash_once()
        chunk = next(self.source.glob("q*"))
        chunk.write_bytes(chunk.read_bytes() + b"tampered")

        with pytest.raises(SourceQueueChanged):
            self.migrate()

    def test_info_json_removal_is_detected(self):
        self._crash_once()
        (self.source / "info.json").unlink()
        with pytest.raises(MigrationError):
            self.migrate()

    def test_change_during_verification_aborts_success(self):
        build_source(self.source, [b"a", b"b", b"c", b"d"], chunksize=2)
        real_check = migration_module._ensure_source_unchanged
        calls = 0

        def tampering_check(source_path: Path, snapshot: dict[str, Any]) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                (source_path / "q00000").write_bytes(b"x" * 16)
            real_check(source_path, snapshot)

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                migration_module,
                "_ensure_source_unchanged",
                tampering_check,
            )
            with pytest.raises(SourceQueueChanged):
                self.migrate()
        assert json.loads(self.state.read_text())["status"] == "in_progress"


class VerificationTest(MigrationTestCase):
    def test_full_target_consumption_matches_source(self):
        items = [bytes((i, i + 1, i + 2)) for i in range(15)] + [b""]
        expected = build_source(self.source, items, chunksize=4, pops=2)

        result = self.migrate(batch_size=5)

        source_sequence = drain_source(self.source)
        target_sequence = drain_target(self.target)
        assert target_sequence == source_sequence == expected
        assert result.digest == digest_of(source_sequence)
        assert result.order_check_passed is True

    def test_corrupt_target_fails_verification(self):
        expected = build_source(self.source, [b"a", b"b", b"c", b"d"], chunksize=2)
        self.migrate(batch_size=2)
        check_queue = FifoSQLiteQueue(self.target)
        try:
            assert len(check_queue) == len(expected)
        finally:
            check_queue.close()

        # Rewrite a target row behind the completed migration's back, then
        # reset the state so verification runs again against a changed target.
        with sqlite3.Connection(self.target) as conn:
            conn.execute("UPDATE queue SET item = ? WHERE id = 1", (b"zzz",))
        state = json.loads(self.state.read_text())
        state["status"] = "in_progress"
        self.state.write_text(json.dumps(state))

        with pytest.raises(MigrationError, match="refusing to mix queues"):
            self.migrate()


@pytest.mark.parametrize("fail_after", [1, 2, 3, 4, 5, 6, 7])
def test_resumes_after_crash_without_duplicates(fail_after: int) -> None:
    items = [f"payload-{i}".encode() for i in range(10)]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "source"
        target = tmp_path / "target.sqlite3"
        state = tmp_path / "state.json"
        expected = build_source(source, items, chunksize=2)

        FailingSQLiteQueue.fail_after = fail_after
        FailingSQLiteQueue.pushes = 0
        with pytest.raises(OSError, match="simulated crash"):
            run_crashing_migration(source, target, state, 3)

        crashed_state = json.loads(state.read_text())
        assert crashed_state["status"] == "in_progress"
        assert crashed_state["count"] <= fail_after

        # The source stayed readable while the migration was unfinished.
        assert_source_readable(source, expected)

        result = migrate_fifo_disk_to_sqlite(
            source, target, state_path=state, batch_size=3
        )

        assert result.resumed is True
        assert result.item_count == len(expected)
        assert result.byte_count == sum(map(len, expected))
        assert result.digest == digest_of(expected)
        assert result.order_check_passed is True
        assert drain_target(target) == expected
        assert json.loads(state.read_text())["status"] == "completed"


@given(
    items=st.lists(st.binary(max_size=16), min_size=0, max_size=20),
    chunksize=st.integers(1, 4),
    pops=st.integers(0, 20),
    fail_after=st.integers(1, 25),
    batch_size=st.integers(1, 5),
)
def test_random_migration_with_interruption(
    items: list[bytes],
    chunksize: int,
    pops: int,
    fail_after: int,
    batch_size: int,
) -> None:
    """Property test: interrupted and resumed migrations keep bytes + order."""

    class RandomFailingQueue(FifoSQLiteQueue):
        pushes = 0

        def push(self, item: bytes) -> None:
            super().push(item)
            self.pushes += 1
            if self.pushes >= fail_after:
                raise OSError("simulated crash")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "source"
        target = tmp_path / "target.sqlite3"
        state = tmp_path / "state.json"
        pops = min(pops, len(items))
        expected = build_source(source, items, chunksize=chunksize, pops=pops)

        interrupted = False
        if expected:
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(
                    migration_module, "FifoSQLiteQueue", RandomFailingQueue
                )
                try:
                    migrate_fifo_disk_to_sqlite(
                        source, target, state_path=state, batch_size=batch_size
                    )
                except OSError:
                    interrupted = True
        else:
            write_empty_source(source)

        result = migrate_fifo_disk_to_sqlite(
            source, target, state_path=state, batch_size=batch_size
        )

        # A second call always sees a saved state when items were migrated;
        # an empty queue is migrated in one fresh run.
        assert result.resumed is (interrupted or bool(expected))
        assert result.item_count == len(expected)
        assert result.byte_count == sum(map(len, expected))
        assert result.digest == digest_of(expected)
        assert result.order_check_passed is True
        assert drain_target(target) == expected
        assert drain_source(source) == expected
