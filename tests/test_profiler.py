from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

import pytest

from itemadapter import FieldStats, ItemAdapter, StructureProfiler
from tests import AttrsItem, requires_attrs


def make_batch():
    return [
        {"name": "a", "author": {"name": "x"}, "tags": ["t1", "t2"]},
        {"name": None, "author": None, "tags": []},
        {"name": "c"},
    ]


class TestBasicCounts:
    def test_docstring_example(self):
        profiler = StructureProfiler.profile(make_batch())
        stats = profiler.stats()
        assert profiler.item_count == 3

        name = stats["name"]
        assert name.observations == 3
        assert name.valid == 2
        assert name.none == 1
        assert name.null_count == 1

        author = stats["author"]
        assert author.observations == 2
        assert author.valid == 1
        assert author.none == 1

    def test_nested_denominator_not_double_counted(self):
        """Nulls of a parent are counted once at the parent level; nested
        paths are conditional on the parent object existing."""
        profiler = StructureProfiler.profile(make_batch())
        author_name = profiler.stats()["author.name"]
        assert author_name.observations == 1
        assert author_name.valid == 1
        assert author_name.none == 0
        assert author_name.missing == 0

    def test_empty_collection_and_string(self):
        profiler = StructureProfiler.profile(
            [
                {"tags": [], "meta": {}, "note": ""},
                {"tags": ["a"], "meta": {"k": 1}, "note": "x"},
            ]
        )
        stats = profiler.stats()
        assert stats["tags"].empty == 1
        assert stats["tags"].valid == 1
        assert stats["meta"].empty == 1
        assert stats["meta"].valid == 1
        # empty strings are valid values, not empty collections
        assert stats["note"].valid == 2
        assert stats["note"].empty == 0

    def test_types_tracked(self):
        profiler = StructureProfiler.profile(make_batch())
        stats = profiler.stats()
        assert stats["name"].types == {"str", "NoneType"}
        assert stats["author"].types == {"dict", "NoneType"}
        assert stats["tags"].types == {"list"}
        assert stats["tags"].element_types == {"str"}

    def test_invalid_item(self):
        profiler = StructureProfiler()
        with pytest.raises(TypeError, match="No adapter found"):
            profiler.add(1234)


class TestDeclaredFields:
    def test_declared_but_unset_counts_missing_without_touching_defaults(self):
        def boom():
            raise AssertionError("default factory must not be called")

        @dataclass(init=False)
        class Unset:
            name: str = field(default_factory=boom)
            value: int = field(default_factory=boom)

        profiler = StructureProfiler.profile([Unset(), Unset()])
        stats = profiler.stats()
        assert stats["name"].observations == 2
        assert stats["name"].missing == 2
        assert stats["value"].missing == 2
        assert stats["name"].valid == 0

    def test_partially_set_dataclass(self):
        @dataclass(init=False)
        class Partial:
            name: str
            value: int

        item = Partial()
        item.name = "a"  # value remains declared but not assigned
        profiler = StructureProfiler.profile([item])
        stats = profiler.stats()
        assert stats["name"].valid == 1
        assert stats["value"].observations == 1
        assert stats["value"].missing == 1

    def test_undeclared_only_with_declared_schema(self):
        class Movie(TypedDict):
            name: str

        profiler = StructureProfiler()
        # TypedDict instances are plain dicts at run time, so the declared
        # schema must be passed explicitly
        profiler.add(Movie(name="a", extra=1), item_class=Movie)  # type: ignore[typeddict-unknown-key]
        stats = profiler.stats()
        assert stats["extra"].undeclared == 1
        assert stats["extra"].valid == 1
        assert stats["name"].undeclared == 0

    def test_plain_dict_keys_are_never_undeclared(self):
        profiler = StructureProfiler.profile([{"anything": 1}])
        assert profiler.stats()["anything"].undeclared == 0

    @requires_attrs
    def test_mixed_item_types_share_paths(self):
        @dataclass
        class DataClassMovie:
            name: str | None = None

        profiler = StructureProfiler.profile(
            [
                {"name": "dict"},
                DataClassMovie(name=None),
                AttrsItem(name="attrs"),
            ]
        )
        name = profiler.stats()["name"]
        assert name.observations == 3
        assert name.valid == 2
        assert name.none == 1
        assert name.types == {"str", "NoneType"}


class TestLists:
    def test_list_length_difference_is_not_missing(self):
        profiler = StructureProfiler.profile(
            [
                {"variants": [{"sku": "a", "price": 1}, {"sku": "b", "price": 2}]},
                {"variants": [{"sku": "c"}]},
            ]
        )
        stats = profiler.stats()
        sku = stats["variants.sku"]
        # the denominator is the number of list elements, not of parent items
        assert sku.observations == 3
        assert sku.valid == 3
        assert sku.missing == 0
        price = stats["variants.price"]
        # dicts declare no fields: absence lowers observations, not missing
        assert price.observations == 2
        assert price.missing == 0
        assert price.valid == 2

    def test_declared_nested_items_in_list(self):
        @dataclass
        class Variant:
            sku: str | None = None
            price: int | None = None

        @dataclass
        class Product:
            variants: list = field(default_factory=list)

        profiler = StructureProfiler.profile(
            [
                Product(variants=[Variant(sku="a", price=1), Variant(sku="b")]),
                Product(variants=[Variant(sku="c", price=None)]),
                Product(),
            ]
        )
        stats = profiler.stats()
        assert stats["variants"].observations == 3
        assert stats["variants"].empty == 1
        assert stats["variants.sku"].observations == 3
        assert stats["variants.sku"].valid == 3
        price = stats["variants.price"]
        assert price.observations == 3
        assert price.valid == 1
        assert price.none == 2
        assert price.missing == 0


class TestMerge:
    def test_merge_equals_full_run(self):
        items = [
            {"name": "a", "author": {"name": "x"}, "tags": ["t"]},
            {"name": None, "author": None, "tags": []},
            {"name": "c", "extra": 1},
            AttrsItem(name="attrs") if AttrsItem is not None else {"name": "d"},
        ]
        full = StructureProfiler.profile(items)
        first_half = StructureProfiler.profile(items[:2])
        second_half = StructureProfiler.profile(items[2:])
        merged = first_half.merge(second_half)
        assert merged.item_count == full.item_count == 4
        assert merged.stats() == full.stats()

    def test_merge_is_commutative_and_associative(self):
        batches = [
            [{"name": "a"}],
            [{"name": None, "tags": []}],
            [{"name": "c", "tags": ["x", "y"]}],
        ]
        profilers = [StructureProfiler.profile(batch) for batch in batches]
        full = StructureProfiler.profile(item for batch in batches for item in batch)
        left = profilers[0].merge(profilers[1]).merge(profilers[2])
        right = profilers[0].merge(profilers[1].merge(profilers[2]))
        assert left.stats() == right.stats() == full.stats()
        assert profilers[0].merge(profilers[1]).stats() == profilers[1].merge(profilers[0]).stats()

    def test_add_operators(self):
        first = StructureProfiler.profile([{"name": "a"}])
        second = StructureProfiler.profile([{"name": None}])
        combined = first + second
        assert combined.item_count == 2
        assert combined.stats()["name"].none == 1
        first += second
        assert first.item_count == 2
        assert first.stats() == combined.stats()

    def test_merge_rejects_other_types(self):
        with pytest.raises(TypeError, match="Cannot merge"):
            StructureProfiler().merge(FieldStats())

    def test_merge_does_not_mutate_sources(self):
        first = StructureProfiler.profile([{"name": "a"}])
        second = StructureProfiler.profile([{"name": None}])
        first.merge(second)
        assert first.stats()["name"].none == 0
        assert first.item_count == 1


class TestReport:
    def test_report_shape_and_rates(self):
        report = StructureProfiler.profile(make_batch()).report()
        assert report["item_count"] == 3
        name = report["paths"]["name"]
        assert name["observations"] == 3
        assert name["none"] == 1
        assert name["none_rate"] == pytest.approx(1 / 3)
        assert name["valid_rate"] == pytest.approx(2 / 3)
        assert name["null_rate"] == pytest.approx(1 / 3)
        assert name["types"] == ["NoneType", "str"]
        author_name = report["paths"]["author.name"]
        assert author_name["observations"] == 1
        assert author_name["null_rate"] == 0.0

    def test_observations_invariant(self):
        profiler = StructureProfiler()
        profiler.update(make_batch())
        if AttrsItem is not None:
            profiler.add(AttrsItem(name="x", value=None))
        for path_stats in profiler.stats().values():
            assert path_stats.observations == (
                path_stats.missing + path_stats.none + path_stats.empty + path_stats.valid
            )

    def test_adapter_still_works(self):
        adapter = ItemAdapter({"name": "a"})
        assert adapter["name"] == "a"
