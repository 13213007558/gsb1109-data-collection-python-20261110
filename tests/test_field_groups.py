from __future__ import annotations

from typing import Any

import pytest
from parsel import Selector

from itemloaders import FieldGroupConflictError, ItemLoader
from itemloaders.processors import Identity, MapCompose, TakeFirst


def drop_empty(value: Any) -> Any:
    """A MapCompose function that filters empty strings and None out."""
    return None if value in (None, "") else value


class VariantLoader(ItemLoader):
    color_in = MapCompose(drop_empty, str.strip)
    stock_in = MapCompose(drop_empty, int)
    default_output_processor = Identity()


class TestFieldGroupCleaning:
    def test_records_are_cleaned_as_a_unit(self) -> None:
        loader = VariantLoader()
        loader.add_value(
            None,
            [
                {"color": " red ", "stock": "3"},
                {"color": "blue", "stock": "7"},
            ],
        )
        assert loader.get_collected_group_records() == [
            {"color": ["red"], "stock": [3]},
            {"color": ["blue"], "stock": [7]},
        ]
        assert loader.load_item() == {
            "color": ["red", "blue"],
            "stock": [3, 7],
        }

    def test_filtered_field_does_not_borrow_from_next_record(self) -> None:
        loader = VariantLoader()
        loader.add_value(
            None,
            [
                {"color": "red", "stock": "1"},
                {"color": "", "stock": "2"},
                {"color": "blue", "stock": "3"},
            ],
        )
        # The empty color is dropped inside the second record only; the record
        # itself survives with its stock, so color and stock stay aligned.
        assert loader.get_collected_group_records() == [
            {"color": ["red"], "stock": [1]},
            {"stock": [2]},
            {"color": ["blue"], "stock": [3]},
        ]
        # Comparing the full item, not merely the length of each column: the
        # stock of the color-less record must not shift onto another color.
        assert loader.load_item() == {
            "color": ["red", "blue"],
            "stock": [1, 2, 3],
        }

    def test_missing_field_is_never_backfilled(self) -> None:
        loader = VariantLoader()
        loader.add_value(
            None,
            [
                {"color": "red", "stock": "1"},
                {"stock": "2"},
                {"color": "blue", "stock": "3"},
            ],
        )
        assert loader.get_collected_group_records() == [
            {"color": ["red"], "stock": [1]},
            {"stock": [2]},
            {"color": ["blue"], "stock": [3]},
        ]
        assert loader.load_item() == {
            "color": ["red", "blue"],
            "stock": [1, 2, 3],
        }

    def test_fully_filtered_record_disappears_without_misalignment(self) -> None:
        loader = VariantLoader()
        loader.add_value(
            None,
            [
                {"color": "red", "stock": "1"},
                {"color": "", "stock": None},
                {"color": "blue", "stock": "3"},
            ],
        )
        assert loader.get_collected_group_records() == [
            {"color": ["red"], "stock": [1]},
            {"color": ["blue"], "stock": [3]},
        ]
        assert loader.load_item() == {
            "color": ["red", "blue"],
            "stock": [1, 3],
        }

    def test_single_mapping_is_one_record(self) -> None:
        loader = VariantLoader()
        loader.add_value(None, {"color": "red", "stock": "1"})
        assert loader.get_collected_group_records() == [
            {"color": ["red"], "stock": [1]},
        ]

    def test_one_to_many_transform_keeps_record_association(self) -> None:
        def split_colors(value: str) -> list[str]:
            return [part.strip() for part in value.split("/")]

        class MultiLoader(ItemLoader):
            color_in = MapCompose(split_colors)
            stock_in = MapCompose(int)
            default_output_processor = Identity()

        loader = MultiLoader()
        loader.add_value(
            None,
            [
                {"color": "red / crimson", "stock": "1"},
                {"color": "blue", "stock": "2"},
            ],
        )
        # The split produces several colors, but they stay inside the first
        # record, paired with stock 1. The association is visible on the
        # collected records even though the final scalar columns are flat.
        assert loader.get_collected_group_records() == [
            {"color": ["red", "crimson"], "stock": [1]},
            {"color": ["blue"], "stock": [2]},
        ]
        assert loader.load_item() == {
            "color": ["red", "crimson", "blue"],
            "stock": [1, 2],
        }

    def test_output_processors_run_on_aligned_fields(self) -> None:
        class TakeFirstLoader(VariantLoader):
            color_out = TakeFirst()
            stock_out = TakeFirst()

        loader = TakeFirstLoader()
        loader.add_value(
            None,
            [
                {"color": "red", "stock": "1"},
                {"color": "", "stock": "2"},
                {"color": "blue", "stock": "3"},
            ],
        )
        assert loader.load_item() == {"color": "red", "stock": 1}

    def test_grouped_handling_avoids_column_misalignment(self) -> None:
        # The bug being fixed: collecting each field separately lets the
        # filtered values desynchronize the two columns.
        separate = VariantLoader()
        separate.add_value("color", ["red", "", "blue"], MapCompose(drop_empty))
        separate.add_value("stock", ["1", "2", "3"], MapCompose(drop_empty, int))
        assert separate.load_item() == {
            "color": ["red", "blue"],
            "stock": [1, 2, 3],
        }

        grouped = VariantLoader()
        grouped.add_value(
            None,
            [
                {"color": "red", "stock": "1"},
                {"color": "", "stock": "2"},
                {"color": "blue", "stock": "3"},
            ],
        )
        item = grouped.load_item()
        # Same column lengths, but only the grouped version keeps the meaning
        # of each source row, observable through the cleaned records.
        assert item == separate.load_item()
        assert grouped.get_collected_group_records() == [
            {"color": ["red"], "stock": [1]},
            {"stock": [2]},
            {"color": ["blue"], "stock": [3]},
        ]


class TestFieldGroupConflicts:
    def test_single_field_then_group_is_rejected_by_default(self) -> None:
        loader = VariantLoader()
        loader.add_value("color", "red")
        with pytest.raises(FieldGroupConflictError, match="'color'"):
            loader.add_value(None, [{"color": "blue", "stock": 1}])
        # The rejected write must not leave partial data behind.
        assert loader.get_collected_group_records() == []
        assert loader.load_item() == {"color": ["red"]}

    def test_group_then_single_field_is_rejected_by_default(self) -> None:
        loader = VariantLoader()
        loader.add_value(None, [{"color": "red", "stock": 1}])
        with pytest.raises(FieldGroupConflictError, match="'color'"):
            loader.add_value("color", "blue")
        assert loader.load_item() == {"color": ["red"], "stock": [1]}

    def test_unknown_group_field_does_not_conflict(self) -> None:
        loader = VariantLoader()
        loader.add_value("name", "widget")
        loader.add_value(None, [{"color": "red", "stock": 1}])
        assert loader.load_item() == {
            "name": ["widget"],
            "color": ["red"],
            "stock": [1],
        }

    def test_merge_policy_combines_sources_in_write_order(self) -> None:
        loader = VariantLoader(group_conflict_policy="merge")
        loader.add_value(None, [{"color": "red", "stock": 1}])
        loader.add_value("color", "blue")
        loader.add_value(None, [{"color": "green", "stock": 2}])
        assert loader.get_collected_values("color") == [
            "red",
            "blue",
            "green",
        ]
        assert loader.load_item() == {
            "color": ["red", "blue", "green"],
            "stock": [1, 2],
        }

    def test_merge_policy_can_be_set_on_a_subclass(self) -> None:
        class MergingLoader(VariantLoader):
            group_conflict_policy = "merge"

        loader = MergingLoader()
        loader.add_value(None, [{"color": "red", "stock": 1}])
        loader.add_value("color", "blue")
        assert loader.load_item() == {
            "color": ["red", "blue"],
            "stock": [1],
        }

    def test_invalid_policy_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="group_conflict_policy"):
            ItemLoader(group_conflict_policy="overwrite")

    def test_nested_loader_inherits_policy(self) -> None:
        parent = VariantLoader(
            selector=Selector(text="<html><body></body></html>"),
            group_conflict_policy="merge",
        )
        nested = parent.nested_xpath("//body")
        assert nested.group_conflict_policy == "merge"

    def test_non_mapping_group_value_is_rejected(self) -> None:
        loader = ItemLoader()
        with pytest.raises(TypeError, match="Field-group values must be mappings"):
            loader.add_value(None, ["not-a-record"])
        with pytest.raises(TypeError, match="Field-group values must be mappings"):
            loader.add_value(None, "not-a-record")


class TestFieldGroupReplace:
    def test_replace_group_only_touches_its_fields(self) -> None:
        loader = VariantLoader()
        loader.add_value(
            None,
            [
                {"color": "red", "stock": 1, "sku": "a"},
                {"color": "blue", "stock": 2, "sku": "b"},
            ],
        )
        loader.replace_value(None, [{"color": "black", "stock": 9}])
        assert loader.get_collected_group_records() == [
            {"sku": ["a"]},
            {"sku": ["b"]},
            {"color": ["black"], "stock": [9]},
        ]
        assert loader.load_item() == {
            "color": ["black"],
            "stock": [9],
            "sku": ["a", "b"],
        }

    def test_replace_group_clears_single_field_values_in_merge_mode(self) -> None:
        loader = VariantLoader(group_conflict_policy="merge")
        loader.add_value("color", "red")
        loader.add_value(None, [{"color": "blue", "stock": 1}])
        loader.replace_value(None, [{"color": "green", "stock": 2}])
        assert loader.load_item() == {"color": ["green"], "stock": [2]}

    def test_replace_group_then_write_again(self) -> None:
        loader = VariantLoader()
        loader.add_value(None, [{"color": "red", "stock": 1}])
        loader.replace_value(None, [{"color": "blue", "stock": 2}])
        loader.add_value(None, [{"color": "green", "stock": 3}])
        assert loader.load_item() == {
            "color": ["blue", "green"],
            "stock": [2, 3],
        }


class TestFieldGroupSelectors:
    selector = Selector(
        text="""
        <html><body>
        <div class="variant"><span class="color">red</span><span class="stock">1</span></div>
        <div class="variant"><span class="color">blue</span><span class="stock">2</span></div>
        </body></html>
        """
    )

    def test_add_xpath_multi_fields_still_works(self) -> None:
        loader = ItemLoader(selector=self.selector)
        loader.add_xpath(
            None,
            "//span[@class='color']/text()",
            TakeFirst(),
            lambda text: {"color": text},
        )
        assert loader.get_collected_group_records() == [{"color": ["red"]}]
        assert loader.load_item() == {"color": ["red"]}

    def test_nested_loader_shares_group_records(self) -> None:
        loader = VariantLoader(selector=self.selector)
        nested = loader.nested_xpath("//body")
        nested.add_value(
            None,
            [
                {"color": "red", "stock": "1"},
                {"color": "blue", "stock": "2"},
            ],
        )
        assert loader.get_collected_group_records() == [
            {"color": ["red"], "stock": [1]},
            {"color": ["blue"], "stock": [2]},
        ]
        assert loader.load_item() == {
            "color": ["red", "blue"],
            "stock": [1, 2],
        }


class TestSingleFieldApiUnchanged:
    def test_single_field_values_are_unaffected(self) -> None:
        loader = ItemLoader()
        loader.add_value("name", "marta")
        loader.add_value("name", ["pepe", "jose"])
        assert loader.get_collected_values("name") == ["marta", "pepe", "jose"]
        assert loader.load_item() == {"name": ["marta", "pepe", "jose"]}

    def test_add_none_value_collects_nothing(self) -> None:
        loader = ItemLoader()
        loader.add_value("name", None)
        assert loader.get_collected_values("name") == []
        assert loader.load_item() == {}

    def test_group_records_empty_when_only_single_fields_used(self) -> None:
        loader = ItemLoader()
        loader.add_value("name", "marta")
        assert loader.get_collected_group_records() == []
        assert isinstance(loader.get_collected_group_records(), list)
