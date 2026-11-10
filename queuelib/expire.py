"""Optional expiration support for queues.

A regular queue keeps every item until it is popped. Some items, such as
coupon tasks, stop being useful after a given point in time. This module adds
an optional, per-item *deadline* on top of any queue that follows the
:class:`~queuelib.queue.BaseQueue` interface.

The current time is never read from the system: the caller passes a zero
argument *clock* callable when building the queue, and every expiration
decision uses the value it returns (or an explicit value passed to the
method). This makes the rules deterministic and testable, and lets the same
code work with fake, monotonic or remote clocks.

Rules:

* An item is valid while ``deadline > now``. When the two are equal the item
  has *just* expired, so it is never handed to a consumer again.
* Time must not move backwards. A value lower than a previously seen one
  raises :class:`ExpiredItemError`.
* :meth:`ExpiringQueue.peek` never removes an item, expired or not.
* Deadlines are stored together with their items, so closing and reopening a
  disk queue does not reset or lose them.
"""

from __future__ import annotations

import struct
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from queuelib.queue import _clear

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from queuelib.queue import BaseQueue

# Prefix of the bytes stored for a deadline item. The 8 bytes that follow are
# the deadline packed as a big-endian signed 64-bit integer.
_MARK = b"QLDL\x01"
_MARK_SIZE = len(_MARK)
_DEADLINE_SIZE = struct.calcsize(">q")
_HEADER_SIZE = _MARK_SIZE + _DEADLINE_SIZE
_DEADLINE_MIN = -(2**63)
_DEADLINE_MAX = 2**63 - 1


class ExpiredItemError(RuntimeError):
    """Raised when expiration support is used inconsistently.

    This covers a clock that reports an earlier time than a previously seen
    one, and plain items (pushed without a deadline) found in a queue that is
    supposed to carry deadlines.
    """


#: The item's own deadline was reached (``now >= deadline``).
REASON_DEADLINE_REACHED = "deadline reached"
#: A ``pop()`` could not return anything because every remaining item had
#: expired; the items reclaimed together after the first one carry this reason.
REASON_ALL_EXPIRED = "all items expired"


@dataclass(frozen=True, slots=True)
class ExpiredItem:
    """An item removed from a queue because its deadline passed."""

    item: bytes
    deadline: int
    reason: str


def _pack(deadline: int, item: bytes) -> bytes:
    return _MARK + struct.pack(">q", deadline) + item


def _unpack(raw: bytes) -> tuple[int, bytes]:
    if len(raw) < _HEADER_SIZE or raw[:_MARK_SIZE] != _MARK:
        raise ExpiredItemError("queue item has no deadline")
    (deadline,) = struct.unpack(">q", raw[_MARK_SIZE:_HEADER_SIZE])
    return deadline, raw[_HEADER_SIZE:]


class DeadlineQueue:
    """Interface of a queue whose items carry a deadline."""

    @abstractmethod
    def push(self, obj: bytes, deadline: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def pop(self, now: int | None = None) -> bytes | None:
        raise NotImplementedError

    @abstractmethod
    def peek(self, now: int | None = None) -> bytes | None:
        raise NotImplementedError

    @abstractmethod
    def pop_expired(self, now: int | None = None) -> list[ExpiredItem]:
        raise NotImplementedError

    @abstractmethod
    def __len__(self) -> int:
        raise NotImplementedError


class ExpiringQueue(DeadlineQueue):
    """Add per-item deadlines to any :class:`~queuelib.queue.BaseQueue`.

    The wrapped *queue* stores the raw payloads; this wrapper pairs every item
    with an integer deadline and decides, using *clock*, whether an item is
    still valid. *clock* is a zero-argument callable returning the current
    time as an integer, and it is the only source of time the queue uses.
    Methods also accept an explicit *now* to override the clock, which is
    handy for tests and for batching decisions around one time reading.

    *on_expired* is an optional callback invoked once for every reclaimed
    :class:`ExpiredItem`. It is the safe way to observe reclaimed items when
    the wrapped queue may be closed and discarded on the caller's behalf, for
    example the per-bucket queues of a :class:`~queuelib.pqueue.PriorityQueue`
    or :class:`~queuelib.rrqueue.RoundRobinQueue`.

    Items are valid while their deadline is strictly greater than the current
    time. :meth:`pop` skips expired head items instead of returning them and
    moves them to an internal reclamation list; :meth:`pop_expired` returns
    that list (and also reclaims any expired head items on demand). A backing
    disk queue keeps the deadlines across a close/reopen cycle because they
    are written next to the items.
    """

    def __init__(
        self,
        queue: BaseQueue,
        clock: Callable[[], int],
        on_expired: Callable[[ExpiredItem], None] | None = None,
    ) -> None:
        self.q = queue
        self.clock = clock
        self.on_expired = on_expired
        self._last_now: int | None = None
        self._expired: list[ExpiredItem] = []

    def _record(self, expired: Iterable[ExpiredItem]) -> None:
        for entry in expired:
            self._expired.append(entry)
            if self.on_expired is not None:
                self.on_expired(entry)

    def _time(self, now: int | None) -> int:
        if now is None:
            now = self.clock()
        if not isinstance(now, int):
            raise TypeError(f"time must be an int, got {type(now).__name__}")
        if self._last_now is not None and now < self._last_now:
            raise ExpiredItemError(f"clock moved backwards: {now} < {self._last_now}")
        self._last_now = now
        return now

    def _head(self) -> tuple[int, bytes] | None:
        raw = self.q.peek()
        if raw is None:
            return None
        return _unpack(raw)

    def _drain_expired_heads(
        self, now: int
    ) -> tuple[list[tuple[int, bytes]], tuple[int, bytes] | None]:
        """Remove expired head items, stopping at the first valid one.

        Returns the removed ``(deadline, item)`` pairs together with the
        valid head that stopped the drain, or ``None`` if the queue is empty.
        """
        removed: list[tuple[int, bytes]] = []
        while True:
            head = self._head()
            if head is None:
                return removed, None
            deadline, _ = head
            if deadline > now:
                return removed, head
            self.q.pop()  # remove the expired item that _head() just read
            removed.append(head)

    @staticmethod
    def _make_expired(
        removed: list[tuple[int, bytes]], *, all_expired: bool
    ) -> list[ExpiredItem]:
        expired: list[ExpiredItem] = []
        for index, (deadline, item) in enumerate(removed):
            if index == 0 or not all_expired:
                reason = REASON_DEADLINE_REACHED
            else:
                reason = REASON_ALL_EXPIRED
            expired.append(ExpiredItem(item=item, deadline=deadline, reason=reason))
        return expired

    def push(self, obj: bytes, deadline: int) -> None:
        if not isinstance(obj, bytes):
            raise TypeError(f"Unsupported type: {type(obj).__name__}")
        if not isinstance(deadline, int):
            raise TypeError(f"deadline must be an int, got {type(deadline).__name__}")
        if not _DEADLINE_MIN <= deadline <= _DEADLINE_MAX:
            raise ValueError(
                f"deadline must be within a signed 64-bit integer, got {deadline}"
            )
        self.q.push(_pack(deadline, obj))

    def pop(self, now: int | None = None) -> bytes | None:
        now = self._time(now)
        removed, valid_head = self._drain_expired_heads(now)
        if valid_head is not None:
            _, item = valid_head
            self.q.pop()
            self._record(self._make_expired(removed, all_expired=False))
            return item
        self._record(self._make_expired(removed, all_expired=True))
        return None

    def peek(self, now: int | None = None) -> bytes | None:
        now = self._time(now)
        head = self._head()
        if head is None:
            return None
        deadline, item = head
        if deadline > now:
            return item
        return None

    def pop_expired(self, now: int | None = None) -> list[ExpiredItem]:
        """Reclaim expired head items and return all reclaimed items.

        Expired items are reclaimed lazily, as they reach the queue head:
        :meth:`pop` reclaims the ones it skips, and this method reclaims any
        expired head items on demand. Reclaimed items accumulate until this
        method returns and clears them, giving the caller a chance to log each
        item together with its deadline and :class:`ExpiredItem` reason. It
        never removes valid items and never changes their order.
        """
        now = self._time(now)
        removed, _ = self._drain_expired_heads(now)
        self._record(self._make_expired(removed, all_expired=False))
        reclaimed = self._expired
        self._expired = []
        return reclaimed

    def clear(self) -> None:
        _clear(self.q)
        self._expired.clear()

    def close(self) -> None:
        self.q.close()

    def __len__(self) -> int:
        return len(self.q)
