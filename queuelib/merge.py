from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Hashable, Iterable

T = TypeVar("T")

# Sentinel meaning that at least one active shard has never reported a
# watermark, so no timestamp can yet be proven globally ordered.
_BLOCKED = object()


class ShardState(Enum):
    """Lifecycle state of a shard in a :class:`MergingQueue`."""

    ACTIVE = "active"
    CLOSED = "closed"


@dataclass(frozen=True)
class LateRecord(Generic[T]):
    """An item that arrived after its timestamp was already passed.

    Late records never re-enter the ordered output stream; they are only
    available through :meth:`MergingQueue.pop_late`.
    """

    shard: Hashable
    sequence: int
    timestamp: Any
    value: T


class _Shard(Generic[T]):
    __slots__ = ("heap", "id", "next_seq", "state", "used_seqs", "watermark")

    def __init__(self, shard_id: Hashable) -> None:
        self.id = shard_id
        # Entries kept as (timestamp, sequence, value), ordered by timestamp
        # first and by the in-shard sequence on ties.
        self.heap: list[tuple[Any, int, T]] = []
        self.watermark: Any | None = None
        self.state: ShardState = ShardState.ACTIVE
        self.next_seq: int = 0
        self.used_seqs: set[int] = set()


class MergingQueue(Generic[T]):
    """Merge ordered streams produced by independent collection shards.

    Each shard pushes items tagged with an observation timestamp and reports
    progress through a watermark: after reporting watermark ``T`` it promises
    not to push any further item with a timestamp lower than or equal to ``T``.
    The queue only exposes items whose ordering relative to every still-active
    shard is already certain, so downstream consumers get a single globally
    ordered stream even when some shards are temporarily empty.

    An empty buffer never means a shard is finished: a shard blocks output past
    its watermark until it either advances the watermark or is explicitly
    closed with :meth:`close_shard`. A closed shard may rejoin through
    :meth:`reopen_shard`.

    Output ordering is ``(timestamp, shard id, sequence)``. The shard id must
    be stable and mutually comparable; the sequence is either assigned
    automatically per shard or supplied explicitly.

    Items whose timestamp is lower than the highest timestamp already emitted
    (the committed watermark) are late. This includes data arriving after the
    producing shard closed and rejoined. Late items never overtake the emitted
    output and are returned separately through :meth:`pop_late`.
    """

    def __init__(self, shard_ids: Iterable[Hashable] = ()) -> None:
        self._shards: dict[Hashable, _Shard[T]] = {}
        for shard_id in shard_ids:
            self.register_shard(shard_id)
        # Entries proven globally ordered, as (timestamp, shard id, seq, value).
        self._ready: list[tuple[Any, Hashable, int, T]] = []
        self._late: deque[LateRecord[T]] = deque()
        # Highest timestamp ever handed out through pop().
        self._committed: Any | None = None

    def register_shard(self, shard: Hashable) -> None:
        """Register a shard, including one that joins after the start."""
        if shard in self._shards:
            raise ValueError(f"shard {shard!r} is already registered")
        self._shards[shard] = _Shard(shard)

    def advance_watermark(self, shard: Hashable, timestamp: Any) -> None:
        """Report that *shard* will not push items with timestamp <= *timestamp*.

        The watermark can only move forward.
        """
        state = self._require_active(shard)
        if state.watermark is not None and timestamp < state.watermark:
            msg = (
                f"watermark for shard {shard!r} cannot move backwards: "
                f"{state.watermark!r} -> {timestamp!r}"
            )
            raise ValueError(msg)
        state.watermark = timestamp

    def push(
        self,
        shard: Hashable,
        timestamp: Any,
        value: T,
        sequence: int | None = None,
    ) -> int:
        """Add an item observed by *shard* at *timestamp*.

        When *sequence* is omitted it is assigned automatically, starting at 0
        per shard. Explicit sequences may be submitted out of order but must
        not repeat one already used by the shard; gaps are allowed. Returns
        the sequence assigned to the item.
        """
        state = self._require_active(shard)
        if sequence is None:
            sequence = state.next_seq
        elif sequence in state.used_seqs:
            msg = f"sequence {sequence} for shard {shard!r} was already used"
            raise ValueError(msg)
        state.used_seqs.add(sequence)
        state.next_seq = max(state.next_seq, sequence + 1)
        heapq.heappush(state.heap, (timestamp, sequence, value))
        return sequence

    def close_shard(self, shard: Hashable) -> None:
        """Mark *shard* as finished, removing it from the watermark barrier."""
        state = self._require_shard(shard)
        if state.state is ShardState.CLOSED:
            raise RuntimeError(f"shard {shard!r} is already closed")
        state.state = ShardState.CLOSED

    def reopen_shard(self, shard: Hashable) -> None:
        """Let a previously closed shard rejoin the merge.

        The shard keeps its watermark and sequence counter, and blocks output
        again until it reports further progress.
        """
        state = self._require_shard(shard)
        if state.state is ShardState.ACTIVE:
            raise RuntimeError(f"shard {shard!r} is already active")
        state.state = ShardState.ACTIVE

    def pop(self) -> T | None:
        """Return the next globally ordered item, or ``None`` if none is certain."""
        if not self._ready:
            self._pump()
        if not self._ready:
            return None
        timestamp, _shard, _sequence, value = heapq.heappop(self._ready)
        if self._committed is None or timestamp > self._committed:
            self._committed = timestamp
        return value

    def peek(self) -> T | None:
        """Return the next item without consuming it, or ``None``."""
        if not self._ready:
            self._pump()
        if not self._ready:
            return None
        return self._ready[0][3]

    def pop_late(self) -> LateRecord[T] | None:
        """Return the next late record, or ``None`` when the side channel is empty."""
        if not self._late:
            return None
        return self._late.popleft()

    def peek_late(self) -> LateRecord[T] | None:
        """Return the next late record without consuming it, or ``None``."""
        return self._late[0] if self._late else None

    def clear(self) -> None:
        """Drop every buffered, ready and late item, keeping shards registered."""
        for state in self._shards.values():
            state.heap = []
            state.watermark = None
            state.next_seq = 0
            state.used_seqs = set()
        self._ready = []
        self._late = deque()
        self._committed = None

    def close(self) -> None:
        pass

    @property
    def committed_watermark(self) -> Any | None:
        """Highest timestamp ever emitted through :meth:`pop`."""
        return self._committed

    @property
    def active_shards(self) -> list[Hashable]:
        """Shards currently participating in the watermark barrier."""
        return [
            shard_id
            for shard_id, state in self._shards.items()
            if state.state is ShardState.ACTIVE
        ]

    @property
    def is_closed(self) -> bool:
        """Whether at least one shard is registered and all of them are closed."""
        return bool(self._shards) and all(
            state.state is ShardState.CLOSED for state in self._shards.values()
        )

    def shard_state(self, shard: Hashable) -> ShardState:
        """Return the lifecycle state of *shard*."""
        return self._require_shard(shard).state

    def shard_watermark(self, shard: Hashable) -> Any | None:
        """Return the last watermark reported by *shard*."""
        return self._require_shard(shard).watermark

    @property
    def late_size(self) -> int:
        """Number of late records waiting on the side channel."""
        return len(self._late)

    def __len__(self) -> int:
        """Number of non-late items held, both buffered and ready to emit."""
        return len(self._ready) + sum(
            len(state.heap) for state in self._shards.values()
        )

    def _pump(self) -> None:
        cutoff = self._cutoff()
        if cutoff is _BLOCKED:
            return
        for state in self._shards.values():
            while state.heap and (cutoff is None or state.heap[0][0] <= cutoff):
                timestamp, sequence, value = heapq.heappop(state.heap)
                if self._committed is not None and timestamp < self._committed:
                    self._late.append(LateRecord(state.id, sequence, timestamp, value))
                else:
                    heapq.heappush(self._ready, (timestamp, state.id, sequence, value))

    def _cutoff(self) -> Any:
        active = [
            state for state in self._shards.values() if state.state is ShardState.ACTIVE
        ]
        if not active:
            return None
        cutoff: Any = None
        for state in active:
            if state.watermark is None:
                return _BLOCKED
            if cutoff is None or state.watermark < cutoff:
                cutoff = state.watermark
        return cutoff

    def _require_shard(self, shard: Hashable) -> _Shard[T]:
        try:
            return self._shards[shard]
        except KeyError as exc:
            raise KeyError(f"unknown shard {shard!r}") from exc

    def _require_active(self, shard: Hashable) -> _Shard[T]:
        state = self._require_shard(shard)
        if state.state is ShardState.CLOSED:
            raise RuntimeError(f"shard {shard!r} is closed, reopen it first")
        return state
