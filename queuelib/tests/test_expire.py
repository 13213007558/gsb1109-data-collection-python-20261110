from __future__ import annotations

import pytest

from queuelib import (
    REASON_ALL_EXPIRED,
    REASON_DEADLINE_REACHED,
    DeadlineQueue,
    ExpiredItem,
    ExpiredItemError,
    ExpiringQueue,
    FifoDiskQueue,
    FifoMemoryQueue,
    FifoSQLiteQueue,
    LifoDiskQueue,
    LifoMemoryQueue,
    LifoSQLiteQueue,
    PriorityQueue,
    RoundRobinQueue,
)

MEMORY_CLASSES = (FifoMemoryQueue, LifoMemoryQueue)
DISK_CLASSES = (FifoDiskQueue, LifoDiskQueue, FifoSQLiteQueue, LifoSQLiteQueue)
ALL_CLASSES = MEMORY_CLASSES + DISK_CLASSES


class FakeClock:
    """Caller-controlled clock, the only source of time in these tests."""

    def __init__(self, start: int = 0) -> None:
        self.time = start

    def __call__(self) -> int:
        return self.time

    def set(self, value: int) -> None:
        self.time = value


def make_backing(cls, root):
    if issubclass(cls, FifoMemoryQueue):
        return cls()
    if cls is FifoDiskQueue:
        return cls(root / "dir")
    return cls(root / "queue")


def is_lifo(cls: type) -> bool:
    return cls.__name__.startswith("Lifo")


def push_head_first(q, spec, cls):
    """Push (item, deadline) pairs given in desired head-to-tail order.

    FIFO keeps the oldest item at the head while LIFO keeps the newest, so for
    LIFO the pairs are pushed in reverse to land in the same head order.
    """
    order = reversed(spec) if is_lifo(cls) else spec
    for item, deadline in order:
        q.push(item, deadline)


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_empty_queue(cls, tmp_path):
    clock = FakeClock()
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    assert q.pop() is None
    assert q.peek() is None
    assert q.pop_expired() == []
    assert len(q) == 0
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_valid_items_follow_backing_order(cls, tmp_path):
    clock = FakeClock(0)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    for item in (b"a", b"b", b"c"):
        q.push(item, deadline=100)
    expected = [b"c", b"b", b"a"] if is_lifo(cls) else [b"a", b"b", b"c"]
    assert [q.pop() for _ in range(3)] == expected
    assert q.pop() is None
    assert q.pop_expired() == []
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_exact_deadline_is_expired(cls, tmp_path):
    # Equality means expired: validity is deadline > now, never >=.
    clock = FakeClock(10)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    q.push(b"on-the-edge", deadline=10)
    assert q.peek() is None
    assert q.pop() is None
    reclaimed = q.pop_expired()
    assert len(reclaimed) == 1
    assert reclaimed[0].item == b"on-the-edge"
    assert reclaimed[0].deadline == 10
    assert reclaimed[0].reason == REASON_DEADLINE_REACHED
    assert len(q) == 0
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_one_instant_before_deadline_is_still_valid(cls, tmp_path):
    clock = FakeClock(9)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    q.push(b"on-the-edge", deadline=10)
    assert q.peek() == b"on-the-edge"
    assert q.pop() == b"on-the-edge"
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_peek_never_evicts(cls, tmp_path):
    clock = FakeClock(10)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    push_head_first(q, [(b"expired", 5), (b"valid", 100)], cls)
    assert len(q) == 2
    for _ in range(3):
        assert q.peek() is None  # expired head hides the valid one
        assert len(q) == 2  # but peek must not drop anything
    # pop_expired reclaims the head, exposing the still-valid item.
    reclaimed = q.pop_expired()
    assert [e.item for e in reclaimed] == [b"expired"]
    assert len(q) == 1
    assert q.peek() == b"valid"
    assert q.pop() == b"valid"
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_pop_skips_expired_head_and_reclaims_it(cls, tmp_path):
    clock = FakeClock(10)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    push_head_first(q, [(b"old", 5), (b"fresh", 100)], cls)
    assert q.pop() == b"fresh"
    assert q.pop() is None
    reclaimed = q.pop_expired()
    assert len(reclaimed) == 1
    assert reclaimed[0] == ExpiredItem(
        item=b"old", deadline=5, reason=REASON_DEADLINE_REACHED
    )
    assert len(q) == 0
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_all_expired_reason_when_pop_drains_the_queue(cls, tmp_path):
    clock = FakeClock(10)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    push_head_first(q, [(b"a", 1), (b"b", 2), (b"c", 3)], cls)
    assert q.pop() is None  # nothing valid: whole queue is reclaimed at once
    reclaimed = q.pop_expired()
    assert [e.item for e in reclaimed] == [b"a", b"b", b"c"]
    assert [e.reason for e in reclaimed] == [
        REASON_DEADLINE_REACHED,
        REASON_ALL_EXPIRED,
        REASON_ALL_EXPIRED,
    ]
    # Reclaimed entries remember their own deadline.
    assert [e.deadline for e in reclaimed] == [1, 2, 3]
    assert q.pop_expired() == []
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_same_deadline_tasks_share_one_rule(cls, tmp_path):
    # Several tasks expiring at the same instant are treated identically.
    clock = FakeClock(49)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    q.push(b"x", deadline=50)
    q.push(b"y", deadline=50)
    q.push(b"z", deadline=50)
    order = [b"z", b"y", b"x"] if is_lifo(cls) else [b"x", b"y", b"z"]
    assert [q.pop(), q.pop(), q.pop()] == order  # all valid one instant before
    clock.set(50)
    q.push(b"p", deadline=50)
    q.push(b"q", deadline=50)
    assert q.pop() is None  # both expire together exactly at 50
    reclaimed = q.pop_expired()
    assert {e.item for e in reclaimed} == {b"p", b"q"}
    assert all(e.deadline == 50 for e in reclaimed)
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_clock_moving_backwards_raises(cls, tmp_path):
    values = iter([10, 5])
    q = ExpiringQueue(make_backing(cls, tmp_path), lambda: next(values))
    q.push(b"a", deadline=100)
    q.pop()  # reads 10
    with pytest.raises(ExpiredItemError, match="clock moved backwards"):
        q.pop()  # reads 5
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_explicit_now_moving_backwards_raises(cls, tmp_path):
    clock = FakeClock(10)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    q.push(b"a", deadline=100)
    q.peek(now=10)
    with pytest.raises(ExpiredItemError):
        q.peek(now=9)
    with pytest.raises(ExpiredItemError):
        q.pop(now=9)
    with pytest.raises(ExpiredItemError):
        q.pop_expired(now=9)
    # Equal time is allowed: only strictly smaller values are rejected.
    assert q.peek(now=10) == b"a"
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_invalid_push_arguments(cls, tmp_path):
    q = ExpiringQueue(make_backing(cls, tmp_path), FakeClock())
    with pytest.raises(TypeError):
        q.push("not-bytes", deadline=10)
    with pytest.raises(TypeError):
        q.push(b"a", deadline="10")
    with pytest.raises(ValueError, match="signed 64-bit"):
        q.push(b"a", deadline=2**63)
    with pytest.raises(ValueError, match="signed 64-bit"):
        q.push(b"a", deadline=-(2**63) - 1)
    with pytest.raises(TypeError):
        q.pop(now=1.5)
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_on_expired_callback(cls, tmp_path):
    seen: list[ExpiredItem] = []
    clock = FakeClock(10)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock, on_expired=seen.append)
    push_head_first(q, [(b"a", 1), (b"b", 2), (b"c", 100)], cls)
    assert q.pop() == b"c"
    assert [e.item for e in seen] == [b"a", b"b"]
    # The callback mirrors the reclamation list returned by pop_expired.
    assert q.pop_expired() == seen
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_every_pushed_item_ends_somewhere(cls, tmp_path):
    """Accounting invariant: each item is returned once or reclaimed once."""
    clock = FakeClock(10)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    pushed = [
        (b"a", 15),
        (b"b", 15),
        (b"c", 30),
        (b"d", 5),
        (b"e", 40),
        (b"f", 20),
    ]
    for item, deadline in pushed:
        q.push(item, deadline)

    valid: list[bytes] = []
    expired: list[bytes] = []
    valid.append(q.pop())  # two still-valid items at time 10
    valid.append(q.pop())
    clock.set(18)  # the deadline-15 items, and d, are now in the past
    while True:
        item = q.pop()
        if item is None:
            break
        valid.append(item)
    expired.extend(entry.item for entry in q.pop_expired())
    clock.set(40)
    assert q.pop() is None
    expired.extend(entry.item for entry in q.pop_expired())

    pushed_items = {item for item, _ in pushed}
    valid_set = set(valid)
    expired_set = set(expired)
    assert valid_set.isdisjoint(expired_set)
    assert valid_set | expired_set == pushed_items
    assert len(valid) + len(expired) == len(pushed)
    assert len(q) == 0
    q.close()


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_clear_removes_items_and_reclamation_buffer(cls, tmp_path):
    clock = FakeClock(10)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    # pop() reclaims the expired head into the buffer but leaves valid2 behind.
    push_head_first(q, [(b"expired", 5), (b"valid1", 100), (b"valid2", 100)], cls)
    assert q.pop() == b"valid1"
    assert len(q) == 1  # valid2 is still queued; b"expired" waits in the buffer
    q.clear()
    assert len(q) == 0
    assert q.peek() is None
    assert q.pop() is None
    assert q.pop_expired() == []  # clear() also discards reclaimed items
    q.push(b"fresh", deadline=100)
    assert q.pop() == b"fresh"
    q.close()


@pytest.mark.parametrize("cls", DISK_CLASSES)
def test_deadlines_survive_close_and_reopen(cls, tmp_path):
    clock = FakeClock(0)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    q.push(b"valid", deadline=100)
    q.push(b"expired", deadline=5)
    q.close()

    # A brand-new wrapper and clock: deadlines must come back from disk,
    # neither reset nor lost.
    clock = FakeClock(50)
    q = ExpiringQueue(make_backing(cls, tmp_path), clock)
    assert q.pop() == b"valid"
    assert q.pop() is None
    reclaimed = q.pop_expired()
    assert len(reclaimed) == 1
    assert reclaimed[0].item == b"expired"
    assert reclaimed[0].deadline == 5  # original deadline preserved
    assert len(q) == 0
    q.close()


def test_coupon_valid_and_expired_sequence():
    """Headline scenario: valid and expired coupon tasks are separated, and
    every enqueued task has exactly one destination."""
    clock = FakeClock(100)
    q = ExpiringQueue(FifoMemoryQueue(), clock)
    pushed = [
        (b"coupon-A", 105),  # valid at 100
        (b"coupon-B", 100),  # expires exactly at 100
        (b"coupon-C", 110),  # valid
        (b"coupon-D", 99),  # already expired
    ]
    for item, deadline in pushed:
        q.push(item, deadline)

    # Peek never silently drops the expired task sitting behind A.
    assert q.peek() == b"coupon-A"
    assert len(q) == 4

    served: list[bytes] = []
    served.append(q.pop())  # A, still valid
    assert served == [b"coupon-A"]

    # B has reached its deadline, so pop skips it and serves C instead.
    served.append(q.pop())
    assert served == [b"coupon-A", b"coupon-C"]

    # Only the expired D remains; pop reclaims it and reports nothing to serve.
    assert q.pop() is None
    assert len(q) == 0

    reclaimed = q.pop_expired()
    assert [(e.item, e.deadline) for e in reclaimed] == [
        (b"coupon-B", 100),
        (b"coupon-D", 99),
    ]
    assert all(e.reason == REASON_DEADLINE_REACHED for e in reclaimed)

    pushed_items = {item for item, _ in pushed}
    served_set = set(served)
    reclaimed_set = {e.item for e in reclaimed}
    assert served_set.isdisjoint(reclaimed_set)
    assert served_set | reclaimed_set == pushed_items
    assert len(served) + len(reclaimed) == len(pushed)


def test_priority_queue_expiration_accounting():
    clock = FakeClock(10)
    sink: list[ExpiredItem] = []
    pq = PriorityQueue(
        lambda prio: ExpiringQueue(FifoMemoryQueue(), clock, on_expired=sink.append)
    )
    pushed = [
        (b"a", 1, 5),  # expires
        (b"b", 1, 100),  # valid
        (b"c", 2, 50),  # valid, lower priority
        (b"d", 2, 5),  # expires
    ]
    for item, prio, deadline in pushed:
        pq.push(item, prio, deadline=deadline)

    # The highest-priority bucket starts with expired a: peek cannot skip it
    # and must not evict it, so it reports None while all four items remain.
    assert len(pq) == 4
    assert pq.peek() is None
    assert len(pq) == 4

    # pop filters the expired heads and serves the valid items in priority.
    served: list[bytes] = []
    while True:
        item = pq.pop()
        if item is None:
            break
        served.append(item)
    assert served == [b"b", b"c"]
    assert sorted(e.item for e in sink) == [b"a", b"d"]
    assert {e.item for e in sink}.isdisjoint(served)
    assert set(served) | {e.item for e in sink} == {item for item, _, _ in pushed}
    assert len(pq) == 0


def test_round_robin_queue_expiration_accounting():
    clock = FakeClock(10)
    sink: list[ExpiredItem] = []
    rr = RoundRobinQueue(
        lambda key: ExpiringQueue(FifoMemoryQueue(), clock, on_expired=sink.append)
    )
    pushed = [
        (b"x", "A", 100),  # valid
        (b"y1", "B", 5),  # expires
        (b"y2", "B", 50),  # valid
        (b"y3", "B", 5),  # expires
    ]
    for item, key, deadline in pushed:
        rr.push(item, key, deadline=deadline)

    served: list[bytes] = []
    while True:
        item = rr.pop()
        if item is None:
            break
        served.append(item)
    assert served == [b"x", b"y2"]
    assert sorted(e.item for e in sink) == [b"y1", b"y3"]
    assert set(served) | {e.item for e in sink} == {item for item, _, _ in pushed}
    assert len(rr) == 0


def test_priority_queue_skips_a_fully_expired_top_bucket():
    # The highest-priority bucket contains only expired items: pop() must not
    # return None while a lower-priority bucket still has valid work.
    clock = FakeClock(10)
    sink: list[ExpiredItem] = []
    pq = PriorityQueue(
        lambda prio: ExpiringQueue(FifoMemoryQueue(), clock, on_expired=sink.append)
    )
    pq.push(b"dead1", 0, deadline=1)
    pq.push(b"dead2", 0, deadline=2)
    pq.push(b"alive", 1, deadline=100)
    assert pq.peek() is None  # expired top bucket head, nothing is evicted
    assert pq.pop() == b"alive"  # the top bucket is reclaimed, then b"alive"
    assert pq.pop() is None
    assert sorted(e.item for e in sink) == [b"dead1", b"dead2"]
    assert len(pq) == 0


def test_round_robin_queue_skips_a_fully_expired_bucket():
    clock = FakeClock(10)
    sink: list[ExpiredItem] = []
    rr = RoundRobinQueue(
        lambda key: ExpiringQueue(FifoMemoryQueue(), clock, on_expired=sink.append)
    )
    rr.push(b"dead", "A", deadline=1)
    rr.push(b"alive", "B", deadline=100)
    assert rr.pop() == b"alive"  # bucket A is reclaimed, round-robin moves to B
    assert rr.pop() is None
    assert [e.item for e in sink] == [b"dead"]
    assert len(rr) == 0


def test_round_robin_peek_blocked_by_expired_head():
    # The bucket that pop() would serve first is blocked by an expired head.
    # peek() must report None rather than jump to another key's valid item.
    clock = FakeClock(10)
    rr = RoundRobinQueue(lambda key: ExpiringQueue(FifoMemoryQueue(), clock))
    rr.push(b"a-expired", "A", deadline=1)
    rr.push(b"a-valid", "A", deadline=100)
    rr.push(b"b-valid", "B", deadline=100)
    assert rr.peek() is None
    assert rr.pop() == b"a-valid"  # reclaims a-expired, then serves a-valid
    assert rr.peek() == b"b-valid"
    assert rr.pop() == b"b-valid"
    assert rr.pop() is None


def test_deadline_requires_expiring_inner_queue():
    pq = PriorityQueue(lambda prio: FifoMemoryQueue())
    with pytest.raises(TypeError):
        pq.push(b"a", 0, deadline=10)
    rr = RoundRobinQueue(lambda key: FifoMemoryQueue())
    with pytest.raises(TypeError):
        rr.push(b"a", "k", deadline=10)


def test_deadline_queue_methods_are_abstract():
    q = DeadlineQueue()
    with pytest.raises(NotImplementedError):
        q.push(b"a", 1)
    with pytest.raises(NotImplementedError):
        q.pop()
    with pytest.raises(NotImplementedError):
        q.peek()
    with pytest.raises(NotImplementedError):
        q.pop_expired()
    with pytest.raises(NotImplementedError):
        len(q)


def test_plain_item_in_expiring_queue_is_rejected():
    # Corruption guard: a deadline wrapper only understands its own records.
    clock = FakeClock(0)
    backing = FifoMemoryQueue()
    backing.push(b"raw-without-deadline")
    q = ExpiringQueue(backing, clock)
    with pytest.raises(ExpiredItemError):
        q.peek()
    with pytest.raises(ExpiredItemError):
        q.pop()
