"""Declarative extraction recipes.

A recipe describes an extraction as a sequence of *stages*, each stage using
one of a fixed set of supported operations. Parsel takes care of chaining the
stages across HTML/XML nodes, JSON values and plain strings, so individual
crawlers do not have to repeat the parsing glue.

Recipes are plain data: lists of operation dictionaries. They never contain
Python expressions, only operation names and their arguments, which makes
them safe to load from JSON or YAML configuration.

A typical recipe selects nodes from an HTML page, parses JSON embedded in a
``<script>`` tag, and treats one of the JSON string fields as HTML again::

    recipe = Recipe([
        {"op": "xpath", "query": "//script[@id='products']/text()"},
        {"op": "parse_json"},
        {"op": "jmespath", "query": "items[*]"},
        {
            "op": "field",
            "name": "description",
            "pipeline": [
                {"op": "jmespath", "query": "description_html"},
                {"op": "parse_html"},
                {"op": "css", "query": "p::text"},
                {"op": "text", "kwargs": {"on_missing": "default", "default": ""}},
            ],
        },
    ])
    records = recipe.extract(Selector(text=page))

Every stage declares how the values it produces are combined with the
``expand`` key: ``"flatten"`` (the default) expands lists into separate
values, ``"first"`` keeps a single value, and ``"wrap"`` keeps the whole list
as one value.

Failures never take down the whole extraction. A stage that fails on a value
records an :class:`ExtractionError` carrying the failing stage, the original
input and the value being processed; the other values keep being processed.
The final records expose missing fields together with the reason why they
could not be extracted, so a single bad JSON document never makes every
product disappear.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast

from .selector import Selector, SelectorList

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

ExpandMode: TypeAlias = Literal["flatten", "first", "wrap"]

_EXPAND_MODES = frozenset(("flatten", "first", "wrap"))

_NO_QUERY_OPS = frozenset(("parse_json", "parse_html", "text"))

_SELECTION_OPS = frozenset(("xpath", "css", "jmespath", "jsonpath"))

_EXPANDABLE_OPS = frozenset((*_SELECTION_OPS, "field"))

_SUPPORTED_OPS = frozenset(
    (
        "xpath",
        "css",
        "jmespath",
        "jsonpath",
        "text",
        "parse_json",
        "parse_html",
        "field",
    )
)


class RecipeError(ValueError):
    """Raised when a recipe is malformed or uses an unsupported operation."""


class StageError(RuntimeError):
    """Raised by a stage when it cannot process a value.

    The original exception is available as ``__cause__``.
    """


@dataclass
class ExtractionError:
    """A failed extraction attempt.

    ``stage`` is the 0-based index of the stage that failed within the recipe
    or sub-pipeline, ``operation`` is its operation name, ``value`` is the
    value that was being processed, ``origin`` is a snapshot of the original
    input record the failing value came from, and ``message`` describes the
    failure.
    """

    stage: int
    operation: str
    message: str
    value: Any = None
    origin: Any = None

    def as_dict(self) -> dict[str, Any]:
        """Return a plain-dict representation, safe to serialize as JSON."""
        return {
            "stage": self.stage,
            "operation": self.operation,
            "message": self.message,
            "value": jsonable(self.value),
            "origin": jsonable(self.origin),
        }


@dataclass
class Record:
    """An extracted product record.

    ``data`` holds the fields extracted successfully, ``missing`` maps field
    names to the :class:`ExtractionError` explaining why the field is absent,
    and ``errors`` collects failures of stages outside of field pipelines.
    A record with neither is fully extracted.
    """

    data: dict[str, Any] = field(default_factory=dict)
    missing: dict[str, ExtractionError] = field(default_factory=dict)
    errors: list[ExtractionError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether no extraction error was recorded."""
        return not self.missing and not self.errors

    def as_dict(self) -> dict[str, Any]:
        """Return a plain-dict representation, safe to serialize as JSON."""
        result: dict[str, Any] = {
            key: jsonable(value) for key, value in self.data.items()
        }
        if self.missing:
            result["_missing"] = {
                name: error.as_dict() for name, error in self.missing.items()
            }
        if self.errors:
            result["_errors"] = [error.as_dict() for error in self.errors]
        return result


def jsonable(value: Any) -> Any:
    """Best-effort conversion of an in-flight value into JSON-safe data."""
    if isinstance(value, Selector):
        try:
            return value.get()
        except Exception:  # the representation must never raise
            return repr(value)
    if isinstance(value, SelectorList):
        return [jsonable(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


def materialize(value: Any) -> Any:
    """Resolve in-flight selectors into plain extracted data.

    Selectors are serialized with :meth:`~parsel.selector.Selector.get`, lists
    and dictionaries are resolved recursively, and any other value is returned
    unchanged. Field pipelines end here so that records store plain values
    instead of selector objects.
    """
    if isinstance(value, Selector):
        return value.get()
    if isinstance(value, list):
        return [materialize(item) for item in value]
    if isinstance(value, dict):
        return {key: materialize(item) for key, item in value.items()}
    return value


def _is_stage_mapping(stage: Any) -> bool:
    return isinstance(stage, dict) and isinstance(stage.get("op"), str)


def _copy_record(record: Record) -> Record:
    return Record(
        data=dict(record.data),
        missing=dict(record.missing),
        errors=list(record.errors),
    )


def _validate_expand(stage: dict[str, Any], index: int, operation: str) -> ExpandMode:
    if "expand" in stage and operation not in _EXPANDABLE_OPS:
        msg = (
            f"Stage {index} operation {operation!r} does not support 'expand', "
            f"it always maps one value to one value"
        )
        raise RecipeError(msg)
    expand = stage.get("expand", "flatten")
    if expand not in _EXPAND_MODES:
        msg = (
            f"Unsupported expand mode {expand!r} at stage {index}, expected one "
            f"of {sorted(_EXPAND_MODES)}"
        )
        raise RecipeError(msg)
    return cast("ExpandMode", expand)


def _iter_stages(raw: Any, context: str) -> Iterable[tuple[int, dict[str, Any]]]:
    if not isinstance(raw, list) or not raw:
        msg = f"{context} must be a non-empty list of stage mappings"
        raise RecipeError(msg)
    for index, stage in enumerate(raw):
        if not _is_stage_mapping(stage):
            msg = f"{context} stage {index} must be a mapping with a string 'op'"
            raise RecipeError(msg)
        yield index, stage


class _WrappedValues(list[Any]):
    """A marker list that stays bundled across stages.

    Stages map over its elements instead of fanning them out into separate
    branches, so an explicit ``expand="wrap"`` keeps multi-valued results
    together until they are stored or materialized.
    """


def _expand(results: list[Any], mode: ExpandMode) -> list[Any]:
    flattened: list[Any] = []
    for result in results:
        if isinstance(result, list):
            flattened.extend(result)
        else:
            flattened.append(result)
    if mode == "first":
        return flattened[:1] if flattened else [SelectorList()]
    if mode == "wrap":
        return [_WrappedValues(flattened)]
    return flattened


class _CompiledStage:
    """A validated recipe stage bound to its execution logic."""

    __slots__ = ("expand", "index", "kwargs", "name", "operation", "pipeline", "query")

    def __init__(self, stage: dict[str, Any], index: int) -> None:
        operation = stage["op"]
        if operation not in _SUPPORTED_OPS:
            msg = (
                f"Unsupported operation {operation!r} at stage {index}, "
                f"supported operations: {sorted(_SUPPORTED_OPS)}"
            )
            raise RecipeError(msg)
        self.operation = operation
        self.index = index
        self.name = stage.get("name")
        self.query = stage.get("query")
        self.expand = _validate_expand(stage, index, operation)
        self.kwargs = stage.get("kwargs", {})
        if not isinstance(self.kwargs, dict):
            msg = f"Stage {index} kwargs must be a mapping, got {type(self.kwargs).__name__}"
            raise RecipeError(msg)
        pipeline = stage.get("pipeline")
        if pipeline is None:
            self.pipeline = []
        elif operation != "field":
            msg = f"Stage {index} 'pipeline' is only supported by the 'field' operation"
            raise RecipeError(msg)
        else:
            self.pipeline = [
                _CompiledStage(sub_stage, sub_index)
                for sub_index, sub_stage in _iter_stages(
                    pipeline, f"stage {index} pipeline"
                )
            ]
            if any(sub_stage.operation == "field" for sub_stage in self.pipeline):
                msg = f"Stage {index} field pipelines cannot contain nested 'field' stages"
                raise RecipeError(msg)
        if operation == "field":
            if not isinstance(self.name, str) or not self.name:
                msg = f"Stage {index} field operation requires a non-empty 'name'"
                raise RecipeError(msg)
            if not self.pipeline:
                msg = f"Stage {index} field operation requires a non-empty 'pipeline'"
                raise RecipeError(msg)
        elif self.query is None and operation not in _NO_QUERY_OPS:
            msg = f"Stage {index} operation {operation!r} requires a 'query'"
            raise RecipeError(msg)
        if self.name is not None and operation != "field":
            msg = f"Stage {index} 'name' is only supported by the 'field' operation"
            raise RecipeError(msg)
        if operation == "text":
            self._validate_text_kwargs()
        if operation == "parse_html":
            parser_type = self.kwargs.get("type", "html")
            if parser_type not in ("html", "xml"):
                msg = (
                    f"Stage {index} parse_html type must be 'html' or 'xml', "
                    f"got {parser_type!r}"
                )
                raise RecipeError(msg)

    def _validate_text_kwargs(self) -> None:
        mode = self.kwargs.get("on_missing", "keep")
        if mode not in ("keep", "drop", "default"):
            msg = (
                f"Stage {self.index} text on_missing must be 'keep', 'drop' or "
                f"'default', got {mode!r}"
            )
            raise RecipeError(msg)
        if mode == "default" and "default" not in self.kwargs:
            msg = f"Stage {self.index} text on_missing='default' requires a 'default'"
            raise RecipeError(msg)

    def apply_one(self, value: Any) -> list[Any]:
        """Apply the stage to one value, returning zero or more results."""
        if self.operation in _SELECTION_OPS:
            return self._apply_selection(value)
        if self.operation == "text":
            return self._apply_text(value)
        if self.operation == "parse_json":
            return [_parse_json(value)]
        if self.operation == "parse_html":
            parser_type = self.kwargs.get("type", "html")
            return [_parse_html(value, parser_type)]
        msg = f"operation {self.operation!r} is handled by the recipe runner"
        raise StageError(msg)

    def _apply_selection(self, value: Any) -> list[Any]:
        if not isinstance(value, Selector):
            if isinstance(value, SelectorList):
                method = getattr(value, self.operation)
                return _expand([method(self.query, **self.kwargs)], self.expand)
            if self.operation in ("jmespath", "jsonpath") and isinstance(
                value, (dict, list)
            ):
                value = Selector(root=value)
            else:
                msg = (
                    f"operation {self.operation!r} expects a selector, got "
                    f"{type(value).__name__}"
                )
                raise StageError(msg)
        method = getattr(value, self.operation)
        return _expand([method(self.query, **self.kwargs)], self.expand)

    def _apply_text(self, value: Any) -> list[Any]:
        if isinstance(value, SelectorList) and not value:
            text = None
        elif isinstance(value, Selector):
            text = value.get()
        else:
            msg = f"operation 'text' expects a selector, got {type(value).__name__}"
            raise StageError(msg)
        if text is None:
            mode = self.kwargs.get("on_missing", "keep")
            if mode == "drop":
                return []
            if mode == "default":
                text = self.kwargs.get("default")
        return [text]


def _parse_json(value: Any) -> Any:
    text = value.get() if isinstance(value, Selector) else value
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        msg = f"operation 'parse_json' expects text, got {type(text).__name__}"
        raise StageError(msg)
    try:
        return json.loads(text)
    except (TypeError, ValueError) as error:
        raise StageError(f"invalid JSON: {error}") from error


def _parse_html(value: Any, parser_type: str) -> Selector:
    if isinstance(value, Selector) and value.type == parser_type:
        return value
    text = value.get() if isinstance(value, Selector) else value
    if not isinstance(text, str):
        msg = f"operation 'parse_html' expects text, got {type(text).__name__}"
        raise StageError(msg)
    return Selector(text=text, type=parser_type)


class _PipelineFailure(Exception):
    """Internal signal carrying the error of a failed sub-pipeline."""

    def __init__(self, error: ExtractionError) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass
class _Branch:
    """An in-flight extraction branch.

    ``value`` is the current stage output, ``origin`` is a JSON-safe snapshot
    of the input the branch started from, and ``record`` accumulates fields.
    """

    value: Any
    origin: Any
    record: Record


def _run_pipeline(
    stages: Sequence[_CompiledStage], value: Any, origin: Any
) -> list[Any]:
    current = [value]
    for stage in stages:
        next_values: list[Any] = []
        for item in current:
            try:
                next_values.extend(stage.apply_one(item))
            except StageError as error:
                raise _PipelineFailure(
                    ExtractionError(
                        stage=stage.index,
                        operation=stage.operation,
                        message=str(error),
                        value=jsonable(item),
                        origin=origin,
                    )
                ) from error
        current = next_values
    return current


class Recipe:
    """A validated, reusable declarative extraction recipe.

    A recipe is a list of stage mappings. Each stage mapping has an ``op``
    key naming one of the supported operations, an optional ``query``, an
    optional ``expand`` mode (``"flatten"`` by default; also ``"first"`` and
    ``"wrap"``), and operation-specific ``kwargs``. The ``field`` operation
    additionally takes a ``name`` and a nested ``pipeline``.

    Supported operations:

    - ``xpath``, ``css``: select nodes from an HTML/XML selector.
    - ``jmespath``, ``jsonpath``: select values from a JSON selector.
    - ``text``: serialize a selector to text. Supports the ``on_missing``
      keyword with ``"keep"`` (default), ``"drop"`` or ``"default"`` together
      with a ``default`` value.
    - ``parse_json``: parse a text value as JSON.
    - ``parse_html``: parse a text value as HTML (or XML with
      ``kwargs={"type": "xml"}``).
    - ``field``: run a sub-pipeline and store the result in a record field.

    An invalid recipe raises :class:`RecipeError` at construction time, before
    any input is processed. Recipes only call the operations listed above;
    arbitrary Python expressions are not accepted.
    """

    def __init__(self, stages: list[dict[str, Any]]) -> None:
        self.stages: list[_CompiledStage] = []
        for index, stage in _iter_stages(stages, "recipe"):
            self.stages.append(_CompiledStage(stage, index))

    def extract(
        self,
        source: str | Selector,
        type: str | None = None,  # noqa: A002
    ) -> list[Record]:
        """Run the recipe against *source* and return extracted records.

        *source* is an HTML/XML/JSON string or a ready-made
        :class:`~parsel.selector.Selector`. When *source* is a string, *type*
        is forwarded to :class:`~parsel.selector.Selector` to force
        ``"html"``, ``"xml"``, ``"json"`` or ``"text"`` parsing instead of
        relying on auto-detection. One :class:`Record` is returned per value
        that reaches the end of the recipe, plus one record per branch that
        failed along the way, carrying the failing stage and the original
        input. A single bad value therefore never discards the other products.
        """
        if isinstance(source, str):
            selector = (
                Selector(text=source, type=type)
                if type is not None
                else Selector(text=source)
            )
            origin: Any = source
        else:
            selector = source
            origin = jsonable(selector)
        branches = [_Branch(value=selector, origin=origin, record=Record())]
        failed_records: list[Record] = []
        for stage in self.stages:
            branches = self._run_stage(stage, branches, failed_records)
        records = [self._finalize(branch) for branch in branches]
        records.extend(failed_records)
        return records

    def extract_dicts(self, source: str | Selector) -> list[dict[str, Any]]:
        """Run :meth:`extract` and return JSON-serializable dictionaries."""
        return [record.as_dict() for record in self.extract(source)]

    def _run_stage(
        self,
        stage: _CompiledStage,
        branches: list[_Branch],
        failed_records: list[Record],
    ) -> list[_Branch]:
        next_branches: list[_Branch] = []
        for branch in branches:
            if stage.operation == "field":
                self._apply_field(stage, branch)
                next_branches.append(branch)
                continue
            if isinstance(branch.value, _WrappedValues):
                wrapped = self._apply_to_wrapped(stage, branch, failed_records)
                if wrapped is not None:
                    next_branches.append(
                        _Branch(
                            value=wrapped,
                            origin=branch.origin,
                            record=branch.record,
                        )
                    )
                continue
            try:
                results = stage.apply_one(branch.value)
            except StageError as error:
                branch.record.errors.append(
                    ExtractionError(
                        stage=stage.index,
                        operation=stage.operation,
                        message=str(error),
                        value=jsonable(branch.value),
                        origin=branch.origin,
                    )
                )
                failed_records.append(branch.record)
                continue
            next_branches.extend(
                _Branch(
                    value=result,
                    origin=branch.origin,
                    record=_copy_record(branch.record),
                )
                for result in results
            )
        return next_branches

    def _apply_to_wrapped(
        self,
        stage: _CompiledStage,
        branch: _Branch,
        failed_records: list[Record],
    ) -> _WrappedValues | None:
        collected = _WrappedValues()
        for item in branch.value:
            try:
                collected.extend(stage.apply_one(item))
            except StageError as error:
                branch.record.errors.append(
                    ExtractionError(
                        stage=stage.index,
                        operation=stage.operation,
                        message=str(error),
                        value=jsonable(item),
                        origin=branch.origin,
                    )
                )
                failed_records.append(_copy_record(branch.record))
                return None
        return collected

    def _apply_field(self, stage: _CompiledStage, branch: _Branch) -> None:
        name = cast("str", stage.name)
        try:
            values = _run_pipeline(stage.pipeline, branch.value, branch.origin)
        except _PipelineFailure as failure:
            branch.record.missing[name] = failure.error
            return
        if not values:
            branch.record.missing[name] = ExtractionError(
                stage=stage.index,
                operation=stage.operation,
                message="pipeline produced no value",
                value=jsonable(branch.value),
                origin=branch.origin,
            )
            return
        if stage.expand == "first":
            stored = materialize(values[0])
        else:
            stored = materialize(values)
        branch.record.data[name] = stored

    def _finalize(self, branch: _Branch) -> Record:
        if branch.record.data or branch.record.missing or branch.record.errors:
            return branch.record
        return Record(data={"_value": materialize(branch.value)})
