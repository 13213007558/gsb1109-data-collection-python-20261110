from __future__ import annotations

import heapq
import math
from collections import deque
from itertools import count
from typing import TYPE_CHECKING, Any

from queuelib.queue import _clear

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Iterable

    from queuelib.queue import BaseQueue

# Floating point tolerance for comparing accumulated quota against item cost.
_EPS = 1e-12


class WeightedRoundRobinQueue:
    """A weighted round robin queue of multiple internal queues (typically,
    FIFO queues), one per source key. Unlike :class:`RoundRobinQueue`, which
    serves sources in turn regardless of how expensive their items are, this
    queue shares service according to a per-source *weight* and the *cost*
    that the caller attaches to every item.

    The internal queue must implement the following methods:
        * push(obj)
        * pop()
        * peek()
        * close()
        * __len__()

    Internal queues may also implement clear(); those that do not are emptied
    by popping. They must be FIFO queues: the cost of each item is kept in the
    same head-to-tail order, so the cost recorded first has to match the item
    that pops first.

    The constructor receives a qfactory argument, which is a callable used to
    instantiate a new (internal) queue when a new key is allocated. The
    qfactory function is called with the key as first and only argument.
    *weights* maps source keys to their finite, positive numeric weight; a
    key pushed without an explicit weight gets weight 1. start_domains is a
    sequence of keys to initialize the queue with. If the queue was previously
    closed leaving some source buckets non-empty, those keys should be passed
    in start_domains.

    Every pushed item must be paired with a finite, positive numeric *cost*
    (``push(obj, key, cost)``). Invalid costs and weights raise ``ValueError``
    and leave the queue untouched. Costs and weights may use different units:
    only their relative sizes matter.

    Scheduling follows deficit round robin. The active sources are kept in a
    rotation and each source is visited once per round. On a visit a source is
    granted a quota, added to its deficit, and then serves its head items
    while their cost fits the deficit before the rotation moves on. Quotas
    stay proportional to the weights within every round, but they share a
    scale factor of the largest pending item cost divided by the smallest
    active weight, so that even the smallest quota covers any pending item.
    Consequently no source can ever be blocked by an expensive head item, and
    the fairness guarantees hold when an item is more expensive than its
    source's raw weight.

    The rules for the special situations are:

    * A source that runs dry is removed from the rotation together with its
      deficit. It keeps neither credit nor debt. When it gets items again it
      rejoins at the back of the rotation, as a new source with a zero
      deficit, and is granted its first quota on its next visit.
    * A weight change (``set_weight``) takes effect at the source's next
      visit: the deficit accumulated so far is kept, while every later quota
      grant uses the new weight, including the round scale.
    * A single oversized item, however expensive, is served on its source's
      next visit, because the scaled quota always covers the largest pending
      item. It is charged at its true cost against the source's deficit, so
      the source cannot spend more than its weight allows in a round and
      yields the rotation immediately afterwards; the item therefore earns
      its source no extra share.
    * Every active source is visited once per round, including sources with a
      very small weight, so long-lived low-weight sources keep making
      progress. While all sources stay backlogged, the cumulative served cost
      of each source converges to its share of the weights. The discrepancy
      against that ideal share stays bounded by a constant that depends only
      on the weights and the largest pending item cost, not on the number of
      items served.
    """

    def __init__(
        self,
        qfactory: Callable[[Hashable], BaseQueue],
        weights: dict[Hashable, float] | None = None,
        start_domains: Iterable[Hashable] = (),
    ) -> None:
        self.qfactory = qfactory
        self.queues: dict[Hashable, BaseQueue] = {}
        self.weights: dict[Hashable, float] = {}
        # Per-source (cost, unique id) of pending items, head-to-tail.
        self.pending_costs: dict[Hashable, deque[tuple[float, int]]] = {}
        # Per-source accumulated quota minus served cost; never negative.
        self.deficit: dict[Hashable, float] = {}
        # Sources already granted their quota in the current rotation round.
        self.visited: set[Hashable] = set()
        # Max-heap of pending item costs as (-cost, id), with lazy deletion.
        self.cost_heap: list[tuple[float, int]] = []
        self.live_ids: set[int] = set()
        self._ids = count()
        # Sources with items left, in rotation order.
        self.key_queue: deque[Hashable] = deque()
        weights = {} if weights is None else dict(weights)
        for key, weight in weights.items():
            self._set_weight(key, weight)
        for key in start_domains:
            if key not in self.weights:
                self._set_weight(key, 1.0)
            self.queues[key] = self.qfactory(key)
            self.pending_costs[key] = deque()
            self.deficit[key] = 0.0
            self.key_queue.append(key)

    @staticmethod
    def _check_positive_number(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite positive number, got {value!r}")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} must be a finite positive number, got {value!r}")
        return number

    def _set_weight(self, key: Hashable, weight: Any) -> float:
        checked = self._check_positive_number(weight, "weight")
        self.weights[key] = checked
        return checked

    def set_weight(self, key: Hashable, weight: float) -> None:
        """Set the weight of source *key*.

        The new weight is used from the source's next quota grant on: the
        deficit accumulated so far is left unchanged. Sources that are
        currently empty use the new weight when they rejoin.
        """
        self._set_weight(key, weight)

    def push(self, obj: Any, key: Hashable, cost: float) -> None:
        checked_cost = self._check_positive_number(cost, "cost")
        if key not in self.queues:
            self._set_weight(key, self.weights.get(key, 1.0))
            self.queues[key] = self.qfactory(key)
            self.pending_costs[key] = deque()
            # A rejoining source starts with no credit and no debt, and gets
            # granted its quota on its next visit.
            self.deficit[key] = 0.0
            self.key_queue.append(key)
        item_id = next(self._ids)
        q = self.queues[key]
        q.push(obj)  # this may fail (eg. serialization error)
        self.pending_costs[key].append((checked_cost, item_id))
        self.live_ids.add(item_id)
        heapq.heappush(self.cost_heap, (-checked_cost, item_id))

    def _max_pending_cost(self) -> float:
        while self.cost_heap:
            neg_cost, item_id = self.cost_heap[0]
            if item_id in self.live_ids:
                return -neg_cost
            heapq.heappop(self.cost_heap)
        return 1.0

    def _quota(self, key: Hashable) -> float:
        max_cost = self._max_pending_cost()
        min_weight = min(self.weights[k] for k in self.key_queue)
        return self.weights[key] * max_cost / min_weight

    def _fits(self, key: Hashable) -> bool:
        cost = self.pending_costs[key][0][0]
        tolerance = _EPS * max(1.0, abs(cost), abs(self.deficit[key]))
        return self.deficit[key] + tolerance >= cost

    def _remove(self, key: Hashable) -> None:
        q = self.queues.pop(key)
        for _, item_id in self.pending_costs.pop(key):
            self.live_ids.discard(item_id)
        self.deficit.pop(key)
        self.visited.discard(key)
        self.key_queue.popleft()
        q.close()

    def _serve(self, key: Hashable) -> Any:
        q = self.queues[key]
        item = q.pop()
        cost, item_id = self.pending_costs[key].popleft()
        self.live_ids.discard(item_id)
        self.deficit[key] -= cost
        if len(q) == 0:
            self._remove(key)
        return item

    def pop(self) -> Any | None:
        if not self.key_queue:
            return None
        while True:
            key = self.key_queue[0]
            if not self.pending_costs[key]:
                # An empty bucket, only possible via start_domains: drop it.
                self._remove(key)
                if not self.key_queue:
                    return None
                continue
            if key not in self.visited:
                self.deficit[key] += self._quota(key)
                self.visited.add(key)
            if self._fits(key):
                return self._serve(key)
            # The head item does not fit yet: move to the next source. The
            # scaled quota guarantees it fits on the next visit, so the
            # rotation always makes progress.
            self.key_queue.rotate(-1)
            if self.key_queue[0] in self.visited:
                # Wrapped past every active source: start a new round.
                self.visited.clear()

    def peek(self) -> Any | None:
        for key in self.key_queue:
            item = self.queues[key].peek()
            if item is not None:
                return item
        return None

    def clear(self) -> None:
        for q in self.queues.values():
            _clear(q)
            q.close()
        self.queues.clear()
        self.pending_costs.clear()
        self.deficit.clear()
        self.visited.clear()
        self.cost_heap.clear()
        self.live_ids.clear()
        self.key_queue.clear()

    def close(self) -> list[Hashable]:
        active = []
        for key, q in self.queues.items():
            if len(q):
                active.append(key)
            q.close()
        return active

    def __len__(self) -> int:
        return sum(len(q) for q in self.queues.values()) if self.queues else 0
