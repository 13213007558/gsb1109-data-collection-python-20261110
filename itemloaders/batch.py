"""
Batch loading of items with per-record error reporting.

:func:`load_items` loads many records through an :class:`ItemLoader`
subclass, yielding one :class:`LoadResult` per input record instead of
aborting the whole batch (or silently skipping records) when a single
record is broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from itemloaders import ItemLoader

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping


@dataclass
class FieldError:
    """
    A single failure found while loading a record.

    ``field_name`` is the name of the field whose input or output
    processing failed, or ``None`` if the failure is not tied to a
    specific field. ``exception`` is the original exception raised by
    the loader.
    """

    field_name: str | None
    message: str
    exception: Exception


@dataclass
class BatchRecord:
    """
    A single input record for :func:`load_items`.

    ``fields`` maps field names to raw values, loaded through
    :meth:`ItemLoader.add_value`. ``context`` is merged over the shared
    context of :func:`load_items` for this record only, so adjacent
    records can use different loader context settings (e.g. different
    languages) without affecting each other.
    """

    fields: Mapping[str, Any]
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class LoadResult:
    """
    The outcome of loading a single record.

    ``index`` is the 0-based position of the record in the input
    iterable. On success ``item`` holds the loaded item and ``errors``
    is empty. On failure ``item`` is ``None`` and ``errors`` holds one
    :class:`FieldError` per failed field.
    """

    index: int
    item: Any = None
    errors: list[FieldError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return True if the record was loaded without errors."""
        return not self.errors


class BatchLoadError(Exception):
    """
    Raised by :func:`load_items` when ``fail_fast=True`` and a record
    fails to load. Carries the record ``index`` and its ``errors``;
    no further input records are consumed after it is raised.
    """

    def __init__(self, index: int, errors: list[FieldError]) -> None:
        self.index = index
        self.errors = errors
        fields = ", ".join(repr(error.field_name) for error in errors)
        super().__init__(f"Record #{index} failed to load (fields: {fields})")


def load_items(
    records: Iterable[Mapping[str, Any] | BatchRecord],
    loader_class: type[ItemLoader] = ItemLoader,
    *,
    fail_fast: bool = False,
    **context: Any,
) -> Iterator[LoadResult]:
    """
    Load many records through ``loader_class``, yielding one
    :class:`LoadResult` per input record.

    Each element of ``records`` is either a mapping of field names to
    raw values, or a :class:`BatchRecord` when the record needs its own
    loader context overrides. Every record is loaded with a fresh
    ``loader_class`` instance and a fresh copy of the shared ``context``
    (merged with the record's own context, if any), so loader contexts
    and collected values never leak between records.

    Records are consumed lazily: ``records`` may be a one-shot iterator
    or generator, and if the caller stops iterating the returned
    generator early, no further records are read from the input.

    When a record fails, its :class:`LoadResult` keeps the input
    ``index`` and one :class:`FieldError` per failed field, and the
    batch continues with the next record. With ``fail_fast=True`` a
    :class:`BatchLoadError` is raised on the first failing record
    instead, stopping the batch without consuming more input.
    """
    for index, record in enumerate(records):
        if isinstance(record, BatchRecord):
            fields = record.fields
            record_context = {**context, **record.context}
        else:
            fields = record
            record_context = dict(context)
        loader = loader_class(**record_context)
        errors: list[FieldError] = []
        for field_name, value in fields.items():
            try:
                loader.add_value(field_name, value)
            except Exception as e:
                errors.append(FieldError(field_name, str(e), e))
        item = None
        if not errors:
            try:
                item = loader.load_item()
            except Exception as e:
                errors.append(FieldError(None, str(e), e))
        if errors:
            if fail_fast:
                raise BatchLoadError(index, errors) from errors[0].exception
            yield LoadResult(index=index, errors=errors)
        else:
            yield LoadResult(index=index, item=item)
