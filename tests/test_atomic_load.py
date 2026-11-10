from __future__ import annotations

import pytest

from itemloaders import ItemLoader
from itemloaders.processors import Compose, TakeFirst


def reject_bad(values):
    value = values[0]
    if value == "bad":
        raise ValueError("invalid value")
    return value


class ProductLoader(ItemLoader):
    default_output_processor = Compose(TakeFirst())
    sku_out = Compose(reject_bad)


class TestAtomicLoad:
    def test_failed_last_field_rolls_back_then_retry_succeeds(self):
        records = []

        # A first, fully valid business record loads fine.
        loader = ProductLoader()
        loader.add_value("name", "Color TV")
        loader.add_value("sku", "tv-1")
        records.append(loader.load_item(atomic=True))

        # Second record: the last field fails its output processor.
        item = {"name": "original", "sku": "original-sku"}
        loader = ProductLoader(item=item)
        loader.replace_value("name", "Radio")
        loader.replace_value("sku", "bad")
        with pytest.raises(ValueError, match="Error with output processor"):
            loader.load_item(atomic=True)

        # Rollback: the item keeps its original data, no partial writes.
        assert item == {"name": "original", "sku": "original-sku"}
        # Collected raw values are kept for correction and retry.
        assert loader.get_collected_values("name") == ["Radio"]
        assert loader.get_collected_values("sku") == ["bad"]

        # Fix the offending input and retry the same loader.
        loader.replace_value("sku", "radio-1")
        item = loader.load_item(atomic=True)
        assert item == {"name": "Radio", "sku": "radio-1"}
        records.append(item)

        # Previously loaded business records are not polluted.
        assert records[0] == {"name": "Color TV", "sku": "tv-1"}
        assert records == [
            {"name": "Color TV", "sku": "tv-1"},
            {"name": "Radio", "sku": "radio-1"},
        ]

    def test_non_atomic_load_still_writes_partially(self):
        item: dict = {}
        loader = ProductLoader(item=item)
        loader.add_value("name", "Radio")
        loader.add_value("sku", "bad")
        with pytest.raises(ValueError, match="Error with output processor"):
            loader.load_item()
        # Default (non-atomic) mode keeps the historical partial-write behavior.
        assert item == {"name": "Radio"}

    def test_output_processors_run_exactly_once(self):
        calls = []

        def counting_processor(values):
            calls.append(values[0])
            return values[0]

        class CountingLoader(ItemLoader):
            default_output_processor = Compose(TakeFirst())
            name_out = Compose(counting_processor)
            sku_out = Compose(counting_processor)

        loader = CountingLoader()
        loader.add_value("name", "Color TV")
        loader.add_value("sku", "tv-1")
        assert loader.load_item(atomic=True) == {"name": "Color TV", "sku": "tv-1"}
        # No extra "pre-check" pass: side effects happen exactly once per field.
        assert calls == ["Color TV", "tv-1"]

    def test_nested_loader_failure_scope(self):
        from parsel import Selector

        selector = Selector(text="<html><body><p>Radio</p></body></html>")
        item: dict = {}
        loader = ProductLoader(item=item, selector=selector)
        loader.add_value("sku", "bad")
        nested = loader.nested_xpath("//body")
        nested.add_xpath("name", "p/text()")

        # The nested loader shares collected values with its parent, so a
        # failure in any field of the family aborts the whole atomic load.
        with pytest.raises(ValueError, match="Error with output processor"):
            nested.load_item(atomic=True)
        assert item == {}
        assert nested.get_collected_values("name") == ["Radio"]
        assert loader.get_collected_values("sku") == ["bad"]

        loader.replace_value("sku", "radio-1")
        assert loader.load_item(atomic=True) == {"name": "Radio", "sku": "radio-1"}
