from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar, cast

if TYPE_CHECKING:
    import os

T = TypeVar("T")

_META_FILENAME = "sizedqueue.json"
_META_VERSION = 1


class OverflowPolicy(Enum):
    """What a queue does when a task does not fit in the remaining capacity."""

    REJECT = "reject"
    EVICT = "evict"


class EvictionPriority(Enum):
    """Which value level is reclaimed first when space has to be made.

    The task ``priority`` is an integer whose mapping to "value" is chosen
    here, so the eviction condition is fully explicit:

    * :attr:`LOWEST_VALUE` treats the smallest ``priority`` number as the
      lowest value and evicts it first. Use it when a larger priority means a
      more valuable task (the generic scale).
    * :attr:`HIGHEST_VALUE` treats the largest ``priority`` number as the
      lowest value and evicts it first. Use it when a smaller priority means a
      more valuable task, matching the inverted scale of
      :class:`~queuelib.pqueue.PriorityQueue`.
    """

    LOWEST_VALUE = "lowest_value"
    HIGHEST_VALUE = "highest_value"


class EvictionOrder(Enum):
    """How tasks of the same priority are ordered for eviction."""

    FIFO = "fifo"
    LIFO = "lifo"


# Returned in ``PushResult.reason``; kept as plain strings so callers can match
# on them without importing anything beyond the queue itself.
REASON_OVERSIZED = "oversized"
REASON_FULL = "full"
REASON_NO_EVICTABLE = "no_evictable"


@dataclass(frozen=True)
class SizedQueueConfig:
    """Static configuration of a :class:`SizedQueue`.

    ``capacity`` is the hard upper bound on the sum of the declared sizes of
    the tasks currently held by the queue. It is measured in whatever unit the
    caller declares per task, never in bytes or estimated object size.

    ``on_full`` selects between rejecting a task that does not fit
    (:attr:`OverflowPolicy.REJECT`) and evicting waiting tasks to make room
    (:attr:`OverflowPolicy.EVICT`).

    ``eviction_priority`` decides which value level is reclaimed first and
    ``eviction_order`` breaks ties between tasks of the same priority. Both are
    ignored while the policy is :attr:`OverflowPolicy.REJECT`.
    """

    capacity: int
    on_full: OverflowPolicy = OverflowPolicy.REJECT
    eviction_priority: EvictionPriority = EvictionPriority.LOWEST_VALUE
    eviction_order: EvictionOrder = EvictionOrder.FIFO

    def __post_init__(self) -> None:
        if not isinstance(self.capacity, int) or isinstance(self.capacity, bool):
            raise TypeError("capacity must be an int")
        if self.capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if not isinstance(self.on_full, OverflowPolicy):
            raise TypeError("on_full must be an OverflowPolicy member")
        if not isinstance(self.eviction_priority, EvictionPriority):
            raise TypeError("eviction_priority must be an EvictionPriority member")
        if not isinstance(self.eviction_order, EvictionOrder):
            raise TypeError("eviction_order must be an EvictionOrder member")


@dataclass
class SizedTask(Generic[T]):
    """A unit of work together with its declared size.

    ``size`` is declared by the caller and treated as an opaque, exact number;
    the queue never estimates it from the in-memory or serialized footprint of
    ``item``. ``priority`` drives eviction (its meaning depends on
    :attr:`SizedQueueConfig.eviction_priority`), and ``pinned`` marks a task
    that must never be evicted while it is waiting. ``seq`` is assigned by the
    queue and only exists to make admission order durable.
    """

    item: T
    size: int
    priority: int = 0
    pinned: bool = False
    seq: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.size, int) or isinstance(self.size, bool):
            raise TypeError("task size must be an int")
        if self.size < 0:
            raise ValueError("task size must be non-negative")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise TypeError("task priority must be an int")
        if not isinstance(self.pinned, bool):
            raise TypeError("task pinned must be a bool")


@dataclass
class SizedQueueStats:
    """Cumulative counters, kept across ``clear()`` and process restarts."""

    submitted: int = 0
    accepted: int = 0
    rejected: int = 0
    evicted: int = 0


@dataclass
class PushResult(Generic[T]):
    """Outcome of a single :meth:`SizedQueue.push` call.

    Every task that the caller can no longer find in the queue is handed back
    in full here. ``rejected`` is the incoming task when it was not admitted,
    ``evicted`` lists the waiting tasks removed to make room, and ``returned``
    is both combined. ``reason`` explains a rejection and is ``None`` on
    acceptance.
    """

    accepted: bool
    evicted: list[SizedTask[T]] = field(default_factory=list)
    rejected: SizedTask[T] | None = None
    reason: str | None = None

    @property
    def returned(self) -> list[SizedTask[T]]:
        if self.rejected is None:
            return list(self.evicted)
        return [self.rejected, *self.evicted]


class SizedQueue(Generic[T]):
    """A capacity-bounded queue metered by the declared size of its tasks.

    The queue holds :class:`SizedTask` objects and guarantees that the sum of
    their declared sizes never exceeds ``config.capacity``. When a task does
    not fit, the configured :class:`OverflowPolicy` either rejects it or evicts
    lower-value waiting tasks; pinned tasks are never evicted.

    A task whose declared size exceeds the whole capacity is always rejected
    and never causes an eviction, so admitting one oversized task cannot empty
    the queue through repeated eviction.

    State (tasks, configuration and statistics) is persisted as JSON under
    ``path``, rewritten via an atomic rename after every mutating operation so
    that a reopen never reads a torn file and the statistics stay consistent
    across restarts. Like the file queues, no ``fsync()`` is performed, so an
    OS crash or power loss can still discard the most recent writes. Items
    must be JSON-serializable; their serialized form is unrelated to the
    declared size, which is stored separately and never inferred.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        config: SizedQueueConfig | None = None,
    ) -> None:
        self._dir = Path(path)
        self._meta_path = self._dir / _META_FILENAME
        self._items: dict[int, SizedTask[T]] = {}
        self._used = 0
        self._next_seq = 0
        self._stats = SizedQueueStats()
        if self._meta_path.exists():
            self._config = self._load(config)
        else:
            if config is None:
                raise ValueError(
                    "config is required when creating a new SizedQueue; "
                    f"no state was found at {self._dir}"
                )
            self._config = config
            self._dir.mkdir(parents=True, exist_ok=True)
            self._persist()

    @property
    def config(self) -> SizedQueueConfig:
        return self._config

    @property
    def capacity(self) -> int:
        return self._config.capacity

    @property
    def used(self) -> int:
        """Sum of the declared sizes of the tasks currently held."""
        return self._used

    @property
    def stats(self) -> SizedQueueStats:
        return SizedQueueStats(
            submitted=self._stats.submitted,
            accepted=self._stats.accepted,
            rejected=self._stats.rejected,
            evicted=self._stats.evicted,
        )

    def push(self, task: SizedTask[T]) -> PushResult[T]:
        if not isinstance(task, SizedTask):
            raise TypeError(f"Unsupported type: {type(task).__name__}")
        # Fail before touching any state, so a non-serializable item can never
        # leave the in-memory queue diverging from what is stored on disk.
        self._ensure_serializable(task.item)
        self._stats.submitted += 1
        task.seq = self._next_seq
        self._next_seq += 1

        if task.size > self._config.capacity:
            self._stats.rejected += 1
            self._persist()
            return PushResult(accepted=False, rejected=task, reason=REASON_OVERSIZED)

        evicted: list[SizedTask[T]] = []
        while self._used + task.size > self._config.capacity:
            if self._config.on_full is not OverflowPolicy.EVICT:
                self._stats.rejected += 1
                self._persist()
                return PushResult(
                    accepted=False,
                    evicted=evicted,
                    rejected=task,
                    reason=REASON_FULL,
                )
            candidate = self._select_eviction_candidate()
            if candidate is None:
                self._stats.rejected += 1
                self._persist()
                return PushResult(
                    accepted=False,
                    evicted=evicted,
                    rejected=task,
                    reason=REASON_NO_EVICTABLE,
                )
            del self._items[candidate.seq]
            self._used -= candidate.size
            self._stats.evicted += 1
            evicted.append(candidate)

        self._items[task.seq] = task
        self._used += task.size
        self._stats.accepted += 1
        self._persist()
        return PushResult(accepted=True, evicted=evicted)

    def pop(self) -> SizedTask[T] | None:
        if not self._items:
            return None
        seq = next(iter(self._items))
        task = self._items.pop(seq)
        self._used -= task.size
        self._persist()
        return task

    def peek(self) -> SizedTask[T] | None:
        if not self._items:
            return None
        return next(iter(self._items.values()))

    def clear(self) -> None:
        """Remove every waiting task without resetting the statistics."""
        self._items.clear()
        self._used = 0
        self._persist()

    def close(self) -> None:
        self._persist()

    def __len__(self) -> int:
        return len(self._items)

    def _select_eviction_candidate(self) -> SizedTask[T] | None:
        eligible = [task for task in self._items.values() if not task.pinned]
        if not eligible:
            return None
        return min(eligible, key=self._eviction_key)

    def _eviction_key(self, task: SizedTask[T]) -> tuple[int, int]:
        if self._config.eviction_priority is EvictionPriority.LOWEST_VALUE:
            value_key = task.priority
        else:
            value_key = -task.priority
        if self._config.eviction_order is EvictionOrder.FIFO:
            order_key = task.seq
        else:
            order_key = -task.seq
        return value_key, order_key

    @staticmethod
    def _ensure_serializable(item: T) -> None:
        try:
            json.dumps(item)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"SizedQueue items must be JSON-serializable; "
                f"cannot store {type(item).__name__}"
            ) from exc

    def _persist(self) -> None:
        data = {
            "version": _META_VERSION,
            "config": {
                "capacity": self._config.capacity,
                "on_full": self._config.on_full.value,
                "eviction_priority": self._config.eviction_priority.value,
                "eviction_order": self._config.eviction_order.value,
            },
            "next_seq": self._next_seq,
            "stats": {
                "submitted": self._stats.submitted,
                "accepted": self._stats.accepted,
                "rejected": self._stats.rejected,
                "evicted": self._stats.evicted,
            },
            "items": [
                {
                    "seq": task.seq,
                    "size": task.size,
                    "priority": task.priority,
                    "pinned": task.pinned,
                    "item": task.item,
                }
                for task in self._items.values()
            ],
        }
        tmp_path = self._meta_path.with_name(f".{_META_FILENAME}.tmp")
        tmp_path.write_text(json.dumps(data, separators=(",", ":")))
        tmp_path.replace(self._meta_path)

    def _load(self, config: SizedQueueConfig | None) -> SizedQueueConfig:
        data = cast("dict[str, object]", json.loads(self._meta_path.read_text()))
        stored_config = self._config_from_dict(
            cast("dict[str, object]", data["config"])
        )
        if config is not None and config != stored_config:
            raise ValueError(
                "the config passed to SizedQueue does not match the one stored "
                f"at {self._meta_path}"
            )

        raw_stats = cast("dict[str, int]", data["stats"])
        self._stats = SizedQueueStats(
            submitted=raw_stats["submitted"],
            accepted=raw_stats["accepted"],
            rejected=raw_stats["rejected"],
            evicted=raw_stats["evicted"],
        )
        self._next_seq = cast("int", data["next_seq"])

        raw_items = cast("list[dict[str, object]]", data["items"])
        restored = (
            SizedTask(
                item=raw["item"],
                size=cast("int", raw["size"]),
                priority=cast("int", raw["priority"]),
                pinned=cast("bool", raw["pinned"]),
                seq=cast("int", raw["seq"]),
            )
            for raw in raw_items
        )
        for task in sorted(restored, key=lambda task: task.seq):
            self._items[task.seq] = cast("SizedTask[T]", task)
        self._used = sum(task.size for task in self._items.values())
        return stored_config

    @staticmethod
    def _config_from_dict(raw: dict[str, object]) -> SizedQueueConfig:
        return SizedQueueConfig(
            capacity=cast("int", raw["capacity"]),
            on_full=OverflowPolicy(cast("str", raw["on_full"])),
            eviction_priority=EvictionPriority(cast("str", raw["eviction_priority"])),
            eviction_order=EvictionOrder(cast("str", raw["eviction_order"])),
        )
