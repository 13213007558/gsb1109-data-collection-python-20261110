from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any

from queuelib.expire import DeadlineQueue
from queuelib.queue import _clear

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Iterable

    from queuelib.queue import BaseQueue


class RoundRobinQueue:
    """A round robin queue implemented using multiple internal queues (typically,
    FIFO queues). The internal queue must implement the following methods:
        * push(obj)
        * pop()
        * peek()
        * close()
        * __len__()

    Internal queues may also implement clear(); those that do not are emptied
    by popping.

    The constructor receives a qfactory argument, which is a callable used to
    instantiate a new (internal) queue when a new key is allocated. The
    qfactory function is called with the key number as first and only argument.
    start_domains is a sequence of domains to initialize the queue with. If the
    queue was previously closed leaving some domain buckets non-empty, those
    domains should be passed in start_domains.

    The queue maintains a fifo queue of keys. The key that went last is popped
    first and the next queue for that key is then popped.

    The internal queues may be :class:`~queuelib.expire.DeadlineQueue`
    instances, in which case ``push()`` forwards an optional *deadline* to
    them and ``pop()``/``peek()`` pass along the current time.
    """

    def __init__(
        self,
        qfactory: Callable[[Hashable], BaseQueue],
        start_domains: Iterable[Hashable] = (),
    ) -> None:
        self.queues = {}
        self.qfactory = qfactory
        for key in start_domains:
            self.queues[key] = self.qfactory(key)
        self.key_queue = deque(self.queues)

    def push(self, obj: Any, key: Hashable, deadline: int | None = None) -> None:
        if key not in self.key_queue:
            self.queues[key] = self.qfactory(key)
            self.key_queue.appendleft(key)  # it's new, might as well pop first
        q = self.queues[key]
        if deadline is not None:
            if not isinstance(q, DeadlineQueue):
                raise TypeError(
                    "deadline is set but the internal queue has no expiration support"
                )
            q.push(obj, deadline)  # this may fail (eg. serialization error)
        else:
            q.push(obj)  # this may fail (eg. serialization error)

    def peek(self, now: int | None = None) -> Any | None:
        for key in reversed(self.key_queue):
            q = self.queues[key]
            item = q.peek(now) if isinstance(q, DeadlineQueue) else q.peek()
            if item is not None:
                return item
            # A deadline bucket can be non-empty yet blocked by an expired
            # head: report None instead of jumping to another key, so peek
            # never advertises an item that pop() would not return next.
            if isinstance(q, DeadlineQueue) and len(q) > 0:
                return None
        return None

    def clear(self) -> None:
        for q in self.queues.values():
            _clear(q)
            q.close()
        self.queues.clear()
        self.key_queue.clear()

    def pop(self, now: int | None = None) -> Any | None:
        # pop until we find a valid object, closing necessary queues
        while True:
            try:
                key = self.key_queue.pop()
            except IndexError:
                return None

            q = self.queues[key]
            m = q.pop(now) if isinstance(q, DeadlineQueue) else q.pop()

            if len(q) == 0:
                del self.queues[key]
                q.close()
            else:
                self.key_queue.appendleft(key)

            if m is not None:
                return m

    def close(self) -> list[Hashable]:
        active = []
        for k, q in self.queues.items():
            if len(q):
                active.append(k)
            q.close()
        return active

    def __len__(self) -> int:
        return sum(len(x) for x in self.queues.values()) if self.queues else 0
