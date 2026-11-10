from __future__ import annotations

import tempfile

import pytest
from hypothesis import given
from hypothesis import strategies as st

from queuelib import (
    REASON_FULL,
    REASON_NO_EVICTABLE,
    REASON_OVERSIZED,
    EvictionOrder,
    EvictionPriority,
    OverflowPolicy,
    PushResult,
    SizedQueue,
    SizedQueueConfig,
    SizedTask,
)
from queuelib.tests import QueuelibTestCase


def config(**overrides):
    base = {
        "capacity": 10,
        "on_full": OverflowPolicy.REJECT,
        "eviction_priority": EvictionPriority.LOWEST_VALUE,
        "eviction_order": EvictionOrder.FIFO,
    }
    base.update(overrides)
    return SizedQueueConfig(**base)


class TestConfigValidation:
    def test_capacity_must_be_positive_int(self):
        with pytest.raises(ValueError, match="positive integer"):
            SizedQueueConfig(capacity=0)
        with pytest.raises(ValueError, match="positive integer"):
            SizedQueueConfig(capacity=-1)

    def test_capacity_rejects_bool_and_float(self):
        with pytest.raises(TypeError, match="capacity must be an int"):
            SizedQueueConfig(capacity=True)
        with pytest.raises(TypeError, match="capacity must be an int"):
            SizedQueueConfig(capacity=1.5)

    def test_policy_members_are_validated(self):
        with pytest.raises(TypeError, match="OverflowPolicy"):
            SizedQueueConfig(capacity=1, on_full="reject")


class TestTaskValidation:
    def test_size_must_be_non_negative_int(self):
        with pytest.raises(ValueError, match="non-negative"):
            SizedTask(item="x", size=-1)
        with pytest.raises(TypeError, match="task size must be an int"):
            SizedTask(item="x", size=1.0)
        with pytest.raises(TypeError, match="task size must be an int"):
            SizedTask(item="x", size=True)

    def test_zero_size_is_allowed(self):
        task = SizedTask(item="x", size=0)
        assert task.size == 0

    def test_priority_and_pinned_types(self):
        with pytest.raises(TypeError, match="task priority must be an int"):
            SizedTask(item="x", size=1, priority=1.0)
        with pytest.raises(TypeError, match="task pinned must be a bool"):
            SizedTask(item="x", size=1, pinned="yes")


class TestReasonsAndResultType(QueuelibTestCase):
    def test_each_outcome_uses_a_named_reason_constant(self):
        # The literal values are the durable contract; the named constants are
        # what callers import and match on.
        assert REASON_FULL == "full"
        assert REASON_OVERSIZED == "oversized"
        assert REASON_NO_EVICTABLE == "no_evictable"

        reject_q = SizedQueue(self.qpath / "reject", config())
        reject_q.push(SizedTask(item="a", size=10))
        result = reject_q.push(SizedTask(item="b", size=1))
        assert isinstance(result, PushResult)
        assert result.reason is REASON_FULL
        reject_q.close()

        oversized_q = SizedQueue(self.qpath / "oversized", config())
        result = oversized_q.push(SizedTask(item="big", size=11))
        assert result.reason is REASON_OVERSIZED
        oversized_q.close()

        evict_q = SizedQueue(
            self.qpath / "pinned",
            config(on_full=OverflowPolicy.EVICT),
        )
        evict_q.push(SizedTask(item="p", size=10, pinned=True))
        result = evict_q.push(SizedTask(item="blocked", size=1))
        assert result.reason is REASON_NO_EVICTABLE
        evict_q.close()

    def test_accepted_result_has_no_reason(self):
        q = SizedQueue(self.qpath, config())
        result = q.push(SizedTask(item="a", size=1))
        assert isinstance(result, PushResult)
        assert result.reason is None
        assert result.returned == []
        q.close()


class RejectPolicyTest(QueuelibTestCase):
    def queue(self):
        return SizedQueue(self.qpath, config())

    def test_reject_when_full_returns_complete_task(self):
        q = self.queue()
        first = SizedTask(item="first", size=6, priority=1)
        second = SizedTask(item="second", size=4, priority=2)
        assert q.push(first).accepted
        assert q.push(second).accepted
        assert len(q) == 2
        assert q.used == 10

        incoming = SizedTask(item="third", size=1, priority=3)
        result = q.push(incoming)
        assert result.accepted is False
        assert result.reason == "full"
        assert result.rejected is incoming
        assert result.returned == [incoming]
        # The rejected task carries every field the caller declared.
        assert result.rejected.item == "third"
        assert result.rejected.size == 1
        assert result.rejected.priority == 3
        # Rejecting changes nothing inside the queue.
        assert len(q) == 2
        assert q.used == 10
        assert [t.item for t in q._items.values()] == ["first", "second"]
        q.close()

    def test_oversized_task_is_rejected_without_touching_queue(self):
        q = self.queue()
        q.push(SizedTask(item="kept", size=4))
        oversized = SizedTask(item="huge", size=11)
        result = q.push(oversized)
        assert result.accepted is False
        assert result.reason == "oversized"
        assert result.rejected is oversized
        assert result.evicted == []
        assert len(q) == 1
        assert q.used == 4
        q.close()

    def test_pop_frees_capacity_and_allows_retry(self):
        q = self.queue()
        q.push(SizedTask(item="a", size=6))
        q.push(SizedTask(item="b", size=4))
        blocked = SizedTask(item="c", size=1)
        assert q.push(blocked).reason == "full"
        assert q.pop().item == "a"
        assert q.used == 4
        result = q.push(blocked)
        assert result.accepted
        assert q.used == 5
        q.close()

    def test_peek_does_not_consume(self):
        q = self.queue()
        assert q.peek() is None
        task = SizedTask(item="a", size=1)
        q.push(task)
        assert q.peek() is task
        assert q.peek() is task
        assert len(q) == 1
        q.close()


class EvictionPolicyTest(QueuelibTestCase):
    def queue(self, **overrides):
        return SizedQueue(self.qpath, config(on_full=OverflowPolicy.EVICT, **overrides))

    def test_small_capacity_worked_example_tracks_every_move(self):
        q = self.queue()
        evicted = []
        rejected = []

        result = q.push(SizedTask(item="A", size=6, priority=1))
        assert result.accepted
        assert result.evicted == []
        assert len(q) == 1
        assert q.used == 6

        result = q.push(SizedTask(item="B", size=4, priority=2))
        assert result.accepted
        assert result.evicted == []
        assert len(q) == 2
        assert q.used == 10

        # C needs 4 more while 10 are used; the lowest-value task is A (prio 1).
        result = q.push(SizedTask(item="C", size=4, priority=1))
        assert result.accepted
        assert [t.item for t in result.evicted] == ["A"]
        evicted.extend(result.evicted)
        assert len(q) == 2
        assert q.used == 8
        assert [t.item for t in q._items.values()] == ["B", "C"]

        # D has the highest priority; C (prio 1) is now the lowest-value task.
        result = q.push(SizedTask(item="D", size=3, priority=3))
        assert result.accepted
        assert [t.item for t in result.evicted] == ["C"]
        evicted.extend(result.evicted)
        assert len(q) == 2
        assert q.used == 7
        assert [t.item for t in q._items.values()] == ["B", "D"]

        # An oversized task can never be admitted and must not cascade-evict.
        result = q.push(SizedTask(item="E", size=11, priority=0))
        assert result.accepted is False
        assert result.reason == "oversized"
        rejected.append(result.rejected)
        assert len(q) == 2
        assert q.used == 7
        assert [t.item for t in q._items.values()] == ["B", "D"]

        # Everything handed back is the original, fully populated task.
        assert {t.item for t in evicted} == {"A", "C"}
        assert [t.item for t in rejected] == ["E"]
        assert all(isinstance(t, SizedTask) for t in [*evicted, *rejected])

        # The surviving tasks leave through pop() in admission (FIFO) order.
        assert q.pop().item == "B"
        assert q.pop().item == "D"
        assert q.pop() is None
        assert q.used == 0
        stats = q.stats
        assert (stats.submitted, stats.accepted, stats.rejected, stats.evicted) == (
            5,
            4,
            1,
            2,
        )
        q.close()

    def test_oversized_task_never_empties_the_queue(self):
        q = self.queue()
        for name in ("a", "b", "c"):
            assert q.push(SizedTask(item=name, size=3)).accepted
        assert q.used == 9
        result = q.push(SizedTask(item="big", size=11))
        assert result.accepted is False
        assert result.reason == "oversized"
        assert result.evicted == []
        # All three waiting tasks are still present, no eviction loop ran.
        assert len(q) == 3
        assert q.used == 9
        q.close()

    def test_same_value_fifo_evicts_oldest_first(self):
        q = self.queue(eviction_order=EvictionOrder.FIFO)
        older = SizedTask(item="older", size=4, priority=0)
        newer = SizedTask(item="newer", size=4, priority=0)
        q.push(older)
        q.push(newer)
        result = q.push(SizedTask(item="incoming", size=4, priority=0))
        assert [t.item for t in result.evicted] == ["older"]
        assert [t.item for t in q._items.values()] == ["newer", "incoming"]
        q.close()

    def test_same_value_lifo_evicts_newest_first(self):
        q = self.queue(eviction_order=EvictionOrder.LIFO)
        older = SizedTask(item="older", size=4, priority=0)
        newer = SizedTask(item="newer", size=4, priority=0)
        q.push(older)
        q.push(newer)
        result = q.push(SizedTask(item="incoming", size=4, priority=0))
        assert [t.item for t in result.evicted] == ["newer"]
        assert [t.item for t in q._items.values()] == ["older", "incoming"]
        q.close()

    def test_highest_value_priority_evicts_high_priority_first(self):
        q = self.queue(eviction_priority=EvictionPriority.HIGHEST_VALUE)
        high = SizedTask(item="high", size=4, priority=5)
        low = SizedTask(item="low", size=4, priority=1)
        q.push(high)
        q.push(low)
        result = q.push(SizedTask(item="incoming", size=4, priority=3))
        assert [t.item for t in result.evicted] == ["high"]
        assert [t.item for t in q._items.values()] == ["low", "incoming"]
        q.close()

    def test_pinned_tasks_are_never_evicted(self):
        q = self.queue()
        pinned = SizedTask(item="pinned", size=6, priority=9, pinned=True)
        disposable = SizedTask(item="disposable", size=4, priority=0)
        q.push(pinned)
        q.push(disposable)
        assert q.used == 10

        # Reclaim the only unpinned task; the pinned one stays.
        result = q.push(SizedTask(item="one", size=1, priority=0))
        assert [t.item for t in result.evicted] == ["disposable"]
        assert [t.item for t in q._items.values()] == ["pinned", "one"]
        q.close()

    def test_reject_when_only_pinned_tasks_remain(self):
        q = self.queue()
        q.push(SizedTask(item="pinned", size=10, pinned=True))
        assert q.used == 10
        result = q.push(SizedTask(item="blocked", size=1))
        assert result.accepted is False
        assert result.reason == "no_evictable"
        assert result.evicted == []
        assert [t.item for t in q._items.values()] == ["pinned"]
        assert q.used == 10
        q.close()

    def test_partial_eviction_then_stall_returns_every_removed_task(self):
        q = self.queue()
        q.push(SizedTask(item="pinned", size=6, pinned=True))
        q.push(SizedTask(item="disposable", size=4, priority=0))
        assert q.used == 10
        # Removing the 4-unit task still leaves 6 + 5 > 10, and the pinned
        # task cannot be reclaimed, so admission fails after one eviction.
        incoming = SizedTask(item="blocked", size=5)
        result = q.push(incoming)
        assert result.accepted is False
        assert result.reason == "no_evictable"
        assert result.rejected is incoming
        assert [t.item for t in result.evicted] == ["disposable"]
        assert [t.item for t in result.returned] == ["blocked", "disposable"]
        # The pinned task survives; the queue was not emptied to force a fit.
        assert [t.item for t in q._items.values()] == ["pinned"]
        assert q.used == 6
        assert q.stats.evicted == 1
        assert q.stats.rejected == 1
        q.close()

    def test_eviction_chain_reclaims_enough_room(self):
        q = self.queue()
        q.push(SizedTask(item="a", size=2, priority=1))
        q.push(SizedTask(item="b", size=2, priority=1))
        q.push(SizedTask(item="c", size=2, priority=2))
        assert q.used == 6
        result = q.push(SizedTask(item="big", size=9, priority=0))
        assert result.accepted
        # Removing only the two prio-1 tasks still leaves 2 + 9 > 10, so the
        # prio-2 task has to go as well before the big task fits.
        assert [t.item for t in result.evicted] == ["a", "b", "c"]
        assert q.used == 9
        assert [t.item for t in q._items.values()] == ["big"]
        q.close()


class DeclaredSizeTest(QueuelibTestCase):
    def test_accounting_uses_declared_size_not_object_footprint(self):
        q = SizedQueue(self.qpath, config(capacity=5))
        small_item = "x"
        large_item = {"url": "https://example.com/" + "path/" * 100}
        q.push(SizedTask(item=small_item, size=4))
        # The large object counts as exactly one declared unit, not its size.
        q.push(SizedTask(item=large_item, size=1))
        assert q.used == 5
        result = q.push(SizedTask(item="blocked", size=1))
        assert result.reason == "full"
        assert q.pop().item is small_item
        assert q.pop().item is large_item
        q.close()


class StatsTest(QueuelibTestCase):
    def test_counters_cover_every_outcome(self):
        q = SizedQueue(self.qpath, config(capacity=5))
        q.push(SizedTask(item="a", size=2))
        q.push(SizedTask(item="b", size=3))
        q.push(SizedTask(item="rejected", size=1))
        q.pop()
        q.push(SizedTask(item="c", size=2))
        stats = q.stats
        assert stats.submitted == stats.accepted + stats.rejected
        assert (stats.submitted, stats.accepted, stats.rejected) == (4, 3, 1)
        q.close()

    def test_clear_keeps_statistics(self):
        q = SizedQueue(self.qpath, config(on_full=OverflowPolicy.EVICT))
        q.push(SizedTask(item="a", size=4))
        q.push(SizedTask(item="b", size=7))
        q.push(SizedTask(item="c", size=4))
        before = q.stats
        q.clear()
        assert len(q) == 0
        assert q.used == 0
        assert q.stats == before
        q.push(SizedTask(item="d", size=1))
        assert q.stats.submitted == before.submitted + 1
        q.close()

    def test_stats_exposes_a_copy(self):
        q = SizedQueue(self.qpath, config())
        q.push(SizedTask(item="a", size=1))
        snapshot = q.stats
        snapshot.submitted = 999
        assert q.stats.submitted == 1
        q.close()


class PersistenceTest(QueuelibTestCase):
    def test_restart_restores_tasks_config_and_stats(self):
        cfg = config(capacity=10, on_full=OverflowPolicy.EVICT)
        q = SizedQueue(self.qpath, cfg)
        q.push(SizedTask(item="a", size=4, priority=2))
        q.push(SizedTask(item="b", size=3, priority=1))
        # c does not fit while a and b are held; the lowest-value task b goes.
        q.push(SizedTask(item="c", size=4, priority=1))
        q.close()
        assert q.stats.evicted == 1

        reopened = SizedQueue(self.qpath)
        assert reopened.config == cfg
        assert reopened.capacity == 10
        assert len(reopened) == 2
        assert reopened.used == 8
        assert [t.item for t in reopened._items.values()] == ["a", "c"]
        assert reopened.stats == q.stats
        assert reopened.pop().item == "a"
        assert reopened.pop().item == "c"
        reopened.close()

    def test_restart_accepts_matching_config(self):
        cfg = config()
        q = SizedQueue(self.qpath, cfg)
        q.push(SizedTask(item="a", size=1))
        q.close()
        reopened = SizedQueue(self.qpath, cfg)
        assert reopened.config == cfg
        assert reopened.pop().item == "a"
        reopened.close()

    def test_restart_rejects_mismatched_config(self):
        q = SizedQueue(self.qpath, config(capacity=10))
        q.push(SizedTask(item="a", size=1))
        q.close()
        with pytest.raises(ValueError, match="does not match"):
            SizedQueue(self.qpath, config(capacity=11))

    def test_new_queue_requires_config(self):
        with pytest.raises(ValueError, match="config is required"):
            SizedQueue(self.qpath)

    def test_restart_without_close_keeps_last_durable_state(self):
        cfg = config()
        q = SizedQueue(self.qpath, cfg)
        q.push(SizedTask(item="a", size=2))
        q.push(SizedTask(item="b", size=3))
        # No close(): every mutating call already rewrites the state file, so
        # reopening a surviving process reads the latest committed state.
        del q
        reopened = SizedQueue(self.qpath)
        assert len(reopened) == 2
        assert reopened.used == 5
        assert reopened.stats.accepted == 2
        reopened.close()


class InterfaceTest(QueuelibTestCase):
    def test_push_rejects_non_task(self):
        q = SizedQueue(self.qpath, config())
        with pytest.raises(TypeError):
            q.push("not a task")
        q.close()

    def test_non_serializable_item_fails_without_mutating_state(self):
        q = SizedQueue(self.qpath, config())
        q.push(SizedTask(item="kept", size=4))
        with pytest.raises(TypeError, match="JSON-serializable"):
            q.push(SizedTask(item=object(), size=1))
        assert len(q) == 1
        assert q.used == 4
        assert q.peek().item == "kept"
        assert q.stats.submitted == 1
        q.close()
        reopened = SizedQueue(self.qpath)
        assert len(reopened) == 1
        assert reopened.peek().item == "kept"
        reopened.close()

    def test_capacity_never_exceeded_under_mixed_operations(self):
        capacity = 12
        q = SizedQueue(
            self.qpath,
            config(capacity=capacity, on_full=OverflowPolicy.EVICT),
        )
        for index in range(50):
            q.push(SizedTask(item=index, size=index % 7, priority=index % 3))
            assert q.used <= capacity
            if index % 5 == 0:
                q.pop()
                assert q.used <= capacity
        assert sum(t.size for t in q._items.values()) == q.used
        q.close()


@pytest.mark.parametrize("policy", [OverflowPolicy.REJECT, OverflowPolicy.EVICT])
def test_capacity_invariant_holds_after_every_push(policy, tmp_path):
    capacity = 8
    q = SizedQueue(tmp_path / "q", config(capacity=capacity, on_full=policy))
    for index in range(30):
        result = q.push(SizedTask(item=index, size=index % 6, priority=index % 4))
        assert q.used <= capacity
        if result.rejected is not None:
            assert not result.accepted
        if result.accepted:
            assert result.rejected is None
    stats = q.stats
    assert stats.submitted == stats.accepted + stats.rejected
    q.close()


@given(
    capacity=st.integers(1, 12),
    on_full=st.sampled_from([OverflowPolicy.REJECT, OverflowPolicy.EVICT]),
    ops=st.lists(
        st.tuples(
            st.sampled_from(["push", "pop", "clear", "reopen"]),
            st.integers(),
            st.integers(-2, 2),
            st.booleans(),
        ),
        max_size=60,
    ),
)
def test_sized_queue_invariants(capacity, on_full, ops):
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/q"
        cfg = SizedQueueConfig(
            capacity=capacity,
            on_full=on_full,
            eviction_priority=EvictionPriority.LOWEST_VALUE,
            eviction_order=EvictionOrder.FIFO,
        )
        q = SizedQueue(path, cfg)
        # Independent model: seq -> (item, size, priority, pinned), in order.
        held = {}
        counters = {"submitted": 0, "accepted": 0, "rejected": 0, "evicted": 0}
        item_counter = 0

        for op, raw_size, priority, pinned in ops:
            if op == "push":
                # Spans 0..capacity+2 so exact-fit and oversized tasks occur.
                size = raw_size % (capacity + 3)
                item = item_counter
                item_counter += 1
                task = SizedTask(item=item, size=size, priority=priority, pinned=pinned)
                result = q.push(task)
                counters["submitted"] += 1
                # Evictions must be accounted for even when admission ultimately fails.
                for evicted_task in result.evicted:
                    assert evicted_task.pinned is False
                    assert evicted_task.seq in held
                    del held[evicted_task.seq]
                    counters["evicted"] += 1
                if result.accepted:
                    counters["accepted"] += 1
                    assert size <= capacity
                    held[task.seq] = (item, size, priority, pinned)
                else:
                    counters["rejected"] += 1
                    assert result.rejected is task
                    assert task.seq not in held
                    if size > capacity:
                        assert result.reason == "oversized"
                        assert result.evicted == []
            elif op == "pop":
                popped = q.pop()
                if held:
                    head_seq = next(iter(held))
                    expected = held.pop(head_seq)
                    assert popped is not None
                    assert popped.item == expected[0]
                else:
                    assert popped is None
            elif op == "clear":
                q.clear()
                held.clear()
            else:  # reopen
                q.close()
                q = SizedQueue(path)
                assert q.config == cfg

            assert len(q) == len(held)
            assert q.used == sum(entry[1] for entry in held.values())
            assert q.used <= capacity
            assert [t.item for t in q._items.values()] == [
                entry[0] for entry in held.values()
            ]
            stats = q.stats
            assert stats.submitted == counters["submitted"]
            assert stats.accepted == counters["accepted"]
            assert stats.rejected == counters["rejected"]
            assert stats.evicted == counters["evicted"]
            assert stats.submitted == stats.accepted + stats.rejected

        q.close()
        reopened = SizedQueue(path)
        assert len(reopened) == len(held)
        assert reopened.used == sum(entry[1] for entry in held.values())
        reopened.close()
