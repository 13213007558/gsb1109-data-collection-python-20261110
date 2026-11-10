from __future__ import annotations

import heapq
import pickle
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from queuelib.queue import _clear

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Iterable

    from queuelib.queue import BaseQueue


class PriorityQueue:
    """A priority queue implemented using multiple internal queues (typically,
    FIFO queues). The internal queue must implement the following methods:

        * push(obj)
        * pop()
        * peek()
        * close()
        * __len__()

    Internal queues may also implement clear(); those that do not are emptied
    by popping.

    The constructor receives a qfactory argument, which is a callable used to
    instantiate a new (internal) queue when a new priority is allocated. The
    qfactory function is called with the priority number as first and only
    argument.

    Only integer priorities should be used. Lower numbers are higher
    priorities.

    startprios is a sequence of priorities to start with. If the queue was
    previously closed leaving some priority buckets non-empty, those priorities
    should be passed in startprios.

    """

    def __init__(
        self, qfactory: Callable[[int], BaseQueue], startprios: Iterable[int] = ()
    ) -> None:
        self.queues = {}
        self.qfactory = qfactory
        for p in startprios:
            self.queues[p] = self.qfactory(p)
        self.curprio = min(self.queues) if self.queues else None

    def push(self, obj: Any, priority: int = 0) -> None:
        if priority not in self.queues:
            self.queues[priority] = self.qfactory(priority)
        q = self.queues[priority]
        q.push(obj)  # this may fail (eg. serialization error)
        if self.curprio is None or priority < self.curprio:
            self.curprio = priority

    def pop(self) -> Any | None:
        if self.curprio is None:
            return None
        q = self.queues[self.curprio]
        m = q.pop()
        if len(q) == 0:
            del self.queues[self.curprio]
            q.close()
            prios = [p for p, q in self.queues.items() if len(q) > 0]
            self.curprio = min(prios) if prios else None
        return m

    def peek(self) -> Any | None:
        if self.curprio is None:
            return None
        return self.queues[self.curprio].peek()

    def clear(self) -> None:
        for q in self.queues.values():
            _clear(q)
            q.close()
        self.queues.clear()
        self.curprio = None

    def close(self) -> list[int]:
        active = []
        for p, q in self.queues.items():
            if len(q):
                active.append(p)
            q.close()
        return active

    def __len__(self) -> int:
        return sum(len(x) for x in self.queues.values()) if self.queues else 0


class TaskStatus(Enum):
    """Lifecycle states of a task held by a HandlePriorityQueue."""

    PENDING = "pending"
    CANCELLED = "cancelled"
    DEQUEUED = "dequeued"


class InvalidHandleError(Exception):
    """Raised when a task handle is unknown to the queue or no longer refers
    to a pending task."""


@dataclass(frozen=True)
class TaskHandle:
    """Opaque identifier of a queued task, returned by
    HandlePriorityQueue.push().

    A handle is a logical token, not a storage location: it stays valid when
    the task changes priority and across restarts of a persistent queue, and
    it can be pickled so that callers may store it themselves. Handles are
    never recycled; a handle whose task left the queue cannot affect any task
    enqueued later.
    """

    task_id: int
    generation: int


@dataclass
class _TaskRecord:
    generation: int
    priority: int
    seq: int
    status: TaskStatus
    payload: Any


class HandlePriorityQueue:
    """A priority queue whose tasks can be tracked and managed individually.

    push() returns a TaskHandle for the enqueued task. The handle can later be
    used to move the task to a different priority (adjust()), to remove it
    (cancel()) or to query its state (status()), without having to pop and
    re-push every other task.

    Tasks with the same priority are dequeued in the order they were first
    enqueued. Adjusting the priority of a task moves it behind the tasks
    already waiting at the target priority; adjusting it to its current
    priority leaves its position untouched. Ordering is fully deterministic:
    repeating the same sequence of operations always yields the same
    dequeue order, no matter how many times tasks were re-prioritized.

    Handles of tasks that were cancelled, dequeued or cleared become stale:
    using them raises InvalidHandleError and never affects other tasks,
    including tasks enqueued afterwards.

    If a path is passed, close() writes the queue state (including payloads)
    to it, and passing the same path to a new instance resumes the queue where
    it was left, with all handles still valid. Payloads must be picklable when
    a path is used; without a path, payloads of any type are accepted and
    nothing is persisted.
    """

    STATE_VERSION = 1

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._tasks: dict[int, _TaskRecord] = {}
        self._heap: list[tuple[int, int, int]] = []
        self._next_task_id = 1
        self._next_seq = 0
        self._pending = 0
        if (
            self._path is not None
            and self._path.exists()
            and self._path.stat().st_size > 0
        ):
            self._load()

    def push(self, obj: Any, priority: int = 0) -> TaskHandle:
        """Enqueue obj at the given priority and return its task handle."""
        task_id = self._next_task_id
        self._next_task_id += 1
        handle = TaskHandle(task_id, 0)
        record = _TaskRecord(
            generation=handle.generation,
            priority=priority,
            seq=self._next_seq,
            status=TaskStatus.PENDING,
            payload=obj,
        )
        self._next_seq += 1
        self._tasks[task_id] = record
        self._pending += 1
        heapq.heappush(self._heap, (record.priority, record.seq, task_id))
        return handle

    def pop(self) -> Any | None:
        """Dequeue the next task and return its payload, or None if empty."""
        self._discard_stale_head()
        if not self._heap:
            return None
        _, _, task_id = heapq.heappop(self._heap)
        record = self._tasks[task_id]
        record.status = TaskStatus.DEQUEUED
        self._pending -= 1
        payload = record.payload
        record.payload = None
        return payload

    def peek(self) -> Any | None:
        """Return the payload of the next task to dequeue, or None if empty."""
        self._discard_stale_head()
        if not self._heap:
            return None
        return self._tasks[self._heap[0][2]].payload

    def adjust(self, handle: TaskHandle, priority: int) -> None:
        """Move the task referred to by handle to the given priority.

        The task is placed behind the tasks already waiting at that priority.
        Adjusting to the current priority is a no-op. Raises
        InvalidHandleError if the handle is unknown or stale.
        """
        record = self._resolve_pending(handle)
        self._apply_adjust(handle.task_id, record, priority)

    def adjust_many(self, handles: Iterable[TaskHandle], priority: int) -> None:
        """Move every task referred to by handles to the given priority.

        All-or-nothing policy: every handle is validated before any change is
        made, so if any handle is unknown or stale, InvalidHandleError is
        raised and no task is modified.
        """
        entries = [
            (handle.task_id, self._resolve_pending(handle)) for handle in handles
        ]
        for task_id, record in entries:
            self._apply_adjust(task_id, record, priority)

    def cancel(self, handle: TaskHandle) -> None:
        """Remove the task referred to by handle from the queue.

        Raises InvalidHandleError if the handle is unknown or stale.
        """
        record = self._resolve_pending(handle)
        record.status = TaskStatus.CANCELLED
        record.payload = None
        self._pending -= 1

    def status(self, handle: TaskHandle) -> TaskStatus:
        """Return the TaskStatus of the task referred to by handle.

        Handles of cancelled or dequeued tasks remain queryable. Raises
        InvalidHandleError if the handle is unknown to this queue.
        """
        return self._resolve(handle).status

    def clear(self) -> None:
        """Cancel every pending task, leaving the queue empty."""
        for record in self._tasks.values():
            if record.status is TaskStatus.PENDING:
                record.status = TaskStatus.CANCELLED
                record.payload = None
        self._heap.clear()
        self._pending = 0

    def close(self) -> None:
        """Persist the queue state if a path was given on instantiation."""
        if self._path is None:
            return
        state = {
            "version": self.STATE_VERSION,
            "next_task_id": self._next_task_id,
            "next_seq": self._next_seq,
            "tasks": {
                task_id: {
                    "generation": record.generation,
                    "priority": record.priority,
                    "seq": record.seq,
                    "status": record.status.value,
                    "payload": record.payload,
                }
                for task_id, record in self._tasks.items()
            },
        }
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        with tmp_path.open("wb") as tmp_file:
            pickle.dump(state, tmp_file)
        tmp_path.replace(self._path)

    def _load(self) -> None:
        assert self._path is not None
        with self._path.open("rb") as state_file:
            state = pickle.load(state_file)  # noqa: S301
        if state.get("version") != self.STATE_VERSION:
            raise ValueError(f"Unsupported state version: {state.get('version')!r}")
        self._next_task_id = state["next_task_id"]
        self._next_seq = state["next_seq"]
        for task_id, data in state["tasks"].items():
            record = _TaskRecord(
                generation=data["generation"],
                priority=data["priority"],
                seq=data["seq"],
                status=TaskStatus(data["status"]),
                payload=data["payload"],
            )
            self._tasks[task_id] = record
            if record.status is TaskStatus.PENDING:
                self._pending += 1
                heapq.heappush(self._heap, (record.priority, record.seq, task_id))

    def _resolve(self, handle: TaskHandle) -> _TaskRecord:
        if not isinstance(handle, TaskHandle):
            raise InvalidHandleError(f"Not a task handle: {handle!r}")
        record = self._tasks.get(handle.task_id)
        if record is None or record.generation != handle.generation:
            raise InvalidHandleError(f"Unknown task handle: {handle!r}")
        return record

    def _resolve_pending(self, handle: TaskHandle) -> _TaskRecord:
        record = self._resolve(handle)
        if record.status is not TaskStatus.PENDING:
            raise InvalidHandleError(
                f"Task handle is no longer pending ({record.status.value}): {handle!r}"
            )
        return record

    def _apply_adjust(self, task_id: int, record: _TaskRecord, priority: int) -> None:
        if priority == record.priority:
            return
        record.priority = priority
        record.seq = self._next_seq
        self._next_seq += 1
        heapq.heappush(self._heap, (record.priority, record.seq, task_id))

    def _discard_stale_head(self) -> None:
        while self._heap:
            _, seq, task_id = self._heap[0]
            record = self._tasks.get(task_id)
            if (
                record is not None
                and record.status is TaskStatus.PENDING
                and record.seq == seq
            ):
                return
            heapq.heappop(self._heap)

    def __len__(self) -> int:
        return self._pending
