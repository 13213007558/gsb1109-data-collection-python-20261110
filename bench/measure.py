"""Measure parse/compile cost of many field queries on one fixed local page.

The page is built in-process, so no network time is ever included.

Run before and after the shared-document refactor:

    .venv/bin/python bench/measure.py
"""

from __future__ import annotations

import gc
import statistics
import time
import weakref
from functools import wraps
from typing import TYPE_CHECKING

from lxml import etree

import parsel.selector as ps
from parsel import Selector

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    from parsel import SelectorDocument
except ImportError:
    SelectorDocument = None


ITEM_COUNT = 3000
ROUNDS = 30
INDEPENDENT_QUERIES = 20
FIELDS_CSS = [
    "title::text",
    ".catalog > li.product::attr(data-index)",
    "h2.title a::text",
    "h2.title a::attr(href)",
    ".price::text",
    ".desc::text",
]
FIELDS_XPATH = [
    "//h2[@class='title']/a/text()",
    "//span[@class='price']/text()",
    "//p[@class='desc']/text()",
    "//li/@data-index",
]


def build_catalog(item_count: int) -> str:
    items = "".join(
        f'<li class="product" data-index="{i}">'
        f'<h2 class="title"><a href="/item/{i}">Product {i}</a></h2>'
        f'<span class="price">${9.99 + (i % 500):.2f}</span>'
        f'<p class="desc">A description of product {i}.</p>'
        "</li>"
        for i in range(item_count)
    )
    return (
        "<!DOCTYPE html><html><head><title>Catalog</title></head><body>"
        f'<ul class="catalog">{items}</ul>'
        "</body></html>"
    )


PAGE = build_catalog(ITEM_COUNT)


class Counters:
    def __init__(self) -> None:
        self.parses = 0
        self.xpath_compiles = 0
        self.css_translations = 0
        self.json_loads = 0

    def snapshot(self) -> str:
        return (
            f"parses={self.parses} xpath_compiles={self.xpath_compiles} "
            f"css_translations={self.css_translations}"
        )


def reset_caches() -> None:
    ps._compile_xpath.cache_clear()
    ps._compile_jsonpath.cache_clear()


def install_probes(counters: Counters) -> Callable[[], None]:
    reset_caches()
    original_create_root_node = ps.create_root_node

    @wraps(original_create_root_node)
    def counted_create_root_node(*args, **kwargs):
        counters.parses += 1
        return original_create_root_node(*args, **kwargs)

    ps.create_root_node = counted_create_root_node

    original_xpath = etree.XPath

    def counted_xpath(*args, **kwargs):
        counters.xpath_compiles += 1
        return original_xpath(*args, **kwargs)

    etree.XPath = counted_xpath
    ps.etree.XPath = counted_xpath

    translators = [group["_csstranslator"] for group in ps._ctgroup.values()]
    originals = [t.css_to_xpath for t in translators]
    for translator in translators:
        original = translator.css_to_xpath

        def counted(query, _original=original):
            counters.css_translations += 1
            return _original(query)

        translator.css_to_xpath = counted

    original_json_loads = ps.json.loads

    def counted_json_loads(*args, **kwargs):
        counters.json_loads += 1
        return original_json_loads(*args, **kwargs)

    ps.json.loads = counted_json_loads

    def remove() -> None:
        ps.create_root_node = original_create_root_node
        etree.XPath = original_xpath
        ps.etree.XPath = original_xpath
        for translator, original in zip(translators, originals, strict=True):
            translator.css_to_xpath = original
        ps.json.loads = original_json_loads

    return remove


def run_field_queries(view: Selector) -> list[str]:
    """The field queries one independent query object runs on the page."""
    values: list[str] = []
    for query in FIELDS_CSS:
        values.extend(view.css(query).getall())
    for query in FIELDS_XPATH:
        values.extend(view.xpath(query).getall())
    return values


def run_independent_queries(factory: Callable[[], Selector]) -> list[str]:
    """K independent query objects, each extracting its own fields."""
    values: list[str] = []
    for _ in range(INDEPENDENT_QUERIES):
        values.extend(run_field_queries(factory()))
    return values


def lxml_element_count() -> int:
    return sum(1 for obj in gc.get_objects() if etree.iselement(obj))


def time_ms(func: Callable[[], object]) -> tuple[float, object]:
    timings: list[float] = []
    result = None
    for _ in range(ROUNDS):
        gc.collect()
        start = time.perf_counter()
        result = func()
        timings.append((time.perf_counter() - start) * 1000)
    return statistics.median(timings), result


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def measure_section(
    title: str, setup: Callable[[], Callable[[], object]]
) -> object:
    """Return the values produced by one run; print per-run counts and timing."""
    section(title)
    counters = Counters()
    remove_probes = install_probes(counters)
    func = setup()
    values = func()  # single uncached run: exact per-run parse/compile counts
    per_run = (
        f"parses={counters.parses} xpath_compiles={counters.xpath_compiles} "
        f"css_translations={counters.css_translations}"
    )
    remove_probes()
    median_ms, timed_values = time_ms(func)
    assert timed_values == values
    print(f"counters per run (cold caches): {per_run}")
    print(f"median time per run: {median_ms:.1f} ms")
    return values


def main() -> None:
    print(f"page: {ITEM_COUNT} products, {len(PAGE):,} bytes, rounds={ROUNDS}")

    # ---- Warm up outside the measured comparison --------------------------
    warm = Selector(text=PAGE)
    run_field_queries(warm)
    del warm
    gc.collect()

    # ---- Pattern 1: a fresh Selector per independent field query ----------
    def setup_separate() -> Callable[[], object]:
        return lambda: run_independent_queries(lambda: Selector(text=PAGE))

    values_separate = measure_section(
        "BEFORE: one Selector(text=PAGE) per independent query object",
        setup_separate,
    )

    # ---- Pattern 2: one Selector reused for every query (current best) ----
    shared_selector = Selector(text=PAGE)
    values_reused = measure_section(
        "REFERENCE: a single reused Selector (read-only)",
        lambda: lambda: run_independent_queries(lambda: shared_selector),
    )
    assert values_separate == values_reused

    if SelectorDocument is None:
        print("\nSelectorDocument not available yet (baseline run).")
        return

    # ---- Pattern 3: shared parsed document, independent query objects -----
    def setup_shared() -> Callable[[], object]:
        document = SelectorDocument(text=PAGE)
        return lambda: run_independent_queries(document.selector)

    values_shared = measure_section(
        "AFTER: one SelectorDocument, independent query views share the parse",
        setup_shared,
    )
    assert values_shared == values_separate

    # ---- Object release ----------------------------------------------------
    section("object release")
    gc.collect()
    baseline = lxml_element_count()
    doc = SelectorDocument(text=PAGE)
    views = [doc.selector() for _ in range(10)]
    parsed_once = doc.selector().css(".product")
    assert len(parsed_once) == ITEM_COUNT
    gc.collect()
    print(f"lxml elements with live document: {lxml_element_count() - baseline}")
    doc_ref = weakref.ref(doc)
    del doc
    gc.collect()
    print(f"document alive while 10 query views hold it: {doc_ref() is not None}")
    # The query result also keeps the shared document alive through its view.
    del parsed_once
    gc.collect()
    print(f"document alive while query views remain: {doc_ref() is not None}")
    del views
    gc.collect()
    print(f"document released after last query view goes away: {doc_ref() is None}")
    print(f"lxml elements back to baseline: {lxml_element_count() == baseline}")

    # ---- Mutation isolation (drop must not leak to another query) ---------
    section("drop isolation (copy-on-write)")
    document = SelectorDocument(text=PAGE)
    original_ref = weakref.ref(document)
    reader_a = document.selector()
    reader_b = document.selector()
    before = len(reader_b.css(".product"))
    dropped = reader_a.css(".product")[0]
    dropped.drop()
    after = len(reader_b.css(".product"))
    branch_ref = weakref.ref(reader_a.document)
    branch_view = reader_a.document.selector()
    branch_count = len(branch_view.css(".product"))
    print(f"products before drop: {before}")
    print(f"products seen by the other query after drop: {after}")
    print(f"products seen from the mutating branch: {branch_count}")
    assert after == before
    assert branch_count == before - 1
    del document, reader_a, reader_b, branch_view, dropped
    gc.collect()
    print(f"original document released: {original_ref() is None}")
    print(f"copy-on-write branch released: {branch_ref() is None}")
    print(
        "lxml elements back to baseline after branch release: "
        f"{lxml_element_count() == baseline}"
    )


if __name__ == "__main__":
    main()
