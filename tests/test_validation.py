from collections.abc import Mapping
from datetime import datetime
from typing import Any

import pytest
from parsel import Selector

from itemloaders import ItemLoader, Rule, RuleViolation, ValidationError, rule
from itemloaders.processors import Compose, TakeFirst
from itemloaders.validation import ValidationProblem


def failing_price_rule() -> Rule:
    return Rule(
        ("price", "promo_price"),
        lambda price, promo_price: (
            None
            if promo_price <= price
            else "promo price must not be higher than the original price"
        ),
        name="promo_not_above_price",
    )


class OfferLoader(ItemLoader):
    default_output_processor = TakeFirst()
    price_out = Compose(TakeFirst(), float)
    promo_price_out = Compose(TakeFirst(), float)
    start_out = Compose(TakeFirst(), datetime.fromisoformat)
    end_out = Compose(TakeFirst(), datetime.fromisoformat)

    @rule(("price", "promo_price"))
    def promo_not_above_price(price, promo_price):
        if promo_price > price:
            return "promo price must not be higher than the original price"
        return None

    @rule(("start", "end"))
    def end_after_start(start, end):
        if end <= start:
            return "end time must be later than start time"
        return None


def make_loader(
    values: Mapping[str, Any],
    loader_class: type[ItemLoader] = OfferLoader,
) -> ItemLoader:
    loader = loader_class()
    for field_name, value in values.items():
        loader.add_value(field_name, value)
    return loader


VALID_OFFER = {
    "price": "100",
    "promo_price": "80",
    "start": "2026-11-01T00:00:00",
    "end": "2026-11-10T00:00:00",
}


class TestRuleDeclaration:
    def test_decorated_methods_become_rules(self):
        names = [
            validation_rule.name for validation_rule in OfferLoader.validation_rules
        ]
        assert names == ["promo_not_above_price", "end_after_start"]

    def test_rule_fields_are_normalized_to_tuple(self):
        single = Rule("name", lambda name: None)
        assert single.fields == ("name",)
        multiple = Rule(("a", "b"), lambda a, b: None)
        assert multiple.fields == ("a", "b")

    def test_rule_requires_at_least_one_field(self):
        with pytest.raises(ValueError, match="at least one field"):
            Rule((), lambda **kwargs: None)

    def test_rule_name_defaults_to_function_name(self):
        def my_check(**kwargs):
            return None

        assert Rule(("a",), my_check).name == "my_check"

    def test_rules_accumulate_through_inheritance(self):
        class ChildLoader(OfferLoader):
            @rule(("name",))
            def name_rule(name):
                return None

        assert len(OfferLoader.validation_rules) == 2
        assert len(ChildLoader.validation_rules) == 3
        assert ChildLoader.validation_rules[:2] == OfferLoader.validation_rules

    def test_explicit_rules_combine_with_decorated_ones(self):
        extra = Rule(("name",), lambda name: None, name="explicit")

        class ExplicitLoader(OfferLoader):
            validation_rules = (*OfferLoader.validation_rules, extra)

        assert [r.name for r in ExplicitLoader.validation_rules] == [
            "promo_not_above_price",
            "end_after_start",
            "explicit",
        ]


class TestRuleContract:
    def test_none_and_true_mean_success(self):
        assert Rule(("a",), lambda a: None).validate({"a": 1}) is None
        assert Rule(("a",), lambda a: True).validate({"a": 1}) is None

    def test_string_message_is_the_violation(self):
        validation_rule = Rule(("a",), lambda a: "bad value")
        assert validation_rule.validate({"a": 1}) == "bad value"

    def test_false_uses_configured_message(self):
        validation_rule = Rule(
            ("a", "b"), lambda a, b: a == b, message="a must equal b"
        )
        assert validation_rule.validate({"a": 1, "b": 2}) == "a must equal b"

    def test_false_without_message_uses_generic_fallback(self):
        validation_rule = Rule(("a", "b"), lambda a, b: a == b)
        message = validation_rule.validate({"a": 1, "b": 2})
        assert message is not None
        assert "a" in message
        assert "b" in message

    def test_rule_violation_is_caught(self):
        def check(name):
            if not name:
                raise RuleViolation("name must be non-empty")

        validation_rule = Rule(("name",), check)
        assert validation_rule.validate({"name": ""}) == "name must be non-empty"
        assert validation_rule.validate({"name": "x"}) is None


class TestLoadItemValidation:
    def test_valid_record_loads_converted_values(self):
        item = make_loader(VALID_OFFER).load_item()
        assert item == {
            "price": 100.0,
            "promo_price": 80.0,
            "start": datetime(2026, 11, 1),
            "end": datetime(2026, 11, 10),
        }

    def test_independent_rule_violations_are_all_collected(self):
        loader = make_loader(
            {
                "price": "100",
                "promo_price": "120",
                "start": "2026-11-10T00:00:00",
                "end": "2026-11-01T00:00:00",
            }
        )
        with pytest.raises(ValidationError) as exc_info:
            loader.load_item()
        problems = exc_info.value.problems
        assert len(problems) == 2
        assert [problem.rule for problem in problems] == [
            "promo_not_above_price",
            "end_after_start",
        ]
        assert problems[0].fields == ("price", "promo_price")
        assert problems[1].fields == ("start", "end")
        assert all(isinstance(problem, ValidationProblem) for problem in problems)

    def test_problem_string_locates_the_field_combination(self):
        problem = ValidationProblem(
            message="boom", fields=("price", "promo_price"), rule="price_rule"
        )
        text = str(problem)
        assert "[price_rule]" in text
        assert "price, promo_price" in text
        assert "boom" in text

    def test_missing_dependency_is_reported_and_check_not_run(self):
        def must_not_run(**kwargs):
            raise AssertionError("the rule must not run with a missing dependency")

        class MissingLoader(ItemLoader):
            default_output_processor = TakeFirst()
            validation_rules = (Rule(("present", "absent"), must_not_run),)

        loader = MissingLoader()
        loader.add_value("present", "value")
        with pytest.raises(ValidationError) as exc_info:
            loader.load_item()
        problem = exc_info.value.problems[0]
        assert problem.missing == ("absent",)
        assert problem.fields == ("present", "absent")
        assert problem.rule == "must_not_run"

    def test_none_output_value_counts_as_missing(self):
        class NoneLoader(ItemLoader):
            name_out = TakeFirst()
            validation_rules = (failing_price_rule(),)

        loader = NoneLoader()
        loader.add_value("price", "100")
        with pytest.raises(ValidationError) as exc_info:
            loader.load_item()
        problem = exc_info.value.problems[0]
        assert problem.missing == ("promo_price",)

    def test_conversion_failure_keeps_field_error_and_skips_dependent_rule(self):
        loader = make_loader(
            {
                "price": "100",
                "promo_price": "not a number",
                "start": "2026-11-10T00:00:00",
                "end": "2026-11-01T00:00:00",
            }
        )
        with pytest.raises(ValidationError) as exc_info:
            loader.load_item()
        problems = exc_info.value.problems
        assert len(problems) == 2
        field_problem, rule_problem = problems
        assert field_problem.rule is None
        assert field_problem.fields == ("promo_price",)
        assert "could not convert" in field_problem.message
        # The rule derived from the failed field produces no extra error...
        assert all(problem.rule != "promo_not_above_price" for problem in problems)
        # ...while the unrelated, independent rule is still evaluated.
        assert rule_problem.rule == "end_after_start"
        # The item is not populated when the record is rejected.
        assert loader.item == {}

    def test_several_conversion_failures_are_collected(self):
        loader = make_loader(
            {
                "price": "bad",
                "promo_price": "also bad",
                "start": "2026-11-01T00:00:00",
                "end": "2026-11-10T00:00:00",
            }
        )
        with pytest.raises(ValidationError) as exc_info:
            loader.load_item()
        failed_fields = {problem.fields[0] for problem in exc_info.value.problems}
        assert failed_fields == {"price", "promo_price"}
        # Every rule depends on a failed field, so no business error is added.
        assert all(problem.rule is None for problem in exc_info.value.problems)

    def test_validation_error_is_a_value_error(self):
        assert issubclass(ValidationError, ValueError)
        loader = make_loader({"price": "100", "start": "2026-11-01T00:00:00"})
        with pytest.raises(ValidationError) as exc_info:
            loader.load_item()
        assert exc_info.value.problems

    def test_successful_load_does_not_raise(self):
        assert make_loader(VALID_OFFER).load_item()


class TestLoaderWithoutRules:
    def test_no_validation_configuration_needed(self):
        loader = ItemLoader()
        loader.add_value("name", "marta")
        assert loader.load_item() == {"name": ["marta"]}
        assert loader.validation_rules == ()

    def test_conversion_error_remains_value_error(self):
        class PlainLoader(ItemLoader):
            name_out = Compose(TakeFirst(), float)

        loader = PlainLoader()
        loader.add_value("name", ["$10"])
        with pytest.raises(ValueError, match="name"):
            loader.load_item()


class TestNestedLoaderValidation:
    SELECTOR = Selector(
        text="<html><body><div>"
        "<p class='price'>100</p>"
        "<p class='promo'>120</p>"
        "</div></body></html>"
    )

    def test_rules_see_values_collected_through_nested_loaders(self):
        loader = OfferLoader(selector=self.SELECTOR)
        nested = loader.nested_css("div")
        nested.add_css("price", "p.price::text")
        loader.add_css("promo_price", "p.promo::text")
        loader.add_value("start", "2026-11-01T00:00:00")
        loader.add_value("end", "2026-11-10T00:00:00")
        with pytest.raises(ValidationError) as exc_info:
            loader.load_item()
        assert exc_info.value.problems[0].rule == "promo_not_above_price"
        assert exc_info.value.problems[0].fields == ("price", "promo_price")
