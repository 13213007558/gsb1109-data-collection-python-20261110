from __future__ import annotations

import sqlite3
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from queuelib.queue import _clear

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Iterable

    from queuelib.queue import BaseQueue

# Failures that the built-in queues can raise while writing an item:
# OSError for file-based queues, TypeError for non-bytes payloads, and
# sqlite3.Error (e.g. OperationalError) for the SQLite queues.
_WRITE_ERRORS = (OSError, TypeError, sqlite3.Error)


class MergingQueue:
    """A keyed FIFO queue that keeps at most one pending entry per key.

    The queue wraps one internal queue (built by ``qfactory``) per key, exactly
    like :class:`~queuelib.rrqueue.RoundRobinQueue`, but it never lets a key hold
    more than one item at a time. Submitting an item for a key that already has
    a pending entry replaces that entry in place, so the entry keeps the queue
    position of its first submission. A key that is updated frequently is hence
    never pushed towards the tail: repeated updates to a hot key leave the queue
    order untouched.

    ``push(obj, key)`` replaces the pending payload of *key* with *obj*.
    ``push(obj, key, merge=func)`` instead calls ``func(old_payload, obj)`` and
    stores its return value. The merge function runs before the queue is
    modified, so if it raises, the previous payload, the queue position and the
    single pending entry for the key are all preserved. When *key* has no
    pending entry, *obj* is stored as a new task at the tail and *merge* is not
    called.

    Once an entry has been popped, its key is no longer pending: updates can no
    longer affect it, and the next submission for that key enters the queue as
    a brand-new task, at the tail.

    The internal queues may be in-memory or persistent, so the queue works with
    any :class:`~queuelib.queue.BaseQueue` implementation. Persistence works as
    for :class:`~queuelib.pqueue.PriorityQueue` and
    :class:`~queuelib.rrqueue.RoundRobinQueue`: :meth:`close` returns the keys
    whose queue is not empty, in queue order, and those keys must be passed back
    as *start_keys* when the queue is reopened.
    """

    def __init__(
        self,
        qfactory: Callable[[Hashable], BaseQueue],
        start_keys: Iterable[Hashable] = (),
    ) -> None:
        self.qfactory = qfactory
        self.queues: dict[Hashable, BaseQueue] = {}
        for key in start_keys:
            q = self.qfactory(key)
            size = len(q)
            if size > 1:
                q.close()
                msg = (
                    f"Cannot resume key {key!r}: its queue holds {size} items, "
                    "but a MergingQueue keeps at most one pending item per key"
                )
                raise ValueError(msg)
            if size == 1:
                self.queues[key] = q
            else:
                q.close()
        self.key_queue: deque[Hashable] = deque(self.queues)
        self.pending: set[Hashable] = set(self.queues)

    def push(
        self,
        obj: Any,
        key: Hashable,
        merge: Callable[[Any, Any], Any] | None = None,
    ) -> None:
        if key not in self.pending:
            q = self.qfactory(key)
            try:
                q.push(obj)
            except _WRITE_ERRORS:
                q.close()
                raise
            self.queues[key] = q
            self.key_queue.append(key)
            self.pending.add(key)
            return
        q = self.queues[key]
        old = q.peek()
        payload = obj if merge is None else merge(old, obj)
        q.pop()
        try:
            q.push(payload)
        except _WRITE_ERRORS:
            # A failed write must not leave the key with an empty queue: put
            # the previous payload back so the single pending entry survives.
            with suppress(*_WRITE_ERRORS):
                q.push(old)
            raise

    def pop(self) -> Any | None:
        if not self.key_queue:
            return None
        key = self.key_queue.popleft()
        q = self.queues.pop(key)
        self.pending.discard(key)
        item = q.pop()
        q.close()
        return item

    def peek(self) -> Any | None:
        if not self.key_queue:
            return None
        return self.queues[self.key_queue[0]].peek()

    def clear(self) -> None:
        for q in self.queues.values():
            _clear(q)
            q.close()
        self.queues.clear()
        self.key_queue.clear()
        self.pending.clear()

    def close(self) -> list[Hashable]:
        active = []
        for key, q in self.queues.items():
            if len(q):
                active.append(key)
            q.close()
        return active

    def __len__(self) -> int:
        return len(self.queues)
