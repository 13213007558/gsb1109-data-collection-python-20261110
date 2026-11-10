"""Incremental structural profiling for batches of mixed items.

:class:`StructureProfiler` walks items of any supported type (dict, TypedDict,
dataclass, attrs, pydantic, scrapy.Item) through :class:`ItemAdapter` and
accumulates per-path statistics that can be merged across batches: profiling
two batches separately and merging the results yields the same counters as
profiling all items at once.

Paths are dotted field names (e.g. ``author.name``). Elements of a
list/tuple/set share the path of the collection itself, so arrays of
different lengths never show up as missing fields.

For every path the profiler tracks:

* ``observations``: how many times the path was evaluated. This is the
  denominator for all rates of that path.
* ``missing``: the field is declared on the item class but not set on the
  instance. Defaults and default factories are never called to fill gaps;
  only actually-set attributes count as present.
* ``undeclared``: the field is set on the item but not declared on the item
  class. Only computed for item types that declare fields (dataclass, attrs,
  pydantic, TypedDict, scrapy.Item); plain dicts declare nothing, so no key
  of a plain dict is ever reported as undeclared. TypedDict instances are
  plain dicts at run time, so pass ``item_class`` to :meth:`StructureProfiler.add`
  to check their keys against the declared annotations.
* ``none``: the value is ``None``.
* ``empty``: the value is an empty collection (dict, list, tuple, set or
  frozenset of length 0). Empty strings/bytes are valid values, not empty
  collections.
* ``valid``: any other value.
* ``types``/``element_types``: observed type names of the value itself and,
  for collections, of their elements.

Denominator semantics: ``observations`` of a nested path only counts the
parent objects in which the path was actually evaluated. For item types with
a declared schema, every declared field is evaluated once per parent object,
so ``observations`` equals the number of parent objects. For plain dicts
(which declare nothing) only present keys are evaluated, so a key that is
absent simply lowers ``observations`` relative to its parent instead of
being counted as missing. Because a ``None`` or missing parent is never
recursed into, null rates of nested paths are conditional on the parent
existing and are not counted again at deeper levels.

Small example::

    profiler = StructureProfiler()
    profiler.update([
        {"name": "a", "author": {"name": "x"}},
        {"name": None, "author": None},
        {"name": "c"},
    ])
    report = profiler.report()

Resulting counters:

* ``name``: observations=3, valid=2, none=1
* ``author``: observations=2, valid=1, none=1 (absent in the third item,
  which only lowers the denominator because dicts declare no fields)
* ``author.name``: observations=1, valid=1 — the None and the absent
  ``author`` values are counted once at the ``author`` level and do not
  inflate the null counts of ``author.name``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from itemadapter.adapter import ItemAdapter

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["FieldStats", "StructureProfiler"]

_COLLECTION_TYPES = (dict, list, tuple, set, frozenset)


@dataclass
class FieldStats:
    """Mergeable counters for a single nested path.

    ``observations`` is the denominator for every rate derived from these
    counters and always equals ``missing + none + empty + valid``.
    ``undeclared`` is orthogonal: an undeclared field is also classified as
    none, empty or valid.
    """

    observations: int = 0
    missing: int = 0
    undeclared: int = 0
    none: int = 0
    empty: int = 0
    valid: int = 0
    types: set[str] = field(default_factory=set)
    element_types: set[str] = field(default_factory=set)

    def merge(self, other: FieldStats) -> None:
        """Add the counters of another FieldStats into this one, in place."""
        self.observations += other.observations
        self.missing += other.missing
        self.undeclared += other.undeclared
        self.none += other.none
        self.empty += other.empty
        self.valid += other.valid
        self.types |= other.types
        self.element_types |= other.element_types

    @property
    def null_count(self) -> int:
        """Number of observations without a usable value."""
        return self.missing + self.none + self.empty

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the counters, including rates.

        All rates use ``observations`` as denominator, i.e. they are
        conditional on the parent object having been observed.
        """

        def rate(count: int) -> float:
            return count / self.observations if self.observations else 0.0

        return {
            "observations": self.observations,
            "missing": self.missing,
            "undeclared": self.undeclared,
            "none": self.none,
            "empty": self.empty,
            "valid": self.valid,
            "missing_rate": rate(self.missing),
            "none_rate": rate(self.none),
            "empty_rate": rate(self.empty),
            "valid_rate": rate(self.valid),
            "null_rate": rate(self.null_count),
            "types": sorted(self.types),
            "element_types": sorted(self.element_types),
        }


class StructureProfiler:
    """Accumulate structural statistics over items, batch by batch.

    Use :meth:`add` or :meth:`update` to feed items, :meth:`merge` to combine
    two profilers (order does not matter), and :meth:`report` for the final
    per-path counters and rates.
    """

    def __init__(self) -> None:
        self._stats: dict[str, FieldStats] = {}
        self.item_count = 0

    @classmethod
    def profile(cls, items: Iterable[Any]) -> StructureProfiler:
        """Return a profiler that has processed all the given items at once."""
        profiler = cls()
        profiler.update(items)
        return profiler

    def add(self, item: Any, *, item_class: type | None = None) -> None:
        """Accumulate the structure of a single item.

        ``item_class`` defaults to the item's own class. Pass it explicitly
        to check the item against a declared schema that cannot be derived
        from the instance, e.g. a TypedDict subclass.
        """
        if not ItemAdapter.is_item(item):
            raise TypeError(f"No adapter found for objects of type: {type(item)} ({item})")
        self.item_count += 1
        self._walk_item(item, path="", seen=frozenset(), item_class=item_class)

    def update(self, items: Iterable[Any]) -> None:
        """Accumulate the structure of a batch of items."""
        for item in items:
            self.add(item)

    def merge(self, other: StructureProfiler) -> StructureProfiler:
        """Return a new profiler combining this one with another.

        Merging is associative and commutative: profiling batches separately
        and merging the profilers yields the same counters as profiling all
        items in a single batch.
        """
        if not isinstance(other, StructureProfiler):
            raise TypeError(f"Cannot merge StructureProfiler with {type(other)}")
        merged = StructureProfiler()
        merged.item_count = self.item_count + other.item_count
        for source in (self, other):
            for path, path_stats in source._stats.items():
                merged._stats.setdefault(path, FieldStats()).merge(path_stats)
        return merged

    def __add__(self, other: StructureProfiler) -> StructureProfiler:
        return self.merge(other)

    def __iadd__(self, other: StructureProfiler) -> Self:
        if not isinstance(other, StructureProfiler):
            raise TypeError(f"Cannot merge StructureProfiler with {type(other)}")
        self.item_count += other.item_count
        for path, path_stats in other._stats.items():
            self._stats.setdefault(path, FieldStats()).merge(path_stats)
        return self

    def stats(self) -> dict[str, FieldStats]:
        """Return a new dict mapping each observed path to its FieldStats."""
        return dict(self._stats)

    def report(self) -> dict[str, Any]:
        """Return a JSON-friendly report with per-path counters and rates."""
        return {
            "item_count": self.item_count,
            "paths": {path: self._stats[path].to_dict() for path in sorted(self._stats)},
        }

    def _walk_item(
        self,
        item: Any,
        path: str,
        seen: frozenset[int],
        item_class: type | None = None,
    ) -> None:
        if id(item) in seen:
            return
        seen = seen | {id(item)}
        adapter = ItemAdapter(item)
        present = set(iter(adapter))
        declared = ItemAdapter.get_field_names_from_class(item_class or item.__class__)
        declared_set = set(declared) if declared is not None else None
        names = list(present)
        if declared is not None:
            names.extend(name for name in declared if name not in present)
        for name in names:
            child_path = f"{path}.{name}" if path else name
            path_stats = self._stats.setdefault(child_path, FieldStats())
            path_stats.observations += 1
            if name not in present:
                path_stats.missing += 1
                continue
            if declared_set is not None and name not in declared_set:
                path_stats.undeclared += 1
            value = adapter[name]
            path_stats.types.add(type(value).__name__)
            if value is None:
                path_stats.none += 1
            elif isinstance(value, _COLLECTION_TYPES) and not value:
                path_stats.empty += 1
            else:
                path_stats.valid += 1
                self._walk_value(value, child_path, path_stats, seen)

    def _walk_value(
        self, value: Any, path: str, path_stats: FieldStats, seen: frozenset[int]
    ) -> None:
        if ItemAdapter.is_item(value):
            self._walk_item(value, path, seen)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for element in value:
                path_stats.element_types.add(type(element).__name__)
                if ItemAdapter.is_item(element):
                    self._walk_item(element, path, seen)
