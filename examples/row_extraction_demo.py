"""Row-scoped extraction demo.

Run with:  python examples/row_extraction_demo.py

The demo page (examples/products.html) contains a product with a missing
price, one with two prices, one with an empty SKU attribute and a nested
product card. Extracting titles and prices as two flat lists and zipping
them pairs Beta with Gamma's first price and silently shifts everything;
RowExtractor instead scopes every field to its own record.
"""

from pathlib import Path

from parsel import (
    MissingValue,
    MultipleMatches,
    RowExtractor,
    Selector,
    many,
    optional,
    required,
)

html = (Path(__file__).parent / "products.html").read_text(encoding="utf-8")

extractor = RowExtractor(
    row="css:div.product",
    fields={
        "sku": optional("xpath:./@data-sku"),
        "title": required("css:h2.title::text"),
        "price": required("css:span.price::text"),
        "tags": many("css:li.tag::text"),
    },
)

report = extractor.extract(html)

print("== Row-scoped extraction (row order preserved) ==")
for row in report:
    print(f"row {row.index}: ok={row.ok}")
    for name, value in row.fields.items():
        if isinstance(value, MissingValue):
            shown = "<MissingValue>"
        elif isinstance(value, MultipleMatches):
            shown = f"<MultipleMatches count={value.count} values={list(value.values)}>"
        else:
            shown = repr(value)
        print(f"    {name}: {shown}")

print("\n== Violations (which record broke the contract) ==")
for violation in report.violations:
    print(
        f"row {violation.row_index}, field {violation.field!r}: "
        f"{type(violation.error).__name__}"
    )

print("\n== Why zip() of two flat lists is not an option ==")
selector = Selector(text=html)
titles = selector.css("h2.title::text").getall()
prices = selector.css("span.price::text").getall()
print(f"flat titles ({len(titles)}): {titles}")
print(f"flat prices ({len(prices)}): {prices}")
print("zip pairs them as:")
for title, price in zip(titles, prices, strict=False):
    print(f"    {title!r} -> {price!r}")
