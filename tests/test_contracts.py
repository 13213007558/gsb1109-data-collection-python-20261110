from __future__ import annotations

import dataclasses

import pytest
from parsel import Selector

from itemloaders import ItemLoader
from itemloaders.contracts import (
    ProcessorContract,
    ProcessorContractError,
    Shape,
    get_processor_contract,
    processor_contract,
)
from itemloaders.processors import (
    Compose,
    Identity,
    Join,
    MapCompose,
    SelectJmes,
    TakeFirst,
)


@processor_contract(accepts=Shape.SINGLE, returns=Shape.SINGLE)
def clean(value):
    return value.strip()


@processor_contract(accepts=Shape.SEQUENCE, returns=Shape.SEQUENCE)
def dedupe(values):
    return list(dict.fromkeys(values))


@processor_contract(
    accepts=Shape.SINGLE, returns=Shape.SINGLE, requires_context={"currency"}
)
def convert_currency(value, loader_context=None):
    return f"{value} {loader_context['currency']}"


def legacy_processor(values):
    return [str(v).strip() for v in values]


class TestBuiltinContracts:
    def test_take_first(self):
        contract = get_processor_contract(TakeFirst())
        assert contract is not None
        assert contract.accepts is Shape.SEQUENCE
        assert contract.returns is Shape.SINGLE

    def test_join(self):
        contract = get_processor_contract(Join())
        assert contract is not None
        assert contract.accepts is Shape.SEQUENCE
        assert contract.returns is Shape.SINGLE

    def test_identity(self):
        contract = get_processor_contract(Identity())
        assert contract is not None
        assert contract.accepts is Shape.ANY
        assert contract.returns is Shape.ANY

    def test_map_compose_derived(self):
        contract = get_processor_contract(MapCompose(clean))
        assert contract is not None
        assert contract.accepts is Shape.ANY
        assert contract.returns is Shape.SEQUENCE
        assert contract.requires_context == frozenset()

    def test_compose_derived(self):
        contract = get_processor_contract(Compose(TakeFirst(), clean))
        assert contract is not None
        assert contract.accepts is Shape.SEQUENCE
        assert contract.returns is Shape.SINGLE

    def test_compose_derived_context_requirements(self):
        contract = get_processor_contract(Compose(TakeFirst(), convert_currency))
        assert contract is not None
        assert contract.requires_context == frozenset({"currency"})

    def test_unknown_processor(self):
        assert get_processor_contract(legacy_processor) is None
        assert get_processor_contract(str.upper) is None


class TestLoaderValidation:
    def test_compatible_chain_loads_normally(self):
        class MyLoader(ItemLoader):
            name_in = MapCompose(clean)
            name_out = TakeFirst()

        loader = MyLoader()
        loader.add_value("name", [" foo ", "bar"])
        assert loader.load_item() == {"name": "foo"}

    def test_default_processors_pass(self):
        report = ItemLoader().validate_processor_contracts()
        assert report.ok
        assert report.unknown == []

    def test_output_processor_expecting_single_value(self):
        class MyLoader(ItemLoader):
            default_output_processor = SelectJmes("foo")

        with pytest.raises(ProcessorContractError) as excinfo:
            MyLoader()
        message = str(excinfo.value)
        assert "default processors" in message
        assert "collected values" in message
        assert "output processor (SelectJmes)" in message

    def test_input_processor_expecting_single_value(self):
        class MyLoader(ItemLoader):
            default_input_processor = SelectJmes("foo")

        with pytest.raises(ProcessorContractError) as excinfo:
            MyLoader()
        message = str(excinfo.value)
        assert "extracted values" in message
        assert "input processor (SelectJmes)" in message

    def test_compose_internal_mismatch(self):
        class MyLoader(ItemLoader):
            name_out = Compose(TakeFirst(), Join())

        with pytest.raises(ProcessorContractError) as excinfo:
            MyLoader()
        message = str(excinfo.value)
        assert "field 'name'" in message
        assert "stage 1 (TakeFirst)" in message
        assert "stage 2 (Join)" in message

    def test_map_compose_inner_function_expecting_sequence(self):
        class MyLoader(ItemLoader):
            name_in = MapCompose(clean, dedupe)

        with pytest.raises(ProcessorContractError) as excinfo:
            MyLoader()
        message = str(excinfo.value)
        assert "field 'name'" in message
        assert "stage 2 (dedupe)" in message
        assert "single values" in message

    def test_missing_required_context(self):
        class MyLoader(ItemLoader):
            name_in = MapCompose(convert_currency)

        with pytest.raises(ProcessorContractError) as excinfo:
            MyLoader()
        message = str(excinfo.value)
        assert "convert_currency" in message
        assert "'currency'" in message

    def test_required_context_provided(self):
        class MyLoader(ItemLoader):
            name_in = MapCompose(convert_currency)
            name_out = TakeFirst()

        loader = MyLoader(currency="EUR")
        loader.add_value("name", "10")
        assert loader.load_item() == {"name": "10 EUR"}

    def test_field_level_override_checked_against_default(self):
        class MyLoader(ItemLoader):
            default_output_processor = Identity()
            name_out = SelectJmes("foo")

        with pytest.raises(ProcessorContractError) as excinfo:
            MyLoader()
        message = str(excinfo.value)
        assert "field 'name'" in message
        assert "default processors" not in message

    def test_field_meta_processors_checked(self):
        @dataclasses.dataclass
        class Product:
            name: str = dataclasses.field(
                default="", metadata={"output_processor": SelectJmes("foo")}
            )

        with pytest.raises(ProcessorContractError) as excinfo:
            ItemLoader(item=Product())
        assert "field 'name'" in str(excinfo.value)

    def test_unknown_processor_still_usable(self):
        class MyLoader(ItemLoader):
            name_in = legacy_processor

        loader = MyLoader()
        report = loader.validate_processor_contracts()
        assert report.ok
        assert any("legacy_processor" in label for label in report.unknown)
        loader.add_value("name", " foo ")
        assert loader.load_item() == {"name": ["foo"]}

    def test_unknown_inner_function_mixed_with_declared(self):
        class MyLoader(ItemLoader):
            name_in = MapCompose(str.strip, clean)
            name_out = Compose(TakeFirst(), str.upper)

        loader = MyLoader()
        report = loader.validate_processor_contracts()
        assert report.ok
        loader.add_value("name", " foo ")
        assert loader.load_item() == {"name": "FOO"}

    def test_validation_does_not_call_processors(self):
        calls = []

        @processor_contract(accepts=Shape.SEQUENCE, returns=Shape.SEQUENCE)
        def spy(values):
            calls.append(values)
            return values

        class MyLoader(ItemLoader):
            default_input_processor = spy
            default_output_processor = spy

        MyLoader()
        assert calls == []

    def test_nested_loader_validates(self):
        class MyLoader(ItemLoader):
            name_out = TakeFirst()

        loader = MyLoader(selector=Selector(text="<p>x</p>"))
        nested = loader.nested_css("p")
        assert nested.validate_processor_contracts().ok
        nested.add_css("name", "p::text")
        assert loader.load_item() == {"name": "x"}


class TestProcessorContractDecorator:
    def test_decorator_on_function(self):
        contract = get_processor_contract(clean)
        assert contract is not None
        assert contract.accepts is Shape.SINGLE
        assert contract.returns is Shape.SINGLE

    def test_direct_attribute_assignment(self):
        class MyProcessor:
            __processor_contract__ = ProcessorContract(
                accepts=Shape.SEQUENCE, returns=Shape.SINGLE
            )

            def __call__(self, values):
                return values[0]

        contract = get_processor_contract(MyProcessor())
        assert contract is not None
        assert contract.returns is Shape.SINGLE

    def test_default_contract_shapes(self):
        @processor_contract()
        def anything(value):
            return value

        contract = get_processor_contract(anything)
        assert contract is not None
        assert contract.accepts is Shape.ANY
        assert contract.returns is Shape.ANY
        assert contract.requires_context == frozenset()
