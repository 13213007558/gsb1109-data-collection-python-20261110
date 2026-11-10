"""Restricted XPath entry point with enforceable execution limits.

The regular :meth:`parsel.Selector.xpath` entry point runs XPath expressions
in the calling process without any execution time or result size limits. A
single complex expression on a large document can keep a CPU core busy for
arbitrarily long.

:func:`run_limited_xpath` evaluates an XPath expression in a dedicated worker
process, which makes it possible to enforce a wall-clock execution time by
hard-terminating the worker. Each call uses its own process, so terminating
one query never affects other queries, whether they run in threads or in other
processes, and successful queries return the same selectors as
:meth:`~parsel.Selector.xpath`.

Limit guarantees
================

The limits fall into two categories.

Strictly enforced
-----------------

* ``max_execution_time`` is enforced against the wall clock of the whole call
  (worker startup, parsing and evaluation included). On expiry the worker
  process is terminated with ``SIGTERM`` and, if needed, ``SIGKILL``
  (``TerminateProcess`` on Windows), and the call raises
  :class:`QueryTimeoutError`. No worker process or thread is left running.
* Isolation between queries is strict: every call spawns a separate process
  and the termination of one process cannot affect any other query.
* ``max_results`` is checked against the full result set produced by the
  expression before anything is returned. Exceeding it raises
  :class:`QueryResultLimitError`.
* The serialized document sent to the worker never exceeds
  ``max_input_size``.

Pre-flight rejection only
-------------------------

* ``max_input_size`` measures the serialized size of the input tree. It is a
  cheap pre-check, not a memory cap: parsing and evaluating a document below
  the size limit can still allocate substantial memory. The execution time
  limit remains the backstop for such cases.
* ``max_results`` only bounds the final result sequence returned by the
  expression. Intermediate node sets computed internally by libxml2 while
  evaluating the expression are not bounded; again, the execution time limit
  is the backstop.
* The length or complexity of the XPath expression itself is not limited.
"""

from __future__ import annotations

import multiprocessing
import pickle
import time
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lxml import etree

from .selector import _ctgroup, _get_xpath_evaluator, _xml_or_html, create_root_node

if TYPE_CHECKING:
    from collections.abc import Mapping
    from multiprocessing.connection import Connection

    from .selector import Selector, SelectorList

#: Default maximum wall-clock duration of a limited query, in seconds.
DEFAULT_MAX_EXECUTION_TIME = 5.0
#: Default maximum number of items in the returned result list.
DEFAULT_MAX_RESULTS = 1000
#: Default maximum serialized input size, in bytes (10 MiB).
DEFAULT_MAX_INPUT_SIZE = 10 * 1024**2

#: Time given to a terminated worker process to exit before it is killed.
_PROCESS_KILL_GRACE = 1.0

# Worker outcome kinds.
_OUTCOME_OK = "ok"
_OUTCOME_XPATH_ERROR = "xpath-error"
_OUTCOME_RESULT_LIMIT = "result-limit"
_OUTCOME_INTERNAL_ERROR = "internal-error"

# Encoded result item kinds.
_ITEM_ELEMENT = "element"
_ITEM_SCALAR = "scalar"


class LimitedQueryError(Exception):
    """Base class for every failure raised by :func:`run_limited_xpath`."""


class QueryTimeoutError(LimitedQueryError):
    """Raised when a query does not finish within ``max_execution_time``.

    The worker process running the query has already been terminated and
    reaped when this exception is raised.
    """

    def __init__(self, execution_time: float, limit: float) -> None:
        self.execution_time = execution_time
        self.limit = limit
        super().__init__(
            f"XPath query exceeded the maximum execution time of {limit} "
            f"seconds and was terminated after {execution_time:.3f} seconds"
        )


class QueryResultLimitError(LimitedQueryError):
    """Raised when a query returns more than ``max_results`` items."""

    def __init__(self, result_count: int, limit: int) -> None:
        self.result_count = result_count
        self.limit = limit
        super().__init__(
            f"XPath query returned {result_count} items, above the maximum of {limit}"
        )


class QueryInputSizeLimitError(LimitedQueryError):
    """Raised when the serialized input exceeds ``max_input_size`` bytes."""

    def __init__(self, input_size: int, limit: int) -> None:
        self.input_size = input_size
        self.limit = limit
        super().__init__(
            f"XPath input is {input_size} bytes, above the maximum of {limit} bytes"
        )


class QueryEvaluationError(LimitedQueryError):
    """Raised when the worker fails to evaluate the XPath expression.

    The original evaluation error message, typically an
    :class:`lxml.etree.XPathError`, is attached as :attr:`original_error`.
    """

    def __init__(self, message: str) -> None:
        self.original_error = message
        super().__init__(f"XPath query failed: {message}")


@dataclass(frozen=True)
class _LimitedXPathJob:
    tree_bytes: bytes
    parser_type: str
    anchor_path: tuple[int, ...]
    query: str
    namespaces: dict[str, str]
    variables: dict[str, Any]
    max_results: int


@dataclass(frozen=True)
class _WorkerOutcome:
    kind: str
    items: list[tuple[str, Any]] = field(default_factory=list)
    result_count: int = 0
    limit: int = 0
    message: str = ""


def _node_index_path(root: etree._Element, node: etree._Element) -> tuple[int, ...]:
    """Return the child indexes leading from *root* to *node*.

    Indexes count every child node, including comments and processing
    instructions, matching lxml's element indexing.
    """
    indexes: list[int] = []
    current = node
    while current is not root:
        parent = current.getparent()
        if parent is None:
            msg = "Result node does not belong to the queried document"
            raise ValueError(msg)
        indexes.append(list(parent).index(current))
        current = parent
    return tuple(reversed(indexes))


def _resolve_index_path(root: etree._Element, path: tuple[int, ...]) -> etree._Element:
    node = root
    for index in path:
        node = node[index]
    return node


def _top_root(node: etree._Element) -> etree._Element:
    top = node
    while top.getparent() is not None:
        top = top.getparent()
    return top


def _encode_result_item(item: Any, root: etree._Element) -> tuple[str, Any]:
    if isinstance(item, etree._Element):
        return _ITEM_ELEMENT, _node_index_path(root, item)
    return _ITEM_SCALAR, item


def _run_xpath_worker(connection: Connection, job: _LimitedXPathJob) -> None:
    """Evaluate the query in the worker process and send back an outcome."""
    try:
        parser_cls = _ctgroup[job.parser_type]["_parser"]
        document_root = create_root_node(
            "",
            body=job.tree_bytes,
            parser_cls=parser_cls,
            encoding="utf-8",
        )
        anchor = _resolve_index_path(document_root, job.anchor_path)
        evaluator = _get_xpath_evaluator(job.query, job.namespaces, smart_strings=False)
        result = evaluator(anchor, **job.variables)
        if not isinstance(result, list):
            result = [result]
        if len(result) > job.max_results:
            connection.send(
                _WorkerOutcome(
                    kind=_OUTCOME_RESULT_LIMIT,
                    result_count=len(result),
                    limit=job.max_results,
                )
            )
            return
        items = [_encode_result_item(item, document_root) for item in result]
        connection.send(_WorkerOutcome(kind=_OUTCOME_OK, items=items))
    except etree.XPathError as exc:
        connection.send(_WorkerOutcome(kind=_OUTCOME_XPATH_ERROR, message=str(exc)))
    except Exception:
        connection.send(
            _WorkerOutcome(kind=_OUTCOME_INTERNAL_ERROR, message=traceback.format_exc())
        )


@dataclass(frozen=True)
class _PreparedInput:
    tree_bytes: bytes
    parser_type: str
    anchor_path: tuple[int, ...]
    resolution_root: etree._Element


def _prepare_input(selector: Selector) -> _PreparedInput:
    if selector.type in ("html", "xml"):
        anchor = selector.root
        if not isinstance(anchor, etree._Element):
            msg = f"Cannot use xpath on a Selector of type {selector.type!r}"
            raise ValueError(msg)
        document_root = _top_root(anchor)
        anchor_path = _node_index_path(document_root, anchor)
        tree_bytes = etree.tostring(
            document_root,
            method=_ctgroup[selector.type]["_tostring_method"],
            encoding="utf-8",
        )
        return _PreparedInput(
            tree_bytes=tree_bytes,
            parser_type=selector.type,
            anchor_path=anchor_path,
            resolution_root=document_root,
        )
    if selector.type == "text":
        if not isinstance(selector.root, str):
            msg = f"Cannot use xpath on a Selector of type {selector.type!r}"
            raise ValueError(msg)
        # Matches Selector._xpath: text selectors are parsed as HTML per call.
        document_root = selector._get_root(selector._text or "", type_="html")
        tree_bytes = etree.tostring(document_root, method="html", encoding="utf-8")
        return _PreparedInput(
            tree_bytes=tree_bytes,
            parser_type="html",
            anchor_path=(),
            resolution_root=document_root,
        )
    msg = f"Cannot use xpath on a Selector of type {selector.type!r}"
    raise ValueError(msg)


def _build_result_selectors(
    selector: Selector,
    query: str,
    items: list[tuple[str, Any]],
    resolution_root: etree._Element,
) -> SelectorList[Selector]:
    result: list[Selector] = []
    for kind, payload in items:
        if kind == _ITEM_ELEMENT:
            node = _resolve_index_path(resolution_root, payload)
        else:
            node = payload
        result.append(
            selector.__class__(
                root=node,
                _expr=query,
                namespaces=selector.namespaces,
                type=_xml_or_html(selector.type),
            )
        )
    return selector.selectorlist_cls(result)


def _terminate(process: multiprocessing.process.BaseProcess) -> None:
    process.terminate()
    process.join(_PROCESS_KILL_GRACE)
    if process.is_alive():
        process.kill()
        process.join()


def run_limited_xpath(
    selector: Selector,
    query: str,
    *,
    max_execution_time: float = DEFAULT_MAX_EXECUTION_TIME,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_input_size: int = DEFAULT_MAX_INPUT_SIZE,
    namespaces: Mapping[str, str] | None = None,
    **variables: Any,
) -> SelectorList[Selector]:
    """Run *query* on *selector* inside a dedicated process with hard limits.

    The return value is the same :class:`~parsel.SelectorList` that
    :meth:`parsel.Selector.xpath` would return for the same arguments: node
    results are re-attached to the selector's own document tree, so nested
    selectors, :meth:`~parsel.Selector.get` and further queries behave
    identically.

    ``query`` is a string containing the XPath expression to evaluate.

    ``max_execution_time`` is the wall-clock budget in seconds for the whole
    call, including worker startup. On expiry the worker process is
    hard-terminated and :class:`QueryTimeoutError` is raised.

    ``max_results`` is the maximum number of items the expression may return;
    exceeding it raises :class:`QueryResultLimitError`.

    ``max_input_size`` is the maximum serialized size in bytes of the tree
    sent to the worker. Larger inputs are rejected up front with
    :class:`QueryInputSizeLimitError` and no worker is spawned.

    ``namespaces`` is an optional ``prefix: namespace-uri`` mapping merged on
    top of the selector namespaces, like in :meth:`~parsel.Selector.xpath`.

    Any additional named arguments are used as values for XPath variables in
    the expression, e.g.::

        run_limited_xpath(selector, '//a[href=$url]', url="http://example.com")

    Raises :class:`QueryTimeoutError`, :class:`QueryResultLimitError`,
    :class:`QueryInputSizeLimitError`, :class:`QueryEvaluationError`, or
    :class:`ValueError` when a limit is non-positive or XPath is not
    supported by the selector type.
    """
    if max_execution_time <= 0:
        msg = "max_execution_time must be strictly positive"
        raise ValueError(msg)
    if max_results <= 0:
        msg = "max_results must be strictly positive"
        raise ValueError(msg)
    if max_input_size <= 0:
        msg = "max_input_size must be strictly positive"
        raise ValueError(msg)

    if query[:1] == "/":
        selector._warn_if_nested(query, stacklevel=2)

    prepared = _prepare_input(selector)
    input_size = len(prepared.tree_bytes)
    if input_size > max_input_size:
        raise QueryInputSizeLimitError(input_size, max_input_size)

    merged_namespaces = dict(selector.namespaces)
    if namespaces is not None:
        merged_namespaces.update(namespaces)

    # The job is sent to a spawned worker through a pipe, so every argument
    # must be picklable. Fail with a clear error instead of the low-level
    # spawn failure. Local functions surface as AttributeError here.
    try:
        pickle.dumps(variables)
    except (TypeError, AttributeError, pickle.PicklingError) as exc:
        raise ValueError(
            f"XPath variables must be picklable to run in a worker process: {exc}"
        ) from exc

    job = _LimitedXPathJob(
        tree_bytes=prepared.tree_bytes,
        parser_type=prepared.parser_type,
        anchor_path=prepared.anchor_path,
        query=query,
        namespaces=merged_namespaces,
        variables=variables,
        max_results=max_results,
    )

    # The spawn context is used on every platform so that the worker is a
    # fresh process that can be hard-terminated without taking the caller down.
    context = multiprocessing.get_context("spawn")
    parent_conn, child_conn = context.Pipe(duplex=False)
    process = context.Process(
        target=_run_xpath_worker,
        args=(child_conn, job),
        daemon=True,
    )
    start = time.monotonic()
    try:
        process.start()
    except BaseException:
        child_conn.close()
        parent_conn.close()
        process.close()
        raise
    child_conn.close()
    try:
        remaining = max_execution_time - (time.monotonic() - start)
        if remaining <= 0 or not parent_conn.poll(remaining):
            # poll() returning False means the deadline elapsed: a crashed
            # worker closes the pipe instead, which poll() reports as
            # readable and recv() raises EOFError on.
            elapsed = time.monotonic() - start
            _terminate(process)
            raise QueryTimeoutError(elapsed, max_execution_time)
        try:
            outcome = parent_conn.recv()
        except EOFError as exc:
            _terminate(process)
            raise QueryEvaluationError(
                "the worker process terminated without sending a result"
            ) from exc
        # The worker exits right after sending, but do not block forever if a
        # custom build misbehaves: the finally block reaps anything alive.
        process.join(_PROCESS_KILL_GRACE)
    finally:
        if process.is_alive():
            _terminate(process)
        process.close()
        parent_conn.close()

    if outcome.kind == _OUTCOME_OK:
        return _build_result_selectors(
            selector, query, outcome.items, prepared.resolution_root
        )
    if outcome.kind == _OUTCOME_RESULT_LIMIT:
        raise QueryResultLimitError(outcome.result_count, outcome.limit)
    if outcome.kind == _OUTCOME_XPATH_ERROR:
        raise QueryEvaluationError(outcome.message)
    raise QueryEvaluationError(outcome.message)
