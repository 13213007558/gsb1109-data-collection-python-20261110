from __future__ import annotations

import json
from typing import Any

import pytest

from parsel import ExtractionError, Recipe, RecipeError, Record, Selector

PAGE = """
<html>
<body>
<script id="products">
{
  "items": [
    {
      "id": 1,
      "name": "Widget",
      "description": "<div><p>First <b>product</b></p><p>More info</p></div>",
      "tags": ["sale", "new"]
    },
    {
      "id": 2,
      "name": "Gadget",
      "description": "<p>Second product</p>",
      "tags": []
    }
  ]
}
</script>
<script id="broken">{this is not json</script>
</body>
</html>
"""


def product_recipe(**field_kwargs: Any) -> Recipe:
    return Recipe(
        [
            {"op": "xpath", "query": "//script[@id='products']/text()"},
            {"op": "parse_json"},
            {"op": "jmespath", "query": "items[*]"},
            {
                "op": "field",
                "name": "id",
                "pipeline": [{"op": "jmespath", "query": "id"}],
            },
            {
                "op": "field",
                "name": "name",
                "pipeline": [{"op": "jmespath", "query": "name"}],
            },
            {
                "op": "field",
                "name": "summary",
                "pipeline": [
                    {"op": "jmespath", "query": "description"},
                    {"op": "parse_html"},
                    {"op": "xpath", "query": "//p[1]/text()"},
                    {"op": "text"},
                ],
                **field_kwargs,
            },
            {
                "op": "field",
                "name": "tags",
                "pipeline": [{"op": "jmespath", "query": "tags[*]"}],
            },
        ]
    )


class TestRecipeValidation:
    def test_unsupported_operation(self) -> None:
        with pytest.raises(RecipeError, match="Unsupported operation"):
            Recipe([{"op": "eval", "query": "1 + 1"}])

    def test_recipe_must_be_a_nonempty_list(self) -> None:
        with pytest.raises(RecipeError):
            Recipe([])

    def test_stage_must_be_mapping_with_op(self) -> None:
        with pytest.raises(RecipeError):
            Recipe([{"query": "//div"}])  # type: ignore[list-item]
        with pytest.raises(RecipeError):
            Recipe(["not-a-mapping"])  # type: ignore[list-item]

    def test_query_required_for_selection_stages(self) -> None:
        with pytest.raises(RecipeError, match="requires a 'query'"):
            Recipe([{"op": "xpath"}])

    def test_invalid_expand_mode(self) -> None:
        with pytest.raises(RecipeError, match="Unsupported expand mode"):
            Recipe([{"op": "xpath", "query": "//div", "expand": "explode"}])

    def test_field_requires_name_and_pipeline(self) -> None:
        with pytest.raises(RecipeError, match="'name'"):
            Recipe([{"op": "field", "pipeline": [{"op": "text"}]}])
        with pytest.raises(RecipeError, match="'pipeline'"):
            Recipe([{"op": "field", "name": "x"}])

    def test_pipeline_only_on_field(self) -> None:
        with pytest.raises(RecipeError, match="only supported by the 'field'"):
            Recipe(
                [
                    {
                        "op": "text",
                        "pipeline": [{"op": "text"}],
                    }
                ]
            )

    def test_nested_field_is_rejected(self) -> None:
        with pytest.raises(RecipeError, match="nested 'field'"):
            Recipe(
                [
                    {
                        "op": "field",
                        "name": "outer",
                        "pipeline": [
                            {
                                "op": "field",
                                "name": "inner",
                                "pipeline": [{"op": "text"}],
                            }
                        ],
                    }
                ]
            )

    def test_text_on_missing_validation(self) -> None:
        with pytest.raises(RecipeError, match="on_missing"):
            Recipe([{"op": "text", "kwargs": {"on_missing": "explode"}}])
        with pytest.raises(RecipeError, match="'default'"):
            Recipe(
                [
                    {
                        "op": "text",
                        "kwargs": {"on_missing": "default"},
                    }
                ]
            )

    def test_kwargs_must_be_mapping(self) -> None:
        with pytest.raises(RecipeError, match="kwargs must be a mapping"):
            Recipe([{"op": "xpath", "query": "//div", "kwargs": []}])  # type: ignore[dict-item]

    def test_parse_html_type_validation(self) -> None:
        with pytest.raises(RecipeError, match="parse_html type"):
            Recipe([{"op": "parse_html", "kwargs": {"type": "pdf"}}])

    def test_name_only_on_field(self) -> None:
        with pytest.raises(RecipeError, match="'name' is only supported"):
            Recipe([{"op": "text", "name": "x"}])


class TestStages:
    def test_xpath_and_css_flatten_by_default(self) -> None:
        recipe = Recipe(
            [
                {"op": "xpath", "query": "//li"},
                {"op": "css", "query": "span::text"},
                {"op": "text"},
            ]
        )
        records = recipe.extract(
            "<ul><li><span>a</span></li><li><span>b</span></li></ul>"
        )
        assert [record.data["_value"] for record in records] == ["a", "b"]

    def test_expand_first(self) -> None:
        recipe = Recipe(
            [
                {"op": "xpath", "query": "//li/text()", "expand": "first"},
                {"op": "text"},
            ]
        )
        records = recipe.extract("<ul><li>a</li><li>b</li></ul>")
        assert len(records) == 1
        assert records[0].data["_value"] == "a"

    def test_expand_wrap_keeps_list_together(self) -> None:
        recipe = Recipe(
            [
                {"op": "xpath", "query": "//li/text()", "expand": "wrap"},
                {"op": "text"},
            ]
        )
        records = recipe.extract("<ul><li>a</li><li>b</li></ul>")
        assert len(records) == 1
        assert records[0].data["_value"] == ["a", "b"]

    def test_text_drop_removes_empty_branches(self) -> None:
        recipe = Recipe(
            [
                {"op": "xpath", "query": "//li"},
                {"op": "xpath", "query": "p/text()", "expand": "first"},
                {"op": "text", "kwargs": {"on_missing": "drop"}},
            ]
        )
        records = recipe.extract("<ul><li><p>keep</p></li><li></li></ul>")
        assert [record.data["_value"] for record in records] == ["keep"]

    def test_text_default_replaces_missing_value(self) -> None:
        recipe = Recipe(
            [
                {"op": "xpath", "query": "//li"},
                {"op": "xpath", "query": "p/text()", "expand": "first"},
                {
                    "op": "text",
                    "kwargs": {"on_missing": "default", "default": "n/a"},
                },
            ]
        )
        records = recipe.extract("<ul><li><p>keep</p></li><li></li></ul>")
        values = [record.data["_value"] for record in records]
        assert values == ["keep", "n/a"]

    def test_parse_json_then_jmespath(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_json"},
                {"op": "jmespath", "query": "items[*].name"},
            ]
        )
        records = recipe.extract('{"items": [{"name": "a"}, {"name": "b"}]}')
        assert [record.data["_value"] for record in records] == ["a", "b"]

    def test_jsonpath_operation(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_json"},
                {"op": "jsonpath", "query": "$.items[*].name"},
            ]
        )
        records = recipe.extract('{"items": [{"name": "a"}, {"name": "b"}]}')
        assert [record.data["_value"] for record in records] == ["a", "b"]

    def test_parse_html_treats_string_as_html(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_json"},
                {"op": "jmespath", "query": "html"},
                {"op": "parse_html"},
                {"op": "css", "query": "b::text"},
                {"op": "text"},
            ]
        )
        records = recipe.extract('{"html": "<div><b>x</b><b>y</b></div>"}')
        assert [record.data["_value"] for record in records] == ["x", "y"]

    def test_parse_html_xml_type(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_html", "kwargs": {"type": "xml"}},
                {"op": "xpath", "query": "/root/item/text()"},
                {"op": "text"},
            ]
        )
        records = recipe.extract("<root><item>x</item></root>", type="xml")
        assert records[0].data["_value"] == "x"

    def test_selection_on_raw_json_value_is_supported(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_json"},
                {"op": "jmespath", "query": "items", "expand": "wrap"},
            ]
        )
        records = recipe.extract('{"items": [1, 2]}')
        assert records[0].data["_value"] == [1, 2]

    def test_xpath_on_json_value_fails_as_stage_error(self) -> None:
        recipe = Recipe([{"op": "parse_json"}, {"op": "xpath", "query": "//item"}])
        records = recipe.extract('{"items": [1, 2]}')
        assert len(records) == 1
        error = records[0].errors[0]
        assert error.operation == "xpath"
        assert "expects a selector" in error.message

    def test_first_with_no_match_keeps_branch_through_next_selection(self) -> None:
        recipe = Recipe(
            [
                {"op": "xpath", "query": "//li"},
                {"op": "xpath", "query": "p/text()", "expand": "first"},
                {"op": "xpath", "query": "span/text()", "expand": "first"},
                {
                    "op": "text",
                    "kwargs": {"on_missing": "default", "default": "none"},
                },
            ]
        )
        records = recipe.extract("<ul><li><p>x</p></li><li></li></ul>")
        assert [record.data["_value"] for record in records] == ["none", "none"]

    def test_wrap_is_sticky_across_later_stages(self) -> None:
        recipe = Recipe(
            [
                {"op": "xpath", "query": "//li/text()", "expand": "wrap"},
                {"op": "text"},
            ]
        )
        records = recipe.extract("<ul><li>a</li><li>b</li></ul>")
        assert len(records) == 1
        assert records[0].data["_value"] == ["a", "b"]

    def test_parse_json_is_idempotent_on_json_data(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_json"},
                {"op": "parse_json"},
                {"op": "jmespath", "query": "x"},
            ]
        )
        records = recipe.extract('{"x": 1}')
        assert [record.data["_value"] for record in records] == [1]

    def test_extract_forwards_text_type(self) -> None:
        recipe = Recipe([{"op": "text"}])
        records = recipe.extract("plain text", type="text")
        assert records[0].data["_value"] == "plain text"

    def test_expand_rejected_on_one_to_one_stages(self) -> None:
        with pytest.raises(RecipeError, match="does not support 'expand'"):
            Recipe([{"op": "parse_json", "expand": "wrap"}])


class TestFields:
    def test_fields_become_plain_record_data(self) -> None:
        records = product_recipe().extract(Selector(text=PAGE))
        assert len(records) == 2
        first, second = records
        assert first.ok
        assert first.data == {
            "id": [1],
            "name": ["Widget"],
            "summary": ["First "],
            "tags": ["sale", "new"],
        }
        assert second.data["name"] == ["Gadget"]
        assert second.data["summary"] == ["Second product"]
        assert "tags" in second.missing
        assert second.missing["tags"].message == "pipeline produced no value"

    def test_scalar_field_with_expand_first(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_json"},
                {
                    "op": "field",
                    "name": "title",
                    "expand": "first",
                    "pipeline": [{"op": "jmespath", "query": "titles[*]"}],
                },
            ]
        )
        records = recipe.extract('{"titles": ["a", "b"]}')
        assert records[0].data["title"] == "a"

    def test_missing_field_records_reason(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_json"},
                {
                    "op": "field",
                    "name": "missing",
                    "pipeline": [{"op": "jmespath", "query": "nope[*]"}],
                },
            ]
        )
        records = recipe.extract('{"present": true}')
        record = records[0]
        assert not record.ok
        assert "missing" in record.missing
        error = record.missing["missing"]
        assert isinstance(error, ExtractionError)
        assert error.operation == "field"
        assert "no value" in error.message

    def test_failed_field_pipeline_records_stage_and_origin(self) -> None:
        recipe = Recipe(
            [
                {"op": "xpath", "query": "//script/text()"},
                {
                    "op": "field",
                    "name": "payload",
                    "pipeline": [
                        {"op": "parse_json"},
                        {"op": "jmespath", "query": "x"},
                    ],
                },
            ]
        )
        records = recipe.extract("<html><script>{bad</script></html>")
        record = records[0]
        error = record.missing["payload"]
        assert error.operation == "parse_json"
        assert error.stage == 0
        assert "invalid JSON" in error.message
        assert error.value == "{bad"
        assert "<script>{bad</script>" in error.origin

    def test_expanded_products_keep_independent_records(self) -> None:
        records = product_recipe().extract(PAGE)
        assert len(records) == 2
        assert records[0].data is not records[1].data
        assert records[0].data["name"] != records[1].data["name"]


class TestErrorIsolation:
    def test_bad_json_does_not_drop_other_products(self) -> None:
        recipe = Recipe(
            [
                {"op": "xpath", "query": "//script/text()"},
                {"op": "parse_json"},
                {"op": "jmespath", "query": "items[*]"},
                {
                    "op": "field",
                    "name": "name",
                    "pipeline": [{"op": "jmespath", "query": "name"}],
                },
            ]
        )
        page = (
            "<html>"
            '<script>{"items": [{"name": "ok"}]}</script>'
            "<script>{broken</script>"
            "</html>"
        )
        records = recipe.extract(page)
        assert len(records) == 2
        good = next(record for record in records if record.ok)
        bad = next(record for record in records if not record.ok)
        assert good.data["name"] == ["ok"]
        error = bad.errors[0]
        assert error.operation == "parse_json"
        assert error.stage == 1
        assert error.value == "{broken"
        assert error.origin.startswith("<html>")

    def test_error_carries_original_input_snapshot(self) -> None:
        recipe = Recipe([{"op": "parse_json"}])
        records = recipe.extract("not json at all", type="text")
        error = records[0].errors[0]
        assert error.origin == "not json at all"
        assert error.value == "not json at all"

    def test_one_bad_item_keeps_sibling_items_in_same_json(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_json"},
                {"op": "jmespath", "query": "items[*]"},
                {
                    "op": "field",
                    "name": "detail",
                    "pipeline": [
                        {"op": "jmespath", "query": "html"},
                        {"op": "parse_html"},
                        {"op": "xpath", "query": "//p/text()"},
                        {"op": "text"},
                    ],
                },
            ]
        )
        data = json.dumps(
            {
                "items": [
                    {"html": "<p>fine</p>"},
                    {"html": 123},
                    {"html": "<p>also fine</p>"},
                ]
            }
        )
        records = recipe.extract(data)
        assert len(records) == 3
        ok_records = [record for record in records if record.ok]
        broken = next(record for record in records if not record.ok)
        assert [record.data["detail"] for record in ok_records] == [
            ["fine"],
            ["also fine"],
        ]
        assert broken.missing["detail"].operation == "parse_html"
        assert "expects text" in broken.missing["detail"].message


class TestRecordOutput:
    def test_extract_accepts_string_and_selector(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_json"},
                {
                    "op": "field",
                    "name": "x",
                    "pipeline": [{"op": "jmespath", "query": "x"}],
                },
            ]
        )
        from_string = recipe.extract('{"x": 1}')
        from_selector = recipe.extract(Selector(text='{"x": 1}'))
        assert from_string[0].data == from_selector[0].data == {"x": [1]}

    def test_extract_dicts_is_json_serializable(self) -> None:
        records = product_recipe().extract_dicts(PAGE)
        encoded = json.dumps(records)
        assert json.loads(encoded) == records

    def test_failed_record_dict_exposes_errors(self) -> None:
        recipe = Recipe([{"op": "parse_json"}])
        result = recipe.extract_dicts("{bad")[0]
        assert "_errors" in result
        error = result["_errors"][0]
        assert set(error) == {"stage", "operation", "message", "value", "origin"}

    def test_missing_field_dict_shape(self) -> None:
        recipe = Recipe(
            [
                {"op": "parse_json"},
                {
                    "op": "field",
                    "name": "gone",
                    "pipeline": [{"op": "jmespath", "query": "gone"}],
                },
            ]
        )
        result = recipe.extract_dicts('{"x": 1}')[0]
        assert "_missing" in result
        assert "gone" in result["_missing"]

    def test_record_class_defaults(self) -> None:
        record = Record()
        assert record.ok
        assert record.data == {}
        assert record.as_dict() == {}
