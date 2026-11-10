from __future__ import annotations

import heapq
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from queuelib import LateRecord, MergingQueue, ShardState


def make_queue() -> MergingQueue[str]:
    return MergingQueue(("A", "B", "C"))


def test_empty_queue_emits_nothing_without_watermarks() -> None:
    queue = make_queue()
    assert queue.pop() is None
    assert queue.peek() is None
    assert len(queue) == 0
    assert not queue.is_closed
    assert MergingQueue().is_closed is False
    assert MergingQueue().pop() is None


def test_missing_watermark_blocks_even_when_items_are_buffered() -> None:
    queue = make_queue()
    queue.push("A", 1, "a1")
    queue.advance_watermark("A", 1)
    queue.advance_watermark("C", 1)
    # B has neither data nor a watermark: nothing is certain yet.
    assert queue.pop() is None
    assert queue.peek() is None
    assert len(queue) == 1


def test_temporarily_empty_shard_is_not_assumed_finished() -> None:
    queue = make_queue()
    queue.push("A", 1, "a1")
    queue.advance_watermark("A", 1)
    # B reports progress but has no items.
    queue.advance_watermark("B", 1)
    queue.push("C", 2, "c1")
    queue.advance_watermark("C", 2)

    assert queue.pop() == "a1"

    # B's watermark (1) is now the barrier; c1 at t=2 must wait even though B
    # has an empty buffer.
    assert queue.pop() is None
    assert len(queue) == 1

    # B advances without ever producing data; A is still catching up.
    queue.advance_watermark("B", 2)
    assert queue.pop() is None

    # Once every active shard promises t>=2, the held item becomes certain.
    queue.advance_watermark("A", 2)
    assert queue.peek() == "c1"
    assert queue.pop() == "c1"
    assert queue.pop() is None


def test_tie_break_by_shard_id_then_sequence() -> None:
    queue: MergingQueue[str] = MergingQueue(("s2", "s1"))
    for shard in ("s1", "s2"):
        queue.advance_watermark(shard, 5)

    queue.push("s2", 5, "s2-first", sequence=0)
    queue.push("s2", 5, "s2-second", sequence=1)
    queue.push("s1", 5, "s1-second", sequence=1)
    queue.push("s1", 5, "s1-first", sequence=0)

    assert [queue.pop() for _ in range(4)] == [
        "s1-first",
        "s1-second",
        "s2-first",
        "s2-second",
    ]


def test_sequences_are_assigned_per_shard_and_gaps_are_allowed() -> None:
    queue = make_queue()
    for shard in ("A", "B", "C"):
        queue.advance_watermark(shard, 10)
    assert queue.push("A", 1, "a0") == 0
    assert queue.push("A", 2, "a5", sequence=5) == 5
    assert queue.push("A", 3, "a6") == 6
    with pytest.raises(ValueError, match="already used"):
        queue.push("A", 4, "bad", sequence=5)
    # Out-of-order but unseen explicit sequences are accepted.
    assert queue.push("A", 5, "a3", sequence=3) == 3


def test_three_shards_interleaved_advance() -> None:
    queue = make_queue()

    # Round 1: B has no data but still reports a watermark.
    queue.push("A", 1, "a1")
    queue.advance_watermark("A", 1)
    queue.advance_watermark("B", 1)
    queue.push("C", 1, "c1")
    queue.advance_watermark("C", 1)

    assert queue.pop() == "a1"
    assert queue.pop() == "c1"
    assert queue.committed_watermark == 1

    # Round 2: B lags behind while A and C move on.
    queue.push("A", 3, "a3")
    queue.advance_watermark("A", 3)
    queue.push("C", 2, "c2")
    queue.advance_watermark("C", 2)
    assert queue.pop() is None  # barrier is B's watermark at 1

    queue.advance_watermark("B", 2)
    assert queue.pop() == "c2"

    # B finally produces data at the timestamp it just promised.
    queue.push("B", 2, "b2")
    assert queue.pop() == "b2"
    assert queue.pop() is None  # a3 is still beyond the barrier

    # Both lagging shards must promise t>=3 before a3 is certain.
    queue.advance_watermark("C", 3)
    assert queue.pop() is None
    queue.advance_watermark("B", 3)
    assert queue.pop() == "a3"
    assert queue.pop() is None


def test_late_items_use_a_separate_channel() -> None:
    queue = make_queue()
    for shard in ("A", "B", "C"):
        queue.advance_watermark(shard, 5)

    queue.push("A", 5, "a5")
    queue.push("B", 5, "b5")
    queue.push("C", 5, "c5")
    assert [queue.pop() for _ in range(3)] == ["a5", "b5", "c5"]
    assert queue.committed_watermark == 5

    # C now admits an item older than what was already emitted.
    queue.push("C", 3, "c3-late")
    assert queue.pop() is None
    assert queue.late_size == 1
    late = queue.pop_late()
    assert isinstance(late, LateRecord)
    assert late == LateRecord(shard="C", sequence=1, timestamp=3, value="c3-late")
    assert late.timestamp < queue.committed_watermark
    assert queue.pop_late() is None
    assert queue.peek_late() is None

    # New in-time items still flow through the ordered channel normally.
    queue.push("A", 6, "a6")
    for shard in ("A", "B", "C"):
        queue.advance_watermark(shard, 6)
    assert queue.peek() == "a6"
    assert queue.pop() == "a6"


def test_late_channel_keeps_detection_order_and_peek_does_not_consume() -> None:
    queue = make_queue()
    for shard in ("A", "B", "C"):
        queue.advance_watermark(shard, 5)
    queue.push("A", 5, "a5")
    queue.pop()
    assert queue.committed_watermark == 5

    # Late items from several shards plus one still in-time item.
    queue.push("B", 2, "b2-late")
    queue.push("A", 4, "a4-late")
    queue.push("C", 1, "c1-late")
    queue.push("C", 6, "c6")
    for shard in ("A", "B", "C"):
        queue.advance_watermark(shard, 6)

    # The in-time item is still served through the ordered channel.
    assert queue.pop() == "c6"
    assert queue.late_size == 3
    assert queue.peek_late() == LateRecord("A", 1, 4, "a4-late")
    assert queue.peek_late() == LateRecord("A", 1, 4, "a4-late")
    assert [queue.pop_late() for _ in range(3)] == [
        LateRecord("A", 1, 4, "a4-late"),
        LateRecord("B", 0, 2, "b2-late"),
        LateRecord("C", 0, 1, "c1-late"),
    ]
    assert queue.pop_late() is None


def test_equal_timestamp_after_commit_is_not_late() -> None:
    queue = make_queue()
    for shard in ("A", "B", "C"):
        queue.advance_watermark(shard, 5)
    queue.push("A", 5, "a5")
    assert queue.pop() == "a5"
    assert queue.committed_watermark == 5

    # "Late" means strictly below the committed watermark: an equal timestamp
    # still flows through the ordered channel, not the late one.
    queue.push("B", 5, "b5-equal")
    assert queue.pop() == "b5-equal"
    assert queue.pop_late() is None
    assert queue.late_size == 0


def test_watermark_cannot_move_backwards() -> None:
    queue = make_queue()
    queue.advance_watermark("A", 5)
    with pytest.raises(ValueError, match="backwards"):
        queue.advance_watermark("A", 4)


def test_closed_shard_leaves_the_barrier_and_rejoin_keeps_state() -> None:
    queue = make_queue()
    queue.advance_watermark("A", 10)
    queue.advance_watermark("B", 10)
    queue.advance_watermark("C", 10)
    queue.push("A", 8, "a8")
    queue.push("B", 9, "b9")

    queue.close_shard("B")
    assert queue.shard_state("B") is ShardState.CLOSED
    assert queue.active_shards == ["A", "C"]
    with pytest.raises(RuntimeError, match="closed"):
        queue.push("B", 9, "rejected")
    with pytest.raises(RuntimeError, match="closed"):
        queue.advance_watermark("B", 11)

    # A and C still gate output; closing B did not close the whole queue.
    assert not queue.is_closed
    assert queue.pop() == "a8"

    queue.close_shard("A")
    queue.close_shard("C")
    assert queue.is_closed
    # With no active shards, every buffered item is provably final.
    assert queue.pop() == "b9"
    assert queue.pop() is None

    # Rejoining restores participation; the old watermark and sequence counter
    # are preserved.
    queue.reopen_shard("B")
    assert queue.shard_state("B") is ShardState.ACTIVE
    assert queue.shard_watermark("B") == 10
    assert queue.push("B", 11, "b11") == 1  # B already pushed sequence 0
    with pytest.raises(RuntimeError, match="already active"):
        queue.reopen_shard("B")
    with pytest.raises(RuntimeError, match="already closed"):
        queue.close_shard("A")


def test_late_registered_shard_blocks_until_it_reports() -> None:
    queue: MergingQueue[str] = MergingQueue(("A", "B"))
    queue.advance_watermark("A", 2)
    queue.advance_watermark("B", 2)
    queue.push("A", 1, "a1")
    assert queue.pop() == "a1"

    queue.register_shard("D")
    assert queue.active_shards == ["A", "B", "D"]
    queue.push("A", 4, "a4")
    queue.advance_watermark("A", 4)
    queue.advance_watermark("B", 4)
    # D has no watermark yet: it could produce anything, so output is blocked.
    assert queue.pop() is None

    queue.advance_watermark("D", 4)
    assert queue.pop() == "a4"


def test_unknown_and_duplicate_shards_are_rejected() -> None:
    queue = make_queue()
    with pytest.raises(KeyError, match="unknown shard"):
        queue.push("Z", 1, "z")
    with pytest.raises(KeyError, match="unknown shard"):
        queue.advance_watermark("Z", 1)
    with pytest.raises(ValueError, match="already registered"):
        queue.register_shard("A")


def test_clear_resets_buffers_but_keeps_shards() -> None:
    queue = make_queue()
    for shard in ("A", "B", "C"):
        queue.advance_watermark(shard, 5)
    queue.push("A", 5, "a5")
    queue.push("B", 2, "b2-late-bound")
    queue.pop()
    queue.push("C", 1, "c1-late")
    queue.clear()

    assert len(queue) == 0
    assert queue.late_size == 0
    assert queue.pop() is None
    assert queue.pop_late() is None
    assert queue.committed_watermark is None
    assert queue.active_shards == ["A", "B", "C"]
    assert queue.shard_watermark("A") is None

    queue.advance_watermark("A", 1)
    queue.advance_watermark("B", 1)
    queue.advance_watermark("C", 1)
    queue.push("A", 1, "fresh")
    assert queue.pop() == "fresh"


def test_peek_matches_pop_and_does_not_consume() -> None:
    queue = make_queue()
    for shard in ("A", "B", "C"):
        queue.advance_watermark(shard, 3)
    queue.push("A", 1, "a1")
    queue.push("B", 2, "b2")
    assert queue.peek() == "a1"
    assert queue.peek() == "a1"
    assert queue.pop() == "a1"
    assert queue.peek() == "b2"
    assert queue.pop() == "b2"
    assert queue.peek() is None


@given(
    st.lists(
        st.one_of(
            st.tuples(st.just("push"), st.integers(0, 2), st.integers(0, 6)),
            st.tuples(st.just("watermark"), st.integers(0, 2), st.integers(0, 6)),
            st.tuples(st.just("close"), st.integers(0, 2)),
            st.tuples(st.just("reopen"), st.integers(0, 2)),
            st.just(("pop",)),
            st.just(("pop_late",)),
        ),
        max_size=120,
    )
)
def test_merge_invariants_under_random_interleaving(
    ops: list[tuple[Any, ...]],
) -> None:
    shard_ids = ("A", "B", "C")
    queue: MergingQueue[str] = MergingQueue(shard_ids)

    # Faithful model of the two internal stages: per-shard heaps of buffered
    # entries, then a heap of entries whose global order is already certain.
    buffered: dict[str, list[tuple[int, int, str]]] = {shard: [] for shard in shard_ids}
    ready: list[tuple[int, str, int, str]] = []
    late: list[LateRecord[str]] = []
    sequences = dict.fromkeys(shard_ids, 0)
    watermarks: dict[str, int] = {}
    active = dict.fromkeys(shard_ids, True)
    committed: int | None = None
    pushed = 0
    received_normal = 0
    received_late = 0
    last_key: tuple[int, str, int] | None = None

    def pump() -> None:
        nonlocal committed
        active_ids = [shard for shard in shard_ids if active[shard]]
        if not active_ids:
            bound: int | None = None
        elif any(shard not in watermarks for shard in active_ids):
            return
        else:
            bound = min(watermarks[shard] for shard in active_ids)
        for shard in shard_ids:
            heap = buffered[shard]
            while heap and (bound is None or heap[0][0] <= bound):
                timestamp, sequence, value = heapq.heappop(heap)
                if committed is not None and timestamp < committed:
                    late.append(LateRecord(shard, sequence, timestamp, value))
                else:
                    heapq.heappush(ready, (timestamp, shard, sequence, value))

    for op in ops:
        kind = op[0]
        if kind == "push":
            _, idx, timestamp = op
            shard = shard_ids[idx]
            # Keep inputs inside the ordering contract: no item at a timestamp
            # already covered by the shard's watermark or already committed.
            # Out-of-contract arrivals get dedicated deterministic tests.
            if (
                not active[shard]
                or (shard in watermarks and timestamp <= watermarks[shard])
                or (committed is not None and timestamp <= committed)
            ):
                continue
            sequence = sequences[shard]
            value = f"{shard}-{timestamp}-{pushed}"
            queue.push(shard, timestamp, value)
            heapq.heappush(buffered[shard], (timestamp, sequence, value))
            sequences[shard] += 1
            pushed += 1
        elif kind == "watermark":
            _, idx, timestamp = op
            shard = shard_ids[idx]
            if active[shard] and (
                shard not in watermarks or timestamp >= watermarks[shard]
            ):
                queue.advance_watermark(shard, timestamp)
                watermarks[shard] = timestamp
        elif kind == "close":
            shard = shard_ids[op[1]]
            if active[shard]:
                queue.close_shard(shard)
                active[shard] = False
        elif kind == "reopen":
            shard = shard_ids[op[1]]
            if not active[shard]:
                queue.reopen_shard(shard)
                active[shard] = True
        elif kind == "pop":
            if not ready:
                pump()
            item = queue.pop()
            if not ready:
                assert item is None
                assert queue.peek() is None
                continue
            timestamp, shard, sequence, value = heapq.heappop(ready)
            assert item == value
            key = (timestamp, shard, sequence)
            if last_key is not None:
                assert key >= last_key
            last_key = key
            committed = timestamp if committed is None else max(committed, timestamp)
            received_normal += 1
        else:
            record = queue.pop_late()
            if not late:
                assert record is None
                continue
            expected_record = late.pop(0)
            assert record == expected_record
            assert committed is not None
            assert record.timestamp < committed
            received_late += 1

    # With every shard closed, the barrier disappears and all remaining items
    # must surface exactly once through one of the two channels. Pumping stays
    # lazy (only when the ready stage is empty) to match the implementation.
    for shard in shard_ids:
        if active[shard]:
            queue.close_shard(shard)
            active[shard] = False
    while True:
        if not ready:
            pump()
        if not ready:
            assert queue.pop() is None
            break
        timestamp, shard, sequence, value = heapq.heappop(ready)
        assert queue.pop() == value
        key = (timestamp, shard, sequence)
        if last_key is not None:
            assert key >= last_key
        last_key = key
        received_normal += 1
    while late:
        record = late.pop(0)
        assert queue.pop_late() == record
        received_late += 1
    assert queue.pop_late() is None
    assert received_normal + received_late == pushed
    assert all(not heap for heap in buffered.values())
    assert len(queue) == 0
