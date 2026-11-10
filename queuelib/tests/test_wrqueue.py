import math
from pathlib import Path

import pytest

from queuelib import RoundRobinQueue
from queuelib.queue import FifoDiskQueue, FifoMemoryQueue, FifoSQLiteQueue
from queuelib.tests import DummyQueue, QueuelibTestCase, track_closed
from queuelib.wrqueue import WeightedRoundRobinQueue


def make_queue(weights=None, start_domains=()):
    return WeightedRoundRobinQueue(
        lambda key: track_closed(FifoMemoryQueue)(), weights, start_domains
    )


class _Lcg:
    """Deterministic, dependency-free pseudo-random source for tests."""

    def __init__(self, seed):
        self._state = seed

    def _next(self):
        self._state = (self._state * 1103515245 + 12345) & 0x7FFFFFFF
        return self._state

    def randint(self, low, high):
        return low + self._next() % (high - low + 1)

    def choice(self, sequence):
        return sequence[self._next() % len(sequence)]

    def random(self):
        return self._next() / 0x7FFFFFFF


class WeightedQueueTestMixin:
    def setUp(self):
        super().setUp()
        self.q = WeightedRoundRobinQueue(self.qfactory, {"1": 1.0, "2": 1.0})

    def qfactory(self, key):
        return track_closed(FifoMemoryQueue)()

    def test_len(self):
        assert not self.q
        assert len(self.q) == 0
        self.q.push(b"a", "1", 1.0)
        self.q.push(b"b", "2", 1.0)
        self.q.push(b"c", "1", 4.0)
        assert self.q
        assert len(self.q) == 3
        items = sorted(self.q.pop() for _ in range(3))
        assert items == [b"a", b"b", b"c"]
        assert not self.q
        assert len(self.q) == 0
        assert self.q.pop() is None

    def test_peek(self):
        assert self.q.peek() is None
        self.q.push(b"a", "1", 1.0)
        self.q.push(b"b", "1", 1.0)
        assert self.q.peek() == b"a"
        assert self.q.peek() == b"a"
        assert self.q.pop() == b"a"
        assert self.q.peek() == b"b"

    def test_pop_returns_falsy_items(self):
        self.q.push(b"", "1", 1.0)
        assert len(self.q) == 1
        assert self.q.peek() == b""
        assert self.q.pop() == b""
        assert len(self.q) == 0
        assert self.q.pop() is None

    def test_close_returns_active(self):
        self.q.push(b"a", "1", 1.0)
        self.q.push(b"b", "2", 1.0)
        self.q.pop()
        assert self.q.close() == ["2"]

    def test_clear(self):
        self.q.push(b"a", "1", 1.0)
        iqueues = list(self.q.queues.values())
        self.q.clear()
        assert len(self.q) == 0
        assert self.q.peek() is None
        assert self.q.pop() is None
        assert all(q.closed for q in iqueues)
        self.q.push(b"b", "2", 1.0)
        assert self.q.pop() == b"b"
        assert not self.q.close()


class FifoMemoryWeightedQueueTest(WeightedQueueTestMixin, QueuelibTestCase):
    pass


class InvalidInputTest(QueuelibTestCase):
    def setUp(self):
        super().setUp()
        self.q = make_queue({"a": 1.0})

    def test_invalid_cost_rejected(self):
        for bad_cost in (
            0,
            -1,
            -0.5,
            float("nan"),
            float("inf"),
            float("-inf"),
            "1",
            None,
            True,
        ):
            with (
                self.subTest(bad_cost=bad_cost),
                pytest.raises(ValueError, match="finite positive number"),
            ):
                self.q.push(b"x", "a", bad_cost)
            assert len(self.q) == 0
            assert self.q.queues == {}

    def test_invalid_cost_does_not_touch_existing_queue(self):
        self.q.push(b"keep", "a", 1.0)
        with pytest.raises(ValueError, match="finite positive number"):
            self.q.push(b"drop", "a", 0.0)
        assert len(self.q) == 1
        assert self.q.pop() == b"keep"

    def test_invalid_constructor_weight_rejected(self):
        for bad_weight in (
            0,
            -2,
            float("nan"),
            float("inf"),
            float("-inf"),
            "x",
            False,
        ):
            with (
                self.subTest(bad_weight=bad_weight),
                pytest.raises(ValueError, match="finite positive number"),
            ):
                WeightedRoundRobinQueue(
                    lambda key: FifoMemoryQueue(), {"a": bad_weight}
                )

    def test_invalid_set_weight_rejected_and_kept(self):
        with pytest.raises(ValueError, match="finite positive number"):
            self.q.set_weight("a", 0.0)
        assert self.q.weights["a"] == 1.0

    def test_default_weight_for_unknown_key(self):
        self.q.push(b"x", "brand-new", 1.0)
        assert self.q.weights["brand-new"] == 1.0

    def test_set_weight_before_first_push_is_used(self):
        queue = make_queue()
        queue.set_weight("a", 7.0)
        queue.push(b"x", "a", 1.0)
        assert queue.weights["a"] == 7.0


class RejoinTest(QueuelibTestCase):
    def test_dry_source_is_removed_and_rejoins_at_back(self):
        q = make_queue({"a": 1.0, "b": 10.0})
        q.push(b"a1", "a", 1.0)
        assert q.pop() == b"a1"
        # "a" ran dry: it leaves the rotation and keeps no deficit.
        assert "a" not in q.queues
        q.push(b"b1", "b", 5.0)
        q.push(b"a2", "a", 1.0)
        # The rejoining source queues behind the source that stayed active.
        assert [q.pop(), q.pop()] == [b"b1", b"a2"]

    def test_rejoining_source_gets_fresh_deficit(self):
        q = make_queue({"a": 1.0, "b": 1.0})
        q.push(b"a0", "a", 1.0)
        for i in range(6):
            q.push(f"b{i}".encode(), "b", 1.0)
        assert q.pop() == b"a0"
        assert "a" not in q.deficit
        q.push(b"a1", "a", 1.0)
        assert q.deficit["a"] == 0.0


class WeightChangeTest(QueuelibTestCase):
    def test_weight_change_reshares_future_service(self):
        q = make_queue({"a": 1.0, "b": 1.0})
        for i in range(80):
            q.push(f"a{i}".encode(), "a", 1.0)
            q.push(f"b{i}".encode(), "b", 1.0)
        q.set_weight("b", 3.0)
        counts = {"a": 0, "b": 0}
        for _ in range(80):
            counts[q.pop().decode()[0]] += 1
        # b now owns 3/4 of the total weight, so it serves three times a.
        assert counts == {"a": 20, "b": 60}

    def test_weight_change_keeps_accumulated_deficit(self):
        q = make_queue({"a": 1.0, "b": 1.0})
        q.push(b"a0", "a", 1.0)
        q.push(b"a1", "a", 1.0)
        q.push(b"b0", "b", 1.0)
        q.pop()  # "a" served but stays backlogged, so its deficit survives
        deficit_before = q.deficit["a"]
        q.set_weight("a", 9.0)
        assert q.deficit["a"] == deficit_before
        assert q.weights["a"] == 9.0


class OversizedItemTest(QueuelibTestCase):
    def test_oversized_item_does_not_stall_and_is_served(self):
        # A 100-cost item next to unit items. The scaled quota always covers
        # the most expensive pending item, so the front source serves "big"
        # on its visit; the queue never returns None early and drains fully.
        q = make_queue({"a": 1.0, "b": 1.0, "c": 1.0})
        q.push(b"big", "a", 100.0)
        q.push(b"x", "b", 1.0)
        q.push(b"y", "c", 1.0)
        assert [q.pop(), q.pop(), q.pop()] == [b"big", b"x", b"y"]
        assert q.pop() is None

    def test_oversized_item_charged_at_true_cost(self):
        # Serving the 100-cost item leaves its source in zero credit: it must
        # not get extra service afterwards compared with an equal-weight peer.
        q = make_queue({"a": 1.0, "b": 1.0})
        q.push(b"big", "a", 100.0)
        q.push(b"b0", "b", 1.0)
        q.pop()  # big
        q.pop()  # b0
        for i in range(40):
            q.push(f"a{i}".encode(), "a", 1.0)
            q.push(f"b{i}".encode(), "b", 1.0)
        counts = {"a": 0, "b": 0}
        for _ in range(80):
            counts[q.pop().decode()[0]] += 1
        assert counts["a"] == counts["b"] == 40

    def test_all_oversized_does_not_stall(self):
        q = make_queue({"a": 1.0, "b": 1.0})
        q.push(b"A", "a", 50.0)
        q.push(b"B", "b", 50.0)
        assert [q.pop(), q.pop()] == [b"A", b"B"]
        assert q.pop() is None

    def test_interleaved_costs_never_block_drain(self):
        # Mixed pushes and pops across three sources and a wide cost range
        # must always terminate cleanly, never returning None while non-empty.
        rng = _Lcg(20261110)
        q = make_queue({"a": 1.0, "b": 2.0, "c": 1.0})
        outstanding = 0
        for _ in range(2000):
            key = rng.choice(["a", "b", "c"])
            q.push(key.encode(), key, float(rng.randint(1, 50)))
            outstanding += 1
            if rng.random() < 0.7:
                assert q.pop() is not None
                outstanding -= 1
        while len(q):
            assert q.pop() is not None
            outstanding -= 1
        assert outstanding == 0
        assert q.pop() is None


class LowWeightProgressTest(QueuelibTestCase):
    def test_low_weight_source_keeps_moving(self):
        q = make_queue({"hi": 100.0, "lo": 1.0})
        for i in range(50):
            q.push(f"h{i}".encode(), "hi", 1.0)
            q.push(f"l{i}".encode(), "lo", 1.0)
        keys = [q.pop().decode()[0] for _ in range(100)]
        assert keys.count("l") == 50
        assert keys.count("h") == 50


def _deviation_bound(weights, max_costs):
    """Constant bound on |served_i - phi_i * total_served| at every prefix.

    Each round flow i receives quantum Q_i = w_i * sigma with
    sigma = max item cost / min weight, so its deficit stays within
    |deficit_i| <= Q_i + F_i where F_i is its largest item cost. The
    accounting identity
        served_i = phi_i * total_served + deficit_i - phi_i * sum_j deficit_j
    then yields the constant bound returned here.
    """
    total_weight = sum(weights.values())
    phi = {key: weight / total_weight for key, weight in weights.items()}
    sigma = max(max_costs.values()) / min(weights.values())
    quantum = {key: weights[key] * sigma for key in weights}
    quantum_sum = sum(quantum.values())
    cost_sum = sum(max_costs.values())
    return {
        key: 2 * quantum[key] + max_costs[key] + phi[key] * (quantum_sum + cost_sum)
        for key in weights
    }


class FairnessTest(QueuelibTestCase):
    def _stream(self, weights, costs, horizon, backlog=20):
        """Run *horizon* pops while refilling every source, so each source
        stays permanently backlogged: the regime in which the share and
        error-bound guarantees are stated.

        *costs* maps each source to either a fixed cost or a fixed sequence of
        costs that is pushed in order and cycled. No network or real download
        is involved.
        """
        sequences = {
            key: (value,) if isinstance(value, (int, float)) else tuple(value)
            for key, value in costs.items()
        }
        max_costs = {key: max(sequence) for key, sequence in sequences.items()}
        q = make_queue(weights)
        for key in weights:
            for idx in range(backlog):
                q.push(key.encode(), key, sequences[key][idx % len(sequences[key])])
        total_weight = sum(weights.values())
        phi = {key: weight / total_weight for key, weight in weights.items()}
        bound = _deviation_bound(weights, max_costs)
        served = dict.fromkeys(weights, 0.0)
        cursor = dict.fromkeys(weights, 0)
        for _ in range(horizon):
            key = q.pop().decode()
            sequence = sequences[key]
            cost = sequence[cursor[key] % len(sequence)]
            cursor[key] += 1
            served[key] += cost
            q.push(key.encode(), key, cost)
            total = sum(served.values())
            for source in weights:
                deviation = abs(served[source] - phi[source] * total)
                assert deviation <= bound[source] + 1e-9
        q.clear()
        return phi, served

    def test_shares_track_weights_with_unit_costs(self):
        weights = {"a": 1.0, "b": 3.0}
        costs = {"a": 1.0, "b": 1.0}
        phi, served = self._stream(weights, costs, 3000)
        total = sum(served.values())
        for source in weights:
            assert math.isclose(served[source] / total, phi[source], abs_tol=0.01)

    def test_cheap_and_expensive_sources_split_cost_evenly(self):
        # The motivating case: KB-sized vs MB-sized tasks, equal weights.
        weights = {"kb": 1.0, "mb": 1.0}
        costs = {"kb": 1.0, "mb": 10.0}
        phi, served = self._stream(weights, costs, 3000)
        total = sum(served.values())
        assert math.isclose(served["kb"] / total, phi["kb"], abs_tol=0.02)
        assert math.isclose(served["mb"] / total, phi["mb"], abs_tol=0.02)

    def test_fixed_cost_sequences_within_error_bound(self):
        # Each source cycles a fixed sequence of mixed small and large costs.
        weights = {"a": 1.0, "b": 3.0, "c": 1.0}
        costs = {
            "a": (1.0, 4.0, 2.0),
            "b": (10.0, 3.0, 7.0, 12.0),
            "c": (2.0, 5.0),
        }
        phi, served = self._stream(weights, costs, 4000)
        total = sum(served.values())
        for source in weights:
            assert math.isclose(served[source] / total, phi[source], abs_tol=0.03)

    def test_cumulative_share_within_error_bound(self):
        for weights, costs in (
            ({"a": 1.0, "b": 3.0}, {"a": 1.0, "b": 10.0}),
            ({"a": 1.0, "b": 3.0}, {"a": 2.0, "b": 10.0}),
            ({"x": 2.0, "y": 5.0, "z": 1.0}, {"x": 3.0, "y": 7.0, "z": 2.0}),
        ):
            with self.subTest(weights=weights, costs=costs):
                phi, served = self._stream(weights, costs, 3000)
                total = sum(served.values())
                for source in weights:
                    assert math.isclose(
                        served[source] / total, phi[source], abs_tol=0.03
                    )


class StartDomainsTest(QueuelibTestCase):
    def test_empty_start_domain_is_skipped(self):
        q = make_queue({"1": 1.0, "2": 1.0}, start_domains=["1", "2"])
        assert len(q) == 0
        q.push(b"next", "2", 1.0)
        assert q.peek() == b"next"
        assert q.pop() == b"next"
        assert q.pop() is None
        assert not q.close()

    def test_start_domains_rotate_in_order(self):
        q = make_queue({"1": 1.0, "2": 1.0}, start_domains=["1", "2"])
        q.push(b"c", "1", 1.0)
        q.push(b"d", "2", 1.0)
        assert [q.pop(), q.pop()] == [b"c", b"d"]


class DiskTestMixin:
    def test_peek_after_failed_push(self):
        with pytest.raises(TypeError):
            self.q.push(lambda x: x, "empty", 1.0)
        self.q.push(b"next", "valid", 1.0)
        assert self.q.peek() == b"next"
        assert self.q.pop() == b"next"
        assert self.q.peek() is None
        assert not self.q.close()

    def test_close_reports_active_domains(self):
        self.q.push(b"a", "1", 1.0)
        self.q.push(b"b", "2", 10.0)
        assert sorted(self.q.close()) == ["1", "2"]

    def test_empty_close_cleans_up(self):
        self.q.push(b"a", "1", 1.0)
        assert self.q.pop() == b"a"
        assert self.q.close() == []


class FifoDiskWeightedQueueTest(
    WeightedQueueTestMixin, DiskTestMixin, QueuelibTestCase
):
    def qfactory(self, key):
        return track_closed(FifoDiskQueue)(Path(self.qdir, str(key)))


class FifoSQLiteWeightedQueueTest(
    WeightedQueueTestMixin, DiskTestMixin, QueuelibTestCase
):
    def qfactory(self, key):
        return track_closed(FifoSQLiteQueue)(Path(self.qdir, str(key)))


class DummyWeightedQueueTest(QueuelibTestCase):
    def test_clear_without_clear_method(self):
        q = WeightedRoundRobinQueue(lambda key: DummyQueue(), {"1": 1.0})
        q.push(b"a", "1", 1.0)
        q.push(b"b", "1", 2.0)
        q.clear()
        assert len(q) == 0
        assert q.pop() is None
        assert not q.close()


class RoundRobinQueueUnchangedTest(QueuelibTestCase):
    def test_original_round_robin_semantics_untouched(self):
        q = RoundRobinQueue(lambda key: FifoMemoryQueue())
        q.push(b"a", "1")
        q.push(b"b", "1")
        q.push(b"c", "2")
        q.push(b"d", "2")
        assert [q.pop(), q.pop(), q.pop(), q.pop()] == [b"a", b"c", b"b", b"d"]
        assert q.pop() is None
