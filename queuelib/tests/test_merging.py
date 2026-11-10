from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

import pytest
from hypothesis import given
from hypothesis import strategies as st

from queuelib import (
    FifoDiskQueue,
    FifoMemoryQueue,
    FifoSQLiteQueue,
    LifoDiskQueue,
    LifoMemoryQueue,
    LifoSQLiteQueue,
    MergingQueue,
)
from queuelib.tests import QueuelibTestCase, track_closed

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable

Queue: TypeAlias = (
    FifoMemoryQueue
    | LifoMemoryQueue
    | FifoDiskQueue
    | LifoDiskQueue
    | FifoSQLiteQueue
    | LifoSQLiteQueue
)
MEMORY: tuple[type[Queue], ...] = (FifoMemoryQueue, LifoMemoryQueue)


class MergingQueueTestMixin:
    def setUp(self):
        super().setUp()
        self.q = MergingQueue(self.qfactory)

    def qfactory(self, key):
        raise NotImplementedError

    def test_empty(self):
        assert len(self.q) == 0
        assert self.q.peek() is None
        assert self.q.pop() is None
        assert self.q.close() == []

    def test_single_push_pop(self):
        self.q.push(b"a", "k")
        assert len(self.q) == 1
        assert self.q.peek() == b"a"
        assert self.q.pop() == b"a"
        assert self.q.pop() is None
        assert len(self.q) == 0

    def test_falsy_payload_stays_pending(self):
        self.q.push(b"", "k")
        assert len(self.q) == 1
        assert self.q.peek() == b""
        assert self.q.pop() == b""
        assert self.q.pop() is None

    def test_replacement_keeps_first_position(self):
        self.q.push(b"a1", "a")
        self.q.push(b"b1", "b")
        self.q.push(b"c1", "c")
        self.q.push(b"b2", "b")
        self.q.push(b"b3", "b")
        assert len(self.q) == 3
        assert list(self.q.key_queue) == ["a", "b", "c"]
        assert [self.q.pop(), self.q.pop(), self.q.pop()] == [
            b"a1",
            b"b3",
            b"c1",
        ]
        assert self.q.pop() is None

    def test_hot_key_is_never_pushed_to_tail(self):
        keys = ["p1", "p2", "p3", "p4"]
        for key in keys:
            self.q.push(f"{key}-0".encode(), key)
        # The head key changes very frequently: it must keep its head position.
        for revision in range(1, 50):
            self.q.push(f"p1-{revision}".encode(), "p1")
        assert list(self.q.key_queue) == keys
        assert self.q.peek() == b"p1-49"
        popped = [self.q.pop() for _ in keys]
        assert popped == [b"p1-49", b"p2-0", b"p3-0", b"p4-0"]

    def test_repeated_submission_is_a_single_pending_entry(self):
        for revision in range(20):
            self.q.push(f"v{revision}".encode(), "hot")
        assert len(self.q) == 1
        assert list(self.q.key_queue) == ["hot"]
        assert self.q.pop() == b"v19"
        assert len(self.q) == 0
        assert self.q.pop() is None

    def test_merge_function_combines_payloads_in_place(self):
        def merge(old, new):
            return old + b"|" + new

        self.q.push(b"a", "k")
        self.q.push(b"other", "x")
        self.q.push(b"b", "k", merge=merge)
        self.q.push(b"c", "k", merge=merge)
        assert len(self.q) == 2
        assert list(self.q.key_queue) == ["k", "x"]
        assert self.q.peek() == b"a|b|c"
        assert self.q.pop() == b"a|b|c"
        assert self.q.pop() == b"other"

    def test_merge_without_pending_entry_creates_tail_task(self):
        seen = []

        def merge(old, new):
            seen.append((old, new))
            return new

        self.q.push(b"x", "x")
        self.q.push(b"k1", "k", merge=merge)
        assert seen == []
        assert list(self.q.key_queue) == ["x", "k"]
        assert self.q.pop() == b"x"
        assert self.q.pop() == b"k1"

    def test_failed_merge_keeps_old_payload_and_position(self):
        def failing_merge(old, new):
            raise RuntimeError("merge boom")

        self.q.push(b"a", "a")
        self.q.push(b"keep", "hot")
        self.q.push(b"c", "c")
        with pytest.raises(RuntimeError, match="merge boom"):
            self.q.push(b"dropped", "hot", merge=failing_merge)
        # Nothing was lost, no duplicate entry appeared, order is unchanged.
        assert len(self.q) == 3
        assert list(self.q.key_queue) == ["a", "hot", "c"]
        assert self.q.queues["hot"].peek() == b"keep"
        # The queue stays usable and the key can still be updated.
        self.q.push(b"fixed", "hot")
        assert [self.q.pop(), self.q.pop(), self.q.pop()] == [
            b"a",
            b"fixed",
            b"c",
        ]

    def test_popped_task_is_detached_and_reentry_goes_to_tail(self):
        self.q.push(b"a1", "a")
        self.q.push(b"b1", "b")
        assert self.q.pop() == b"a1"
        # "a" is no longer pending: updating it queues a brand-new task.
        self.q.push(b"c1", "c")
        self.q.push(b"a2", "a")
        assert list(self.q.key_queue) == ["b", "c", "a"]
        assert [self.q.pop(), self.q.pop(), self.q.pop()] == [
            b"b1",
            b"c1",
            b"a2",
        ]

    def test_peek_tracks_replacement(self):
        self.q.push(b"a1", "a")
        self.q.push(b"b1", "b")
        assert self.q.peek() == b"a1"
        self.q.push(b"a2", "a")
        assert self.q.peek() == b"a2"
        assert self.q.pop() == b"a2"
        assert self.q.peek() == b"b1"

    def test_clear(self):
        self.q.push(b"a", "a")
        self.q.push(b"b", "b")
        internal = list(self.q.queues.values())
        self.q.clear()
        assert len(self.q) == 0
        assert self.q.peek() is None
        assert self.q.pop() is None
        assert all(q.closed for q in internal)
        self.q.push(b"c", "c")
        assert self.q.pop() == b"c"

    def test_close_returns_active_keys_in_order(self):
        self.q.push(b"a", "a")
        self.q.push(b"b", "b")
        self.q.push(b"b2", "b")
        self.q.push(b"c", "c")
        assert self.q.close() == ["a", "b", "c"]

    def test_pop_closes_internal_queue(self):
        self.q.push(b"a", "a")
        internal = self.q.queues["a"]
        self.q.pop()
        assert internal.closed

    def test_failed_push_keeps_old_payload(self):
        class FailingPushQueue(FifoMemoryQueue):
            def push(self, obj):
                if obj == b"explode":
                    raise OSError("disk full")
                super().push(obj)

        q = MergingQueue(lambda key: FailingPushQueue())
        q.push(b"keep", "hot")
        q.push(b"other", "x")
        with pytest.raises(OSError, match="disk full"):
            q.push(b"explode", "hot")
        assert len(q) == 2
        assert list(q.key_queue) == ["hot", "x"]
        assert q.peek() == b"keep"
        assert [q.pop(), q.pop()] == [b"keep", b"other"]
        q.close()


class PersistentMergingMixin:
    def _reopen(self, active):
        return MergingQueue(self.qfactory, active)

    def test_restart_preserves_order_and_latest_content(self):
        self.q.push(b"a-0", "a")
        self.q.push(b"hot-0", "hot")
        self.q.push(b"c-0", "c")
        for revision in range(1, 30):
            self.q.push(f"hot-{revision}".encode(), "hot")
        self.q.push(b"a-1", "a")
        before = [self.q.peek()]
        active = self.q.close()
        # The hot key keeps its original middle position, not the tail.
        assert active == ["a", "hot", "c"]

        reopened = self._reopen(active)
        assert len(reopened) == 3
        assert list(reopened.key_queue) == ["a", "hot", "c"]
        assert [reopened.pop(), reopened.pop(), reopened.pop()] == [
            b"a-1",
            b"hot-29",
            b"c-0",
        ]
        assert reopened.pop() is None
        reopened.close()
        assert before == [b"a-1"]

    def test_restart_preserves_merged_content(self):
        def merge(old, new):
            return old + b"+" + new

        self.q.push(b"v1", "k")
        self.q.push(b"v2", "k", merge=merge)
        self.q.push(b"other", "o")
        active = self.q.close()

        reopened = self._reopen(active)
        assert list(reopened.key_queue) == ["k", "o"]
        assert reopened.pop() == b"v1+v2"
        assert reopened.pop() == b"other"
        reopened.close()

    def test_restart_then_popped_key_reenters_at_tail(self):
        self.q.push(b"a1", "a")
        self.q.push(b"b1", "b")
        active = self.q.close()
        reopened = self._reopen(active)
        assert reopened.pop() == b"a1"
        reopened.push(b"c1", "c")
        reopened.push(b"a2", "a")
        assert list(reopened.key_queue) == ["b", "c", "a"]
        assert [reopened.pop(), reopened.pop(), reopened.pop()] == [
            b"b1",
            b"c1",
            b"a2",
        ]
        reopened.close()

    def test_restart_empty_queue(self):
        active = self.q.close()
        assert active == []
        reopened = self._reopen(active)
        assert len(reopened) == 0
        reopened.push(b"fresh", "k")
        assert reopened.pop() == b"fresh"
        reopened.close()

    def test_resume_rejects_backing_queue_with_several_items(self):
        backing = self.qfactory("overloaded")
        backing.push(b"one")
        backing.push(b"two")
        backing.close()
        with pytest.raises(ValueError, match="at most one pending item"):
            MergingQueue(self.qfactory, ["overloaded"])

    def test_non_bytes_replacement_restores_old_payload(self):
        self.q.push(b"keep", "hot")
        self.q.push(b"other", "x")
        with pytest.raises(TypeError):
            self.q.push("not-bytes", "hot")
        assert len(self.q) == 2
        assert list(self.q.key_queue) == ["hot", "x"]
        assert self.q.peek() == b"keep"
        assert [self.q.pop(), self.q.pop()] == [b"keep", b"other"]


class FifoMemoryMergingQueueTest(MergingQueueTestMixin, QueuelibTestCase):
    def qfactory(self, key):
        return track_closed(FifoMemoryQueue)()


class LifoMemoryMergingQueueTest(MergingQueueTestMixin, QueuelibTestCase):
    def qfactory(self, key):
        return track_closed(LifoMemoryQueue)()


class InMemoryObjectPayloadTest(QueuelibTestCase):
    def test_dict_replace_and_merge_and_failure(self):
        q = MergingQueue(lambda key: track_closed(FifoMemoryQueue)())

        def merge(old, new):
            merged = dict(old)
            merged.update(new)
            return merged

        q.push({"rev": 1}, "product")
        q.push({"rev": 2}, "product")
        assert q.peek() == {"rev": 2}
        q.push({"price": 9}, "product", merge=merge)
        assert q.peek() == {"rev": 2, "price": 9}

        def failing_merge(old, new):
            raise ValueError("bad payload")

        with pytest.raises(ValueError, match="bad payload"):
            q.push({"rev": 3}, "product", merge=failing_merge)
        assert q.peek() == {"rev": 2, "price": 9}
        assert q.pop() == {"rev": 2, "price": 9}
        q.close()


class FifoDiskMergingQueueTest(
    MergingQueueTestMixin, PersistentMergingMixin, QueuelibTestCase
):
    def qfactory(self, key):
        return track_closed(FifoDiskQueue)(Path(self.qdir, str(key)))


class LifoDiskMergingQueueTest(
    MergingQueueTestMixin, PersistentMergingMixin, QueuelibTestCase
):
    def qfactory(self, key):
        return track_closed(LifoDiskQueue)(Path(self.qdir, str(key)))


class FifoSQLiteMergingQueueTest(
    MergingQueueTestMixin, PersistentMergingMixin, QueuelibTestCase
):
    def qfactory(self, key):
        return track_closed(FifoSQLiteQueue)(Path(self.qdir, str(key)))


class LifoSQLiteMergingQueueTest(
    MergingQueueTestMixin, PersistentMergingMixin, QueuelibTestCase
):
    def qfactory(self, key):
        return track_closed(LifoSQLiteQueue)(Path(self.qdir, str(key)))


KEYS: st.SearchStrategy[str] = st.sampled_from("ABCD")


def _opener(cls: type[Queue], path: Path) -> Callable[[], Queue]:
    if issubclass(cls, FifoMemoryQueue):
        return cls
    return lambda: cls(path)


@pytest.mark.parametrize(
    "cls",
    [
        FifoMemoryQueue,
        LifoMemoryQueue,
        FifoDiskQueue,
        LifoDiskQueue,
        FifoSQLiteQueue,
        LifoSQLiteQueue,
    ],
)
@given(ops=st.lists(st.tuples(st.booleans(), KEYS), max_size=80))
def test_merging_queue_matches_ordered_dict_model(
    cls: type[Queue], ops: list[tuple[bool, str]]
) -> None:
    """Repeated updates never reorder keys and always expose the newest payload."""

    def merge(old: bytes, new: bytes) -> bytes:
        return old + b"|" + new

    with tempfile.TemporaryDirectory() as tmp:
        persistent = cls not in MEMORY

        def qfactory(key: Hashable) -> Queue:
            return _opener(cls, Path(tmp, str(key)))()

        q = MergingQueue(qfactory)  # type: ignore[arg-type]
        model: dict[str, bytes] = {}
        seq = 0
        try:
            for use_merge, key in ops:
                payload = f"{key}-{seq}".encode()
                seq += 1
                if key in model:
                    expected = merge(model[key], payload) if use_merge else payload
                else:
                    expected = payload
                q.push(payload, key, merge=merge if use_merge else None)
                model[key] = expected
                assert len(q) == len(model)
                assert list(q.key_queue) == list(model)
                if model:
                    head = next(iter(model))
                    assert q.peek() == model[head]
            if persistent:
                active = q.close()
                assert list(active) == list(model)
                q = MergingQueue(qfactory, active)  # type: ignore[arg-type]
                assert list(q.key_queue) == list(model)
            for payload in model.values():
                assert q.pop() == payload
            assert q.pop() is None
        finally:
            q.close()
