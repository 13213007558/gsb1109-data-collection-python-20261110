from __future__ import annotations

from parsel import Selector
from parsel.rows import (
    Cardinality,
    Field,
    FieldError,
    MissingValue,
    MultipleMatches,
    RowExtractor,
    Violation,
    many,
    optional,
    required,
)

PAGE = """
<html><body>
  <div class="product" data-sku="A-001">
    <h2 class="title">Alpha</h2>
    <span class="price">$10.00</span>
    <ul class="tags"><li class="tag">new</li><li class="tag">sale</li></ul>
  </div>
  <div class="product" data-sku="B-002">
    <h2 class="title">Beta</h2>
    <ul class="tags"><li class="tag">new</li></ul>
  </div>
  <div class="product" data-sku="">
    <h2 class="title">Gamma</h2>
    <span class="price">$30.00</span>
    <span class="price">$32.00</span>
  </div>
  <div class="product" data-sku="D-004">
    <h2 class="title">Delta Bundle</h2>
    <span class="price">$99.00</span>
    <div class="product" data-sku="D-004-A">
      <h2 class="title">Delta Add-on</h2>
      <span class="price">$5.00</span>
    </div>
  </div>
</body></html>
"""


def make_extractor() -> RowExtractor:
    return RowExtractor(
        row="css:div.product",
        fields={
            "sku": optional("xpath:./@data-sku"),
            "title": required("css:h2.title::text"),
            "price": required("css:span.price::text"),
            "discount": optional("css:.discount::text"),
            "tags": many("css:li.tag::text"),
        },
    )


def test_rows_keep_document_order_including_nested_records() -> None:
    report = make_extractor().extract(PAGE)
    assert [row.fields["title"] for row in report] == [
        "Alpha",
        "Beta",
        "Gamma",
        "Delta Bundle",
        "Delta Add-on",
    ]
    assert [row.index for row in report] == list(range(5))


def test_missing_value_is_reported_on_the_right_row() -> None:
    report = make_extractor().extract(PAGE)
    beta = report[1]
    assert isinstance(beta.fields["price"], MissingValue)
    assert beta.fields["price"].query == "css:span.price::text"
    assert not beta.ok
    assert beta.fields["title"] == "Beta"
    # other rows are unaffected by the gap
    assert report[0].fields["price"] == "$10.00"
    assert report[2].fields["title"] == "Gamma"


def test_multiple_matches_is_distinct_from_missing() -> None:
    report = make_extractor().extract(PAGE)
    gamma = report[2]
    error = gamma.fields["price"]
    assert isinstance(error, MultipleMatches)
    assert error.count == 2
    assert error.values == ("$30.00", "$32.00")
    assert not isinstance(error, MissingValue)


def test_empty_string_is_distinct_from_missing_value() -> None:
    report = make_extractor().extract(PAGE)
    assert report[2].fields["sku"] == ""
    assert not isinstance(report[2].fields["sku"], FieldError)
    assert "sku" not in report[2].errors


def test_optional_field_absent_yields_none_without_error() -> None:
    report = make_extractor().extract(PAGE)
    assert all(row.fields["discount"] is None for row in report)
    assert all("discount" not in row.errors for row in report)


def test_many_field_collects_all_values_and_allows_empty() -> None:
    report = make_extractor().extract(PAGE)
    assert report[0].fields["tags"] == ["new", "sale"]
    assert report[1].fields["tags"] == ["new"]
    assert report[2].fields["tags"] == []


def test_nested_record_values_do_not_leak_into_the_outer_record() -> None:
    report = make_extractor().extract(PAGE)
    outer = report[3]
    inner = report[4]
    assert outer.fields["price"] == "$99.00"
    assert outer.fields["title"] == "Delta Bundle"
    assert outer.ok
    assert inner.fields["price"] == "$5.00"
    assert inner.fields["sku"] == "D-004-A"


def test_violations_locate_the_offending_records() -> None:
    report = make_extractor().extract(PAGE)
    assert not report.ok
    violations = report.violations
    assert [(v.row_index, v.field) for v in violations] == [
        (1, "price"),
        (2, "price"),
    ]
    assert isinstance(violations[0].error, MissingValue)
    assert isinstance(violations[1].error, MultipleMatches)
    assert violations[0] == Violation(1, "price", violations[0].error)


def test_plain_string_field_is_shorthand_for_required() -> None:
    extractor = RowExtractor(
        row="css:div.product", fields={"title": "css:h2.title::text"}
    )
    assert extractor.fields["title"] == Field("css:h2.title::text", Cardinality.ONE)
    report = extractor.extract(PAGE)
    assert report[0].fields["title"] == "Alpha"


def test_extract_accepts_a_selector_instance() -> None:
    report = make_extractor().extract(Selector(text=PAGE))
    assert len(report) == 5
    assert report[0].fields["price"] == "$10.00"


def test_unprefixed_query_defaults_to_css() -> None:
    extractor = RowExtractor(row="div.product", fields={"title": "h2.title::text"})
    report = extractor.extract(PAGE)
    assert report[0].fields["title"] == "Alpha"


def test_xpath_row_query() -> None:
    extractor = RowExtractor(
        row="xpath://div[@class='product']",
        fields={"title": required("xpath:.//h2/text()")},
    )
    report = extractor.extract(PAGE)
    assert [row.fields["title"] for row in report][:2] == ["Alpha", "Beta"]


def test_report_is_a_sequence_of_rows() -> None:
    report = make_extractor().extract(PAGE)
    assert len(report) == 5
    assert report[0].index == 0
    assert [row.index for row in report[:2]] == [0, 1]
    assert [row.index for row in report] == [0, 1, 2, 3, 4]


def test_zip_of_flat_lists_would_misalign_but_rows_do_not() -> None:
    selector = Selector(text=PAGE)
    titles = selector.css("h2.title::text").getall()
    prices = selector.css("span.price::text").getall()
    # flat zipping pairs Beta with Gamma's first price
    assert dict(zip(titles, prices, strict=False))["Beta"] == "$30.00"
    report = make_extractor().extract(PAGE)
    assert isinstance(report[1].fields["price"], MissingValue)
    assert report[2].fields["price"] != "$30.00"
