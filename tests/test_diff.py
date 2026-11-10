import copy

import pytest

from itemadapter import CompareConfig, Tolerance, compare_records
from itemadapter.diff import MISSING


def make_record(offers, **extra):
    record = {"name": "shop-a", "offers": offers}
    record.update(extra)
    return record


def offer(sku, price, stock=10):
    return {"sku": sku, "price": price, "stock": stock}


KEYED_CONFIG = CompareConfig(
    key_fields={"offers": "sku"},
    tolerances={"price": Tolerance(relative=0.01)},
)


class TestReorder:
    def test_reordered_keyed_collection_is_not_a_change(self):
        old = make_record([offer("A", 10.0), offer("B", 20.0), offer("C", 30.0)])
        new = make_record([offer("C", 30.0), offer("A", 10.0), offer("B", 20.0)])

        report = compare_records(old, new, KEYED_CONFIG)

        assert report.changes == []
        assert report.issues == []
        assert not report.needs_notification
        assert [(p.key, p.old_index, p.new_index) for p in report.pairings] == [
            ("A", 0, 1),
            ("B", 1, 2),
            ("C", 2, 0),
        ]

    def test_reordered_plain_list_is_reported_in_order(self):
        old = make_record(None, tags=["new", "sale"])
        new = make_record(None, tags=["sale", "new"])

        report = compare_records(old, new, KEYED_CONFIG)

        assert [(c.path, c.old, c.new) for c in report.changes] == [
            ("tags[0]", "new", "sale"),
            ("tags[1]", "sale", "new"),
        ]
        assert report.needs_notification


class TestNumericTolerance:
    def test_tiny_price_fluctuation_within_relative_tolerance(self):
        old = make_record([offer("A", 100.0)])
        new = make_record([offer("A", 100.5)])

        report = compare_records(old, new, KEYED_CONFIG)

        assert report.changes == []
        assert not report.needs_notification

    def test_price_change_beyond_relative_tolerance_is_reported(self):
        old = make_record([offer("A", 100.0)])
        new = make_record([offer("A", 103.0)])

        report = compare_records(old, new, KEYED_CONFIG)

        assert len(report.changes) == 1
        change = report.changes[0]
        assert change.path == "offers['A'].price"
        assert change.kind == "changed"
        assert change.old == 100.0
        assert change.new == 103.0
        assert report.needs_notification

    def test_absolute_tolerance(self):
        config = CompareConfig(
            key_fields={"offers": "sku"},
            tolerances={"price": Tolerance(absolute=0.5)},
        )
        old = make_record([offer("A", 100.0)])
        assert not compare_records(old, make_record([offer("A", 100.4)]), config).changed
        assert compare_records(old, make_record([offer("A", 100.6)]), config).changed

    def test_exact_match_required_without_tolerance(self):
        old = make_record([offer("A", 100.0)])
        new = make_record([offer("A", 100.0, stock=9)])

        report = compare_records(old, new, KEYED_CONFIG)

        assert [c.path for c in report.changes] == ["offers['A'].stock"]


class TestDuplicateKeys:
    def test_duplicate_keys_are_reported_and_left_unpaired(self):
        old = make_record([offer("A", 10.0), offer("A", 11.0), offer("B", 20.0)])
        new = make_record([offer("A", 10.0), offer("B", 20.0)])

        report = compare_records(old, new, KEYED_CONFIG)

        assert [c.kind for c in report.changes] == []
        assert len(report.issues) == 1
        issue = report.issues[0]
        assert issue.kind == "duplicate_key"
        assert issue.key == "A"
        assert issue.path == "offers"
        # "A" elements are not arbitrarily paired; "B" still pairs cleanly.
        assert [(p.key, p.old_index, p.new_index) for p in report.pairings] == [("B", 2, 1)]
        assert report.needs_notification

    def test_duplicate_keys_on_both_sides(self):
        old = make_record([offer("A", 10.0), offer("A", 11.0)])
        new = make_record([offer("A", 10.0), offer("A", 12.0)])

        report = compare_records(old, new, KEYED_CONFIG)

        assert [i.kind for i in report.issues] == ["duplicate_key", "duplicate_key"]
        assert report.pairings == []
        assert report.changes == []


class TestKeyedPairingOutcomes:
    def test_added_and_removed_keys(self):
        old = make_record([offer("A", 10.0), offer("B", 20.0)])
        new = make_record([offer("B", 20.0), offer("C", 30.0)])

        report = compare_records(old, new, KEYED_CONFIG)

        assert [(c.path, c.kind) for c in report.changes] == [
            ("offers['A']", "removed"),
            ("offers['C']", "added"),
        ]
        assert report.changes[0].old == offer("A", 10.0)
        assert report.changes[0].new is MISSING
        assert report.changes[1].old is MISSING
        assert report.changes[1].new == offer("C", 30.0)
        assert [(p.key, p.old_index, p.new_index) for p in report.pairings] == [("B", 1, 0)]

    def test_element_without_key_field_is_reported_not_paired(self):
        old = make_record([offer("A", 10.0), {"price": 5.0}])
        new = make_record([offer("A", 10.0), {"price": 5.0}])

        report = compare_records(old, new, KEYED_CONFIG)

        assert report.changes == []
        assert [i.kind for i in report.issues] == ["missing_key", "missing_key"]
        assert [(p.key, p.old_index, p.new_index) for p in report.pairings] == [("A", 0, 0)]

    def test_non_record_element_is_incomparable(self):
        old = make_record([offer("A", 10.0), "not-a-record"])
        new = make_record([offer("A", 10.0), "not-a-record"])

        report = compare_records(old, new, KEYED_CONFIG)

        assert report.changes == []
        assert [i.kind for i in report.issues] == ["incomparable_type"] * 2

    def test_callable_key_extractor(self):
        config = CompareConfig(key_fields={"offers": lambda el: el["sku"].lower()})
        old = make_record([offer("a", 10.0)])
        new = make_record([offer("A", 10.0)])

        report = compare_records(old, new, config)

        # Paired on the normalized key; the sku casing difference is still
        # reported as a field-level change.
        assert [p.key for p in report.pairings] == ["a"]
        assert [(c.path, c.old, c.new) for c in report.changes] == [
            ("offers['a'].sku", "a", "A"),
        ]


class TestDistinctStates:
    def test_missing_field_vs_none(self):
        old = make_record([offer("A", 10.0)])
        new = make_record([offer("A", 10.0)], subtitle=None)

        report = compare_records(old, new, KEYED_CONFIG)

        assert [(c.path, c.kind, c.old, c.new) for c in report.changes] == [
            ("subtitle", "added", MISSING, None),
        ]

    def test_none_vs_empty_collection(self):
        old = make_record(None, tags=None)
        new = make_record(None, tags=[])

        report = compare_records(old, new, KEYED_CONFIG)

        assert [(c.path, c.kind, c.old, c.new) for c in report.changes] == [
            ("tags", "changed", None, []),
        ]

    def test_missing_vs_empty_collection(self):
        old = make_record(None)
        new = make_record(None, tags=[])

        report = compare_records(old, new, KEYED_CONFIG)

        assert [(c.path, c.kind) for c in report.changes] == [("tags", "added")]

    def test_empty_collections_are_equal(self):
        old = make_record([], tags=[])
        new = make_record([], tags=[])

        report = compare_records(old, new, KEYED_CONFIG)

        assert report.changes == []
        assert report.issues == []
        assert not report.needs_notification


class TestIncomparableTypes:
    def test_collection_vs_scalar(self):
        old = make_record(None, tags=["a"])
        new = make_record(None, tags="a")

        report = compare_records(old, new, KEYED_CONFIG)

        assert report.changes == []
        assert [i.kind for i in report.issues] == ["incomparable_type"]
        assert report.issues[0].path == "tags"

    def test_record_vs_scalar(self):
        old = make_record(None, seller={"name": "x"})
        new = make_record(None, seller="x")

        report = compare_records(old, new, KEYED_CONFIG)

        assert [i.kind for i in report.issues] == ["incomparable_type"]

    def test_top_level_non_records(self):
        report = compare_records([1, 2], [1, 2], KEYED_CONFIG)

        assert [i.kind for i in report.issues] == ["incomparable_type"]


class TestPositionalComparison:
    def test_length_change(self):
        old = make_record(None, tags=["a", "b"])
        new = make_record(None, tags=["a"])

        report = compare_records(old, new, KEYED_CONFIG)

        assert [(c.path, c.kind, c.old) for c in report.changes] == [
            ("tags[1]", "removed", "b"),
        ]

    def test_nested_records_compared_field_wise(self):
        old = make_record(None, seller={"name": "x", "rating": 4.5})
        new = make_record(None, seller={"name": "x", "rating": 4.6})

        report = compare_records(old, new, KEYED_CONFIG)

        assert [(c.path, c.old, c.new) for c in report.changes] == [
            ("seller.rating", 4.5, 4.6),
        ]


class TestRecordsAreNotModified:
    def test_original_records_untouched(self):
        old = make_record([offer("A", 10.0), offer("B", 20.0)], tags=["x"])
        new = make_record([offer("B", 21.0)], tags=[])
        old_snapshot = copy.deepcopy(old)
        new_snapshot = copy.deepcopy(new)

        compare_records(old, new, KEYED_CONFIG)

        assert old == old_snapshot
        assert new == new_snapshot


class TestItemTypes:
    def test_dataclass_items(self):
        from tests import DataClassItem

        old = DataClassItem(name="a", value=1)
        new = DataClassItem(name="a", value=2)

        report = compare_records(old, new)

        assert [(c.path, c.old, c.new) for c in report.changes] == [("value", 1, 2)]

    def test_invalid_top_level_type_raises_nothing_but_reports(self):
        report = compare_records(1, 2)
        assert [i.kind for i in report.issues] == ["incomparable_type"]


@pytest.mark.parametrize(
    ("old_price", "new_price", "expected"),
    [
        (100.0, 100.0, False),
        (100.0, 101.0, False),  # exactly at the 1% boundary
        (100.0, 102.0, True),
        (0.0, 0.0, False),
        (0.0, 0.001, True),  # relative tolerance does not hide changes from zero
    ],
)
def test_relative_tolerance_boundaries(old_price, new_price, expected):
    old = make_record([offer("A", old_price)])
    new = make_record([offer("A", new_price)])

    report = compare_records(old, new, KEYED_CONFIG)

    assert report.changed is expected
