"""Tests for the restricted XPath entry point in :mod:`parsel.limited`."""

from __future__ import annotations

import threading
import time
import typing
import warnings

import psutil
import pytest
from lxml import etree

from parsel import (
    QueryEvaluationError,
    QueryInputSizeLimitError,
    QueryResultLimitError,
    QueryTimeoutError,
    Selector,
    run_limited_xpath,
)

if typing.TYPE_CHECKING:
    from psutil import Process

# A 40k-node document. Repeated descendant unions make libxml2 evaluate the
# same traversal over and over, so the query takes several seconds while
# staying a perfectly valid XPath expression.
SECTIONS = 200
ITEMS_PER_SECTION = 100
LONG_UNIONS = 80
TIMEOUT = 1.0

LARGE_HTML = (
    "<html><body><root>"
    + "".join(
        "<section>" + "<item><x>v</x></item>" * ITEMS_PER_SECTION + "</section>"
        for _ in range(SECTIONS)
    )
    + "</root></body></html>"
)
LONG_QUERY = " | ".join(["//item"] * LONG_UNIONS)


@pytest.fixture(scope="session", autouse=True)
def _warm_up_spawn() -> None:
    # Start the spawn resource tracker once so per-test child process checks
    # only see the workers created by the test itself.
    run_limited_xpath(Selector(text="<r/>"), "/r", max_execution_time=10)


def _current_children() -> set[int]:
    return {process.pid for process in psutil.Process().children(recursive=True)}


def _wait_for_extra_children(
    baseline: set[int], count: int = 1, timeout: float = 10.0
) -> list[Process]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        extra = [
            process
            for process in psutil.Process().children(recursive=True)
            if process.pid not in baseline
        ]
        running = [process for process in extra if process.is_running()]
        if len(running) >= count:
            return running
        time.sleep(0.02)
    pytest.fail(f"only {len(extra)} of {count} expected worker processes started")


def _wait_for_children_reaped(baseline: set[int], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        extra = [
            process
            for process in psutil.Process().children(recursive=True)
            if process.pid not in baseline
        ]
        if not extra:
            return
        time.sleep(0.02)
    pytest.fail(f"worker processes were not reaped: {extra}")


EQUIVALENCE_CASES = [
    ("//item",),
    ("//item/x",),
    ("//item/x/text()",),
    ("//section[2]//item/@class",),
    ("//section[1]/item[position() < 3]",),
    ("count(//item)",),
    ("boolean(//section)",),
    ("string(//item[1]/x)",),
    ("//comment()",),
    ("//processing-instruction()",),
    ("//missing",),
]


@pytest.mark.parametrize("query", [case[0] for case in EQUIVALENCE_CASES])
def test_results_match_regular_xpath(query: str) -> None:
    text = (
        "<html><body><!-- a comment --><?pi foo?>"
        "<section><item class='a'><x>1</x></item>"
        "<item class='b'><x>2</x></item></section>"
        "<section><item class='a'><x>3</x></item></section>"
        "</body></html>"
    )
    selector = Selector(text=text)
    limited = run_limited_xpath(
        selector, query, max_execution_time=10, max_results=10_000
    )
    regular = selector.xpath(query)
    assert type(limited) is type(regular)
    assert limited.getall() == regular.getall()
    for limited_item, regular_item in zip(limited, regular, strict=True):
        assert type(limited_item) is type(regular_item)
        if isinstance(regular_item.root, etree._Element):
            # Node results are re-attached to the selector's own tree.
            assert limited_item.root is regular_item.root
        else:
            assert limited_item.root == regular_item.root
        assert limited_item.type == regular_item.type
        assert limited_item._expr == query


def test_chained_queries_match_regular_entry_point() -> None:
    selector = Selector(text=LARGE_HTML)
    limited = run_limited_xpath(
        selector, "//section", max_execution_time=10, max_results=10_000
    )
    regular = selector.xpath("//section")
    assert (
        limited[5].xpath(".//item[1]/x/text()").getall()
        == regular[5].xpath(".//item[1]/x/text()").getall()
    )
    assert limited.css("item").getall() == regular.css("item").getall()


def test_nested_selector_anchor_matches() -> None:
    selector = Selector(text=LARGE_HTML)
    nested = selector.xpath("//section[3]")[0]
    limited = run_limited_xpath(
        nested, ".//item", max_execution_time=10, max_results=10_000
    )
    assert limited.getall() == nested.xpath(".//item").getall()
    assert limited[0].root in list(selector.root.iter())


def test_absolute_xpath_warning_matches_entry_point() -> None:
    nested = Selector(text=LARGE_HTML).xpath("//section[3]")[0]
    with warnings.catch_warnings(record=True) as limited_warnings:
        warnings.simplefilter("always")
        run_limited_xpath(nested, "//section[1]", max_execution_time=10)
    with warnings.catch_warnings(record=True) as regular_warnings:
        warnings.simplefilter("always")
        nested.xpath("//section[1]")
    assert [type(item.message) for item in limited_warnings] == [
        type(item.message) for item in regular_warnings
    ]


def test_namespaces_and_variables_match() -> None:
    selector = Selector(
        text='<r xmlns:p="urn:x"><p:a b="1"/><p:a b="2"/></r>', type="xml"
    )
    limited = run_limited_xpath(
        selector,
        "//p:a[@b=$value]",
        namespaces={"p": "urn:x"},
        value="2",
        max_execution_time=10,
    )
    assert (
        limited.getall()
        == selector.xpath(
            "//p:a[@b=$value]", namespaces={"p": "urn:x"}, value="2"
        ).getall()
    )


def test_text_selector_matches_entry_point() -> None:
    selector = Selector(text="<p>hello <b>world</b></p>", type="text")
    limited = run_limited_xpath(selector, "//p//text()", max_execution_time=10)
    assert limited.getall() == selector.xpath("//p//text()").getall()


def test_json_selector_rejected() -> None:
    selector = Selector(text='{"a": 1}')
    with pytest.raises(ValueError, match="Cannot use xpath"):
        run_limited_xpath(selector, "//a")


def test_unpicklable_variable_rejected_without_worker() -> None:
    selector = Selector(text="<r><a/></r>")
    baseline = _current_children()
    with pytest.raises(ValueError, match="picklable"):
        run_limited_xpath(selector, "//a[@b=$value]", value=lambda: None)
    assert _current_children() == baseline


def test_invalid_xpath_raises_evaluation_error() -> None:
    selector = Selector(text="<r/>")
    with pytest.raises(QueryEvaluationError) as exc_info:
        run_limited_xpath(selector, "//[", max_execution_time=10)
    assert "XPath query failed" in str(exc_info.value)
    assert exc_info.value.original_error


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_execution_time": 0}, "max_execution_time"),
        ({"max_execution_time": -1}, "max_execution_time"),
        ({"max_results": 0}, "max_results"),
        ({"max_input_size": 0}, "max_input_size"),
    ],
)
def test_invalid_limits_rejected_up_front(
    kwargs: dict[str, object], match: str
) -> None:
    selector = Selector(text="<r/>")
    baseline = _current_children()
    with pytest.raises(ValueError, match=match):
        run_limited_xpath(selector, "/r", **kwargs)  # type: ignore[arg-type]
    assert _current_children() == baseline


def test_max_results_enforced() -> None:
    selector = Selector(text=LARGE_HTML)
    with pytest.raises(QueryResultLimitError) as exc_info:
        run_limited_xpath(selector, "//item", max_results=10, max_execution_time=10)
    assert exc_info.value.result_count == SECTIONS * ITEMS_PER_SECTION
    assert exc_info.value.limit == 10


def test_max_input_size_rejected_up_front() -> None:
    selector = Selector(text=LARGE_HTML)
    baseline = _current_children()
    with pytest.raises(QueryInputSizeLimitError) as exc_info:
        run_limited_xpath(selector, "//item", max_input_size=100)
    assert exc_info.value.input_size > 100
    assert exc_info.value.limit == 100
    # A pre-flight rejection must not spawn a worker.
    assert _current_children() == baseline


def _run_long_query(selector: Selector) -> None:
    with pytest.raises(QueryTimeoutError) as exc_info:
        run_limited_xpath(
            selector, LONG_QUERY, max_execution_time=TIMEOUT, max_results=100_000
        )
    assert exc_info.value.limit == TIMEOUT


def test_timeout_hard_terminates_worker_while_normal_queries_keep_running() -> None:
    selector = Selector(text=LARGE_HTML)
    baseline = _current_children()
    thread = threading.Thread(target=_run_long_query, args=(selector,), daemon=True)
    start = time.monotonic()
    thread.start()

    # Observe the worker actually running before the deadline: this is a live
    # check of the running process, not a timer inspected after the fact.
    workers = _wait_for_extra_children(baseline)
    assert all(process.is_running() for process in workers)

    # In-process queries stay responsive while the long query burns a CPU.
    parallel_started = time.monotonic()
    assert selector.xpath("//section[1]/item[1]/x/text()").getall() == ["v"]
    direct_duration = time.monotonic() - parallel_started
    assert direct_duration < 1.0

    # A second limited query gets its own worker and succeeds concurrently.
    parallel_limited = run_limited_xpath(
        selector, "//section[1]/item[1]", max_execution_time=10
    )
    assert parallel_limited.getall() == selector.xpath("//section[1]/item[1]").getall()

    thread.join()
    elapsed = time.monotonic() - start
    assert elapsed >= TIMEOUT
    # The kill grace is the only legitimate extra wait beyond the deadline.
    assert elapsed < TIMEOUT + 4.0
    _wait_for_children_reaped(baseline)

    # The same document still serves correct results afterwards.
    assert (
        run_limited_xpath(selector, "count(//item)", max_execution_time=10).getall()
        == selector.xpath("count(//item)").getall()
    )


def test_concurrent_long_queries_terminate_independently() -> None:
    selector = Selector(text=LARGE_HTML)
    baseline = _current_children()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            run_limited_xpath(
                selector,
                LONG_QUERY,
                max_execution_time=TIMEOUT,
                max_results=100_000,
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    workers = _wait_for_extra_children(baseline, count=3)
    assert len(workers) == 3
    for thread in threads:
        thread.join()
    assert len(errors) == 3
    assert all(isinstance(error, QueryTimeoutError) for error in errors)
    _wait_for_children_reaped(baseline)


def test_completing_query_is_not_killed() -> None:
    selector = Selector(text=LARGE_HTML)
    start = time.monotonic()
    result = run_limited_xpath(
        selector,
        "//section[1]/item",
        max_execution_time=10,
        max_results=10_000,
    )
    assert len(result) == ITEMS_PER_SECTION
    assert time.monotonic() - start < 5.0
