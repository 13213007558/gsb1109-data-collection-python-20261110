from __future__ import annotations

import copy
from dataclasses import dataclass, field

import pytest

from itemadapter import Delete, ItemAdapter, PatchError, Set
from tests import requires_attrs

try:
    import attrs
except ImportError:
    attrs = None


@dataclass
class DataClassVariant:
    sku: str
    price: float


@dataclass
class DataClassRecord:
    sku: str = "A-1"
    price: float = 100.0
    tags: list = field(default_factory=lambda: ["new", "old", "promo"])
    stock: int | None = 42
    variant: DataClassVariant = field(
        default_factory=lambda: DataClassVariant(sku="A-1-RED", price=90.0)
    )
    extra: dict = field(default_factory=lambda: {"color": "red"})


@dataclass(frozen=True)
class FrozenDataClassRecord:
    price: float = 100.0
    stock: int = 42


if attrs is not None:

    @attrs.define
    class AttrsVariant:
        sku: str
        price: float

    @attrs.define
    class AttrsRecord:
        sku: str = "A-1"
        price: float = 100.0
        tags: list = attrs.field(factory=lambda: ["new", "old", "promo"])
        stock: int | None = 42
        variant: AttrsVariant = attrs.field(
            factory=lambda: AttrsVariant(sku="A-1-RED", price=90.0)
        )
        extra: dict = attrs.field(factory=lambda: {"color": "red"})

    @attrs.frozen
    class FrozenAttrsRecord:
        price: float = 100.0
        stock: int = 42


def make_dict_record():
    return {
        "sku": "A-1",
        "price": 100.0,
        "tags": ["new", "old", "promo"],
        "stock": 42,
        "variant": {"sku": "A-1-RED", "price": 90.0},
        "extra": {"color": "red"},
    }


class TestThreeSemantics:
    """One patch that changes the price, deletes an old tag and blanks the
    stock, showing the three meanings that must not be conflated:
    "not scraped" (no operation, field untouched), "explicitly blank"
    (Set to None) and "delete the old value" (Delete)."""

    def check_record(self, item):
        adapter = ItemAdapter(item)
        adapter.patch(
            [
                Set(("price",), 80.0),  # scraped: update the price
                Delete(("tags", 1)),  # stale: delete the old tag "old"
                Set(("stock",), None),  # sold out: explicitly blank the stock
                # "sku" is not in the patch: not scraped, left untouched
            ]
        )
        assert adapter["sku"] == "A-1"
        assert adapter["price"] == 80.0
        assert adapter["tags"] == ["new", "promo"]
        assert adapter["stock"] is None
        assert "stock" in adapter  # blanked, not deleted

    def test_dict(self):
        item = make_dict_record()
        self.check_record(item)
        assert set(item) == {"sku", "price", "tags", "stock", "variant", "extra"}

    def test_dataclass(self):
        self.check_record(DataClassRecord())

    @requires_attrs
    def test_attrs(self):
        self.check_record(AttrsRecord())

    def test_none_vs_delete(self):
        item = {"stock": 42, "warehouse": "W1"}
        ItemAdapter(item).patch([Set(("stock",), None), Delete(("warehouse",))])
        assert item == {"stock": None}  # blanked key stays, deleted key is gone


class TestNestedItems:
    def test_nested_dict_and_list_paths(self):
        item = make_dict_record()
        ItemAdapter(item).patch(
            [
                Set(("variant", "price"), 70.0),
                Set(("extra", "color"), "blue"),
                Set(("tags", 0), "fresh"),
            ]
        )
        assert item["variant"] == {"sku": "A-1-RED", "price": 70.0}
        assert item["extra"] == {"color": "blue"}
        assert item["tags"] == ["fresh", "old", "promo"]

    def test_nested_dataclass(self):
        item = DataClassRecord()
        variant = item.variant
        ItemAdapter(item).patch(
            [
                Set(("variant", "price"), 70.0),
                Delete(("extra", "color")),
                Set(("stock",), None),
            ]
        )
        assert item.variant.price == 70.0
        assert item.extra == {}
        assert item.stock is None
        assert item.variant is variant  # untouched object identity preserved
        assert isinstance(item.variant, DataClassVariant)  # type preserved

    @requires_attrs
    def test_nested_attrs(self):
        item = AttrsRecord()
        ItemAdapter(item).patch(
            [
                Set(("variant", "price"), 70.0),
                Delete(("extra", "color")),
            ]
        )
        assert item.variant.price == 70.0
        assert item.extra == {}
        assert isinstance(item.variant, AttrsVariant)

    def test_dataclass_field_delete(self):
        item = DataClassRecord()
        adapter = ItemAdapter(item)
        adapter.patch([Delete(("extra",))])
        assert "extra" not in adapter
        assert adapter["sku"] == "A-1"

    @requires_attrs
    def test_attrs_field_delete(self):
        item = AttrsRecord()
        adapter = ItemAdapter(item)
        adapter.patch([Delete(("stock",))])
        assert "stock" not in adapter

    def test_list_of_items(self):
        item = {"variants": [{"sku": "A", "price": 1.0}, {"sku": "B", "price": 2.0}]}
        ItemAdapter(item).patch([Set(("variants", 1, "price"), 3.0)])
        assert item["variants"][1]["price"] == 3.0
        assert item["variants"][0] == {"sku": "A", "price": 1.0}


class TestListDeleteBaseline:
    """All list indices in a batch refer to the same pre-modification
    snapshot, regardless of operation order."""

    def test_multiple_deletes_same_list(self):
        item = {"tags": ["a", "b", "c", "d"]}
        ItemAdapter(item).patch([Delete(("tags", 0)), Delete(("tags", 2))])
        assert item["tags"] == ["b", "d"]

    def test_delete_order_does_not_matter(self):
        item = {"tags": ["a", "b", "c", "d"]}
        ItemAdapter(item).patch([Delete(("tags", 2)), Delete(("tags", 0))])
        assert item["tags"] == ["b", "d"]

    def test_negative_indices(self):
        item = {"tags": ["a", "b", "c"]}
        ItemAdapter(item).patch([Delete(("tags", -1))])
        assert item["tags"] == ["a", "b"]

    def test_set_and_delete_same_list(self):
        item = {"tags": ["a", "b", "c"]}
        ItemAdapter(item).patch([Set(("tags", 2), "z"), Delete(("tags", 0))])
        assert item["tags"] == ["b", "z"]  # the set used the baseline index

    def test_nested_lists(self):
        item = {"rows": [["a", "b"], ["c", "d", "e"]]}
        ItemAdapter(item).patch([Delete(("rows", 1, 0)), Delete(("rows", 1, 2))])
        assert item["rows"] == [["a", "b"], ["d"]]


class TestAtomicFailures:
    def check_untouched(self, item, operations):
        before = copy.deepcopy(item)
        with pytest.raises(PatchError):
            ItemAdapter(item).patch(operations)
        assert item == before

    def test_missing_path(self):
        self.check_untouched(
            make_dict_record(),
            [Set(("price",), 80.0), Set(("variant", "weight", "kg"), 1)],
        )

    def test_missing_top_level_key(self):
        self.check_untouched(make_dict_record(), [Delete(("unknown",))])

    def test_index_out_of_range(self):
        self.check_untouched(
            make_dict_record(),
            [Set(("price",), 80.0), Delete(("tags", 10))],
        )

    def test_negative_index_out_of_range(self):
        self.check_untouched(make_dict_record(), [Delete(("tags", -4))])

    def test_string_index_into_list(self):
        self.check_untouched(make_dict_record(), [Set(("tags", "0"), "x")])

    def test_descend_into_scalar(self):
        self.check_untouched(make_dict_record(), [Set(("price", "amount"), 1)])

    def test_readonly_tuple(self):
        self.check_untouched({"sizes": ("S", "M")}, [Set(("sizes", 0), "L")])
        self.check_untouched({"sizes": ("S", "M")}, [Delete(("sizes", 0))])

    def test_readonly_frozen_dataclass(self):
        record = FrozenDataClassRecord()
        self.check_untouched(record, [Set(("price",), 80.0)])
        self.check_untouched(record, [Delete(("stock",))])

    @requires_attrs
    def test_readonly_frozen_attrs(self):
        record = FrozenAttrsRecord()
        self.check_untouched(record, [Set(("price",), 80.0)])
        self.check_untouched(record, [Delete(("stock",))])

    def test_undeclared_dataclass_field(self):
        self.check_untouched(DataClassRecord(), [Set(("unknown",), 1)])
        self.check_untouched(DataClassRecord(), [Delete(("unknown",))])

    @requires_attrs
    def test_undeclared_attrs_field(self):
        self.check_untouched(AttrsRecord(), [Set(("unknown",), 1)])

    def test_unset_dataclass_field_delete(self):
        item = DataClassRecord()
        del item.extra
        adapter = ItemAdapter(item)
        with pytest.raises(PatchError):
            adapter.patch([Delete(("extra",)), Set(("price",), 80.0)])
        assert "extra" not in adapter
        assert item.price == 100.0

    def test_duplicate_delete(self):
        self.check_untouched(
            {"tags": ["a", "b"]},
            [Delete(("tags", 0)), Delete(("tags", 0))],
        )

    def test_no_half_update(self):
        """A failing batch must not commit its valid operations either."""
        item = make_dict_record()
        with pytest.raises(PatchError):
            ItemAdapter(item).patch(
                [
                    Set(("price",), 80.0),
                    Delete(("tags", 1)),
                    Set(("stock",), None),
                    Set(("does", "not", "exist"), 1),
                ]
            )
        assert item == make_dict_record()

    def test_no_half_update_dataclass(self):
        item = DataClassRecord()
        before = copy.deepcopy(item)
        with pytest.raises(PatchError):
            ItemAdapter(item).patch(
                [
                    Set(("price",), 80.0),
                    Delete(("tags", 0)),
                    Set(("unknown_field",), 1),
                ]
            )
        assert item == before
        assert item.price == 100.0
        assert item.tags == ["new", "old", "promo"]

    def test_invalid_operation(self):
        self.check_untouched(make_dict_record(), [("price", 80.0)])

    def test_empty_path(self):
        self.check_untouched(make_dict_record(), [Set((), 1)])


class TestDictSpecifics:
    def test_set_new_dict_key(self):
        item = make_dict_record()
        ItemAdapter(item).patch([Set(("currency",), "USD")])
        assert item["currency"] == "USD"

    def test_empty_operations(self):
        item = make_dict_record()
        ItemAdapter(item).patch([])
        assert item == make_dict_record()
