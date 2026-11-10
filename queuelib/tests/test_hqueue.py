import pickle

import pytest

from queuelib.pqueue import (
    HandlePriorityQueue,
    InvalidHandleError,
    TaskHandle,
    TaskStatus,
)
from queuelib.tests import QueuelibTestCase


def drain(queue):
    items = []
    while True:
        item = queue.pop()
        if item is None:
            return items
        items.append(item)


class HandlePriorityQueueTest(QueuelibTestCase):
    def setUp(self):
        super().setUp()
        self.q = HandlePriorityQueue()

    def test_push_pop_priorities(self):
        self.q.push("a", 3)
        self.q.push("b", 1)
        self.q.push("c", 2)
        self.q.push("d", 1)
        assert drain(self.q) == ["b", "d", "c", "a"]
        assert self.q.pop() is None

    def test_fifo_within_same_priority(self):
        for letter in "abcde":
            self.q.push(letter, 1)
        assert drain(self.q) == list("abcde")

    def test_push_returns_handle(self):
        handle = self.q.push("a")
        assert isinstance(handle, TaskHandle)
        assert self.q.status(handle) is TaskStatus.PENDING
        assert len(self.q) == 1

    def test_handle_is_opaque_and_picklable(self):
        handle = self.q.push("a", 1)
        assert not hasattr(handle, "path")
        assert pickle.loads(pickle.dumps(handle)) == handle  # noqa: S301
        assert len({handle, handle}) == 1

    def test_accepts_unpicklable_payloads_without_path(self):
        payload = lambda x: x  # noqa: E731
        self.q.push(payload)
        assert self.q.pop() is payload

    def test_peek(self):
        assert self.q.peek() is None
        self.q.push("a", 2)
        self.q.push("b", 1)
        assert self.q.peek() == "b"
        assert len(self.q) == 2
        assert self.q.pop() == "b"
        assert self.q.peek() == "a"

    def test_adjust_moves_task_to_front(self):
        self.q.push("a", 5)
        handle_b = self.q.push("b", 5)
        self.q.push("c", 5)
        self.q.adjust(handle_b, 0)
        assert drain(self.q) == ["b", "a", "c"]

    def test_adjust_goes_to_back_of_target_priority(self):
        handle_a = self.q.push("a", 0)
        self.q.push("b", 5)
        self.q.push("c", 5)
        self.q.adjust(handle_a, 5)
        assert drain(self.q) == ["b", "c", "a"]

    def test_adjust_to_same_priority_keeps_position(self):
        handle_a = self.q.push("a", 0)
        self.q.push("b", 0)
        self.q.adjust(handle_a, 0)
        assert drain(self.q) == ["a", "b"]

    def test_repeated_adjusts_stay_deterministic(self):
        def run():
            queue = HandlePriorityQueue()
            handles = {letter: queue.push(letter, 3) for letter in "abcd"}
            queue.adjust(handles["a"], 1)
            queue.adjust(handles["b"], 1)
            queue.adjust(handles["a"], 3)
            queue.adjust(handles["c"], 1)
            queue.adjust(handles["a"], 1)
            queue.adjust(handles["d"], 0)
            queue.adjust(handles["d"], 4)
            return drain(queue)

        first = run()
        assert first == run()
        assert first == ["b", "c", "a", "d"]

    def test_cancel(self):
        handle_a = self.q.push("a", 1)
        self.q.push("b", 1)
        self.q.cancel(handle_a)
        assert self.q.status(handle_a) is TaskStatus.CANCELLED
        assert len(self.q) == 1
        assert drain(self.q) == ["b"]

    def test_status_after_pop(self):
        handle = self.q.push("a")
        assert self.q.pop() == "a"
        assert self.q.status(handle) is TaskStatus.DEQUEUED

    def test_stale_handle_does_not_affect_new_tasks(self):
        handle_a = self.q.push("a", 1)
        self.q.cancel(handle_a)
        handle_b = self.q.push("b", 1)
        with pytest.raises(InvalidHandleError):
            self.q.cancel(handle_a)
        with pytest.raises(InvalidHandleError):
            self.q.adjust(handle_a, 0)
        assert self.q.status(handle_b) is TaskStatus.PENDING
        assert drain(self.q) == ["b"]

    def test_dequeued_handle_is_stale(self):
        handle_a = self.q.push("a")
        self.q.pop()
        self.q.push("b")
        with pytest.raises(InvalidHandleError):
            self.q.adjust(handle_a, 0)
        with pytest.raises(InvalidHandleError):
            self.q.cancel(handle_a)
        assert drain(self.q) == ["b"]

    def test_unknown_handle_rejected(self):
        forged = TaskHandle(task_id=999, generation=0)
        with pytest.raises(InvalidHandleError):
            self.q.status(forged)
        with pytest.raises(InvalidHandleError):
            self.q.adjust(forged, 0)
        with pytest.raises(InvalidHandleError):
            self.q.cancel(forged)
        handle = self.q.push("a")
        wrong_generation = TaskHandle(handle.task_id, handle.generation + 1)
        with pytest.raises(InvalidHandleError):
            self.q.status(wrong_generation)
        with pytest.raises(InvalidHandleError):
            self.q.status("not-a-handle")

    def test_adjust_many(self):
        handles = {letter: self.q.push(letter, 5) for letter in "abc"}
        self.q.push("d", 5)
        self.q.adjust_many([handles["b"], handles["c"]], 0)
        assert drain(self.q) == ["b", "c", "a", "d"]

    def test_adjust_many_is_all_or_nothing(self):
        handle_a = self.q.push("a", 5)
        handle_b = self.q.push("b", 5)
        self.q.cancel(handle_b)
        handle_c = self.q.push("c", 5)
        with pytest.raises(InvalidHandleError):
            self.q.adjust_many([handle_a, handle_b, handle_c], 0)
        assert self.q.status(handle_a) is TaskStatus.PENDING
        assert self.q.status(handle_c) is TaskStatus.PENDING
        assert drain(self.q) == ["a", "c"]

    def test_adjust_many_unknown_handle(self):
        handle_a = self.q.push("a", 5)
        with pytest.raises(InvalidHandleError):
            self.q.adjust_many([handle_a, TaskHandle(42, 0)], 0)
        assert drain(self.q) == ["a"]

    def test_clear(self):
        handles = [self.q.push(letter, 1) for letter in "ab"]
        self.q.clear()
        assert len(self.q) == 0
        assert self.q.peek() is None
        assert self.q.pop() is None
        for handle in handles:
            assert self.q.status(handle) is TaskStatus.CANCELLED
        self.q.push("c", 1)
        assert self.q.pop() == "c"

    def test_len(self):
        assert len(self.q) == 0
        assert not self.q
        handle_a = self.q.push("a")
        self.q.push("b")
        assert len(self.q) == 2
        assert self.q
        self.q.cancel(handle_a)
        assert len(self.q) == 1
        self.q.pop()
        assert len(self.q) == 0


class HandlePriorityQueuePersistenceTest(QueuelibTestCase):
    def setUp(self):
        super().setUp()
        self.state_path = self.qpath

    def operations_part1(self, queue):
        handles = {letter: queue.push(letter, 3) for letter in "abcde"}
        queue.adjust(handles["b"], 0)
        queue.cancel(handles["d"])
        queue.adjust(handles["e"], 1)
        return handles

    def operations_part2(self, queue, handles):
        queue.adjust(handles["a"], 0)
        queue.push("f", 0)
        queue.adjust(handles["e"], 3)
        queue.cancel(handles["c"])

    def test_close_and_reopen(self):
        queue = HandlePriorityQueue(self.state_path)
        handles = self.operations_part1(queue)
        queue.close()
        reopened = HandlePriorityQueue(self.state_path)
        self.operations_part2(reopened, handles)
        assert drain(reopened) == ["b", "a", "f", "e"]

    def test_restart_keeps_operation_sequence_deterministic(self):
        uninterrupted = HandlePriorityQueue()
        handles = self.operations_part1(uninterrupted)
        self.operations_part2(uninterrupted, handles)
        expected = drain(uninterrupted)

        restarted = HandlePriorityQueue(self.state_path)
        handles = self.operations_part1(restarted)
        restarted.close()
        restarted = HandlePriorityQueue(self.state_path)
        self.operations_part2(restarted, handles)
        assert drain(restarted) == expected

    def test_handles_survive_restart(self):
        queue = HandlePriorityQueue(self.state_path)
        handle_a = queue.push("a", 5)
        handle_b = queue.push("b", 5)
        queue.close()
        reopened = HandlePriorityQueue(self.state_path)
        assert reopened.status(handle_a) is TaskStatus.PENDING
        reopened.adjust(handle_a, 0)
        reopened.cancel(handle_b)
        assert drain(reopened) == ["a"]

    def test_stale_handles_stay_invalid_after_restart(self):
        queue = HandlePriorityQueue(self.state_path)
        handle_a = queue.push("a")
        queue.cancel(handle_a)
        queue.close()
        reopened = HandlePriorityQueue(self.state_path)
        handle_b = reopened.push("b")
        with pytest.raises(InvalidHandleError):
            reopened.cancel(handle_a)
        with pytest.raises(InvalidHandleError):
            reopened.adjust(handle_a, 0)
        assert reopened.status(handle_a) is TaskStatus.CANCELLED
        assert reopened.status(handle_b) is TaskStatus.PENDING
        assert drain(reopened) == ["b"]

    def test_task_ids_not_reused_after_restart(self):
        queue = HandlePriorityQueue(self.state_path)
        first = queue.push("a")
        queue.cancel(first)
        queue.close()
        reopened = HandlePriorityQueue(self.state_path)
        second = reopened.push("b")
        assert second.task_id != first.task_id

    def test_counters_do_not_rewind_after_restart(self):
        queue = HandlePriorityQueue(self.state_path)
        handle_a = queue.push("a", 1)
        queue.adjust(handle_a, 2)
        queue.close()
        reopened = HandlePriorityQueue(self.state_path)
        handle_b = reopened.push("b", 2)
        reopened.adjust(handle_b, 1)
        assert drain(reopened) == ["b", "a"]

    def test_close_without_path_is_noop(self):
        queue = HandlePriorityQueue()
        queue.push("a")
        queue.close()
        assert queue.pop() == "a"
