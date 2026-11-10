import pytest

from itemloaders import BatchLoadError, BatchRecord, ItemLoader, load_items
from itemloaders.processors import MapCompose, TakeFirst


def parse_localized_price(value, loader_context):
    lang = loader_context.get("lang", "en")
    if lang == "de":
        value = value.replace(".", "").replace(",", ".")
    return float(value)


def remember_context(value, loader_context):
    loader_context["seen_by_next_record"] = True
    return value


def read_context_flag(value, loader_context):
    if loader_context.get("seen_by_next_record"):
        raise ValueError("context leaked from a previous record")
    return value


class ProductLoader(ItemLoader):
    default_output_processor = TakeFirst()
    price_in = MapCompose(parse_localized_price)


class TestLoadItems:
    def test_success_and_failure_results_keep_input_index(self):
        records = [
            {"name": "tv", "price": "1299.99"},
            {"name": "broken", "price": "not-a-price"},
            {"name": "radio", "price": "19.99"},
        ]
        results = list(load_items(records, ProductLoader))

        assert [result.index for result in results] == [0, 1, 2]
        assert [result.ok for result in results] == [True, False, True]

        assert results[0].item == {"name": "tv", "price": 1299.99}
        assert results[2].item == {"name": "radio", "price": 19.99}

        failure = results[1]
        assert failure.item is None
        assert len(failure.errors) == 1
        error = failure.errors[0]
        assert error.field_name == "price"
        assert "not-a-price" in error.message
        assert isinstance(error.exception, Exception)

    def test_fail_fast_raises_without_consuming_more_input(self):
        consumed = []

        def records():
            for fields in [
                {"price": "1.0"},
                {"price": "broken"},
                {"price": "3.0"},
            ]:
                consumed.append(fields)
                yield fields

        with pytest.raises(BatchLoadError) as excinfo:
            list(load_items(records(), ProductLoader, fail_fast=True))

        assert excinfo.value.index == 1
        assert [error.field_name for error in excinfo.value.errors] == ["price"]
        assert len(consumed) == 2

    def test_one_shot_generator_is_not_read_after_consumer_stops(self):
        consumed = []

        def records():
            for number in range(10000):
                consumed.append(number)
                yield {"price": f"{number}.0"}

        results = load_items(records(), ProductLoader)
        first = next(results)
        assert first.ok
        assert first.item == {"price": 0.0}
        results.close()
        assert len(consumed) == 1

    def test_collected_values_are_isolated_between_records(self):
        records = [
            {"name": "tv", "price": "1.0"},
            {"price": "2.0"},
        ]
        results = list(load_items(records, ProductLoader))
        assert results[0].item == {"name": "tv", "price": 1.0}
        assert results[1].item == {"price": 2.0}

    def test_loader_context_is_isolated_between_records(self):
        class LeakyLoader(ItemLoader):
            default_output_processor = TakeFirst()
            name_in = MapCompose(remember_context)
            sku_in = MapCompose(read_context_flag)

        records = [
            {"name": "tv"},
            {"sku": "abc"},
        ]
        results = list(load_items(records, LeakyLoader))
        assert results[0].ok
        assert results[1].ok
        assert results[1].item == {"sku": "abc"}

    def test_adjacent_records_with_different_languages(self):
        records = [
            BatchRecord({"price": "1.299,99"}, context={"lang": "de"}),
            BatchRecord({"price": "kaputt"}, context={"lang": "de"}),
            BatchRecord({"price": "1299.99"}, context={"lang": "en"}),
            {"price": "5.50"},
        ]
        results = list(load_items(records, ProductLoader))

        assert [result.ok for result in results] == [True, False, True, True]
        assert results[0].item == {"price": 1299.99}
        assert results[1].errors[0].field_name == "price"
        # The failing German record must not change how the next record
        # is cleaned, and the German context must not leak into the
        # record that relies on the shared default language.
        assert results[2].item == {"price": 1299.99}
        assert results[3].item == {"price": 5.50}

    def test_shared_context_is_merged_under_record_context(self):
        records = [
            {"price": "1.0"},
            BatchRecord({"price": "2,5"}, context={"lang": "de"}),
        ]
        results = list(load_items(records, ProductLoader, lang="en"))
        assert results[0].item == {"price": 1.0}
        assert results[1].item == {"price": 2.5}

    def test_output_processor_failure_is_reported(self):
        class FailingOutputLoader(ItemLoader):
            def name_out(self, values):
                raise ValueError("cannot join")

        results = list(load_items([{"name": "tv"}], FailingOutputLoader))
        assert not results[0].ok
        assert results[0].errors[0].field_name is None
        assert "cannot join" in results[0].errors[0].message

    def test_default_loader_class(self):
        results = list(load_items([{"name": "tv"}]))
        assert results[0].ok
        assert results[0].item == {"name": ["tv"]}
