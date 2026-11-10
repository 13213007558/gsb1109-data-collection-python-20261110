from __future__ import annotations

from typing import TYPE_CHECKING, Any

from queuelib.expire import DeadlineQueue
from queuelib.queue import _clear

if TYPE_CHECKING:
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

    The internal queues may be :class:`~queuelib.expire.DeadlineQueue`
    instances, in which case ``push()`` forwards an optional *deadline* to
    them and ``pop()``/``peek()`` pass along the current time.

    """

    def __init__(
        self, qfactory: Callable[[int], BaseQueue], startprios: Iterable[int] = ()
    ) -> None:
        self.queues = {}
        self.qfactory = qfactory
        for p in startprios:
            self.queues[p] = self.qfactory(p)
        self.curprio = min(self.queues) if self.queues else None

    def push(self, obj: Any, priority: int = 0, deadline: int | None = None) -> None:
        if priority not in self.queues:
            self.queues[priority] = self.qfactory(priority)
        q = self.queues[priority]
        if deadline is not None:
            if not isinstance(q, DeadlineQueue):
                raise TypeError(
                    "deadline is set but the internal queue has no expiration support"
                )
            q.push(obj, deadline)  # this may fail (eg. serialization error)
        else:
            q.push(obj)  # this may fail (eg. serialization error)
        if self.curprio is None or priority < self.curprio:
            self.curprio = priority

    def pop(self, now: int | None = None) -> Any | None:
        # A deadline bucket can hold only expired items: its pop() reclaims
        # them all, becomes empty and returns None. Keep going through the
        # lower-priority buckets instead of reporting a false empty queue.
        while self.curprio is not None:
            q = self.queues[self.curprio]
            m = q.pop(now) if isinstance(q, DeadlineQueue) else q.pop()
            if len(q) == 0:
                del self.queues[self.curprio]
                q.close()
                prios = [p for p, q in self.queues.items() if len(q) > 0]
                self.curprio = min(prios) if prios else None
            if m is not None:
                return m
        return None

    def peek(self, now: int | None = None) -> Any | None:
        if self.curprio is None:
            return None
        q = self.queues[self.curprio]
        # Like the underlying queue, an expired bucket head makes peek()
        # return None without evicting anything; pop() is what drains it.
        return q.peek(now) if isinstance(q, DeadlineQueue) else q.peek()

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
