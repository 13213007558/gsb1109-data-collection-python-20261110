"""Row-scoped extraction: declare repeating records, then fields per record.

Extracting a title list and a price list separately and zipping them
together misaligns as soon as one record misses a field. :class:`RowExtractor`
instead selects the repeating records first and then extracts every field
inside the scope of a single record, so a missing or duplicated value is
reported against the exact record that broke the contract instead of
shifting every following row.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias, overload

from lxml import etree

from .selector import Selector, SelectorList

__all__ = [
    "Cardinality",
    "Field",
    "FieldError",
    "MissingValue",
    "MultipleMatches",
    "Row",
    "RowExtractor",
    "RowReport",
    "Violation",
    "many",
    "optional",
    "required",
]


class Cardinality(Enum):
    """How many times a field is allowed to match inside one record."""

    ONE = "one"
    OPTIONAL = "optional"
    MANY = "many"


@dataclass(frozen=True)
class Field:
    """Declaration of a single field inside a record.

    ``query`` is a ``"css:..."`` or ``"xpath:..."`` prefixed selector
    (an unprefixed query is treated as CSS), evaluated inside the scope
    of one record.
    """

    query: str
    cardinality: Cardinality = Cardinality.ONE


def required(query: str) -> Field:
    """Declare a field that must match exactly once per record."""
    return Field(query, Cardinality.ONE)


def optional(query: str) -> Field:
    """Declare a field that may be absent but must not match twice."""
    return Field(query, Cardinality.OPTIONAL)


def many(query: str) -> Field:
    """Declare a field that may match any number of times, including zero."""
    return Field(query, Cardinality.MANY)


class FieldError:
    """Base class for per-field extraction contract violations."""

    query: str


@dataclass(frozen=True)
class MissingValue(FieldError):
    """A required field matched nothing inside the record.

    Distinct from an empty string: an attribute or text node that is
    present but empty extracts as ``""``, while ``MissingValue`` means
    the selector matched no node at all.
    """

    query: str


@dataclass(frozen=True)
class MultipleMatches(FieldError):
    """A single-valued field matched more than once inside the record."""

    query: str
    count: int
    values: tuple[str, ...]


#: What a field can extract to: a string (possibly empty), ``None`` for an
#: absent optional field, a list for ``many`` fields, or a contract error.
FieldValue: TypeAlias = str | list[str] | None | MissingValue | MultipleMatches


@dataclass
class Row:
    """Fields extracted from a single record, keyed by field name."""

    index: int
    fields: dict[str, FieldValue]

    @property
    def errors(self) -> dict[str, FieldError]:
        """Fields whose extraction violated their declared cardinality."""
        return {
            name: value
            for name, value in self.fields.items()
            if isinstance(value, FieldError)
        }

    @property
    def ok(self) -> bool:
        """Whether every field of this record satisfied its contract."""
        return not self.errors


@dataclass(frozen=True)
class Violation:
    """Locates a contract violation: which record, which field, what happened."""

    row_index: int
    field: str
    error: FieldError


class RowReport(Sequence[Row]):
    """Ordered extraction results plus a flat view of all violations."""

    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows

    def __len__(self) -> int:
        return len(self.rows)

    @overload
    def __getitem__(self, index: int) -> Row: ...

    @overload
    def __getitem__(self, index: slice) -> list[Row]: ...

    def __getitem__(self, index: int | slice) -> Row | list[Row]:
        return self.rows[index]

    def __iter__(self) -> Iterator[Row]:
        return iter(self.rows)

    @property
    def violations(self) -> list[Violation]:
        """Every contract violation, located by row index and field name."""
        return [
            Violation(row.index, name, error)
            for row in self.rows
            for name, error in row.errors.items()
        ]

    @property
    def ok(self) -> bool:
        """Whether every record satisfied every field contract."""
        return all(row.ok for row in self.rows)


def _select(scope: Selector, query: str) -> SelectorList[Selector]:
    kind, sep, expression = query.partition(":")
    if not sep or kind not in ("css", "xpath"):
        expression = query
        kind = "css"
    if kind == "css":
        return scope.css(expression)
    return scope.xpath(expression)


def _scope_without_nested_records(row_selector: Selector, row_query: str) -> Selector:
    """Return a copy of the record with nested records removed.

    Field queries are evaluated against this pruned copy so that nodes
    belonging to a nested record (e.g. the price of a product card
    embedded in another card) never leak into the enclosing record.
    """
    if not isinstance(row_selector.root, etree._Element):
        return row_selector
    root = copy.deepcopy(row_selector.root)
    scope = Selector(root=root, type=row_selector.type)
    for nested in _select(scope, row_query):
        if nested.root is not root:
            nested.drop()
    return scope


class RowExtractor:
    """Extract records row by row instead of zipping flat lists.

    ``row`` selects the repeating records (``"css:..."`` / ``"xpath:..."``
    prefixed, CSS by default). ``fields`` maps field names to
    :func:`required`, :func:`optional` or :func:`many` declarations; a plain
    string is shorthand for :func:`required`.

    Records that match the row selector inside another record (nested
    cards) are extracted as their own rows, and their nodes never leak
    into the enclosing record's fields.
    """

    def __init__(self, row: str, fields: Mapping[str, Field | str]) -> None:
        self.row_query = row
        self.fields: dict[str, Field] = {
            name: spec if isinstance(spec, Field) else Field(spec)
            for name, spec in fields.items()
        }

    def extract(
        self,
        source: Selector | str,
        *,
        type: str | None = None,  # noqa: A002
    ) -> RowReport:
        """Extract all records from a :class:`Selector` or a document string,
        preserving row order."""
        selector = (
            source if isinstance(source, Selector) else Selector(text=source, type=type)
        )
        return RowReport(
            [
                self._extract_row(index, row_selector)
                for index, row_selector in enumerate(_select(selector, self.row_query))
            ]
        )

    def _extract_row(self, index: int, row_selector: Selector) -> Row:
        scope = _scope_without_nested_records(row_selector, self.row_query)
        return Row(
            index=index,
            fields={
                name: self._extract_field(scope, spec)
                for name, spec in self.fields.items()
            },
        )

    def _extract_field(self, scope: Selector, spec: Field) -> FieldValue:
        values: list[str] = [match.get() for match in _select(scope, spec.query)]
        if spec.cardinality is Cardinality.MANY:
            return values
        if not values:
            if spec.cardinality is Cardinality.OPTIONAL:
                return None
            return MissingValue(spec.query)
        if len(values) > 1:
            return MultipleMatches(spec.query, len(values), tuple(values))
        return values[0]
