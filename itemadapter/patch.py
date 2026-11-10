"""Explicit, atomic patch operations for items.

When a detail page only returns a subset of fields, the three cases below
must not be conflated when updating a stored record:

* a field missing from the response means "not scraped" (leave untouched):
  simply do not include an operation for it;
* :class:`Set` with a ``None`` value means "explicitly blank the field";
* :class:`Delete` means "remove the old value" (a mapping key, an item
  field or a list element).

Operations are applied atomically: if any path does not exist, any list
index is out of range, or any target field is read-only, the whole batch
fails with :class:`PatchError` and the original record is left unchanged.
List indices are always interpreted against the same pre-modification
snapshot, so deleting several elements from the same list in one batch
does not suffer from index shifting.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from itemadapter.adapter import ItemAdapter

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "Delete",
    "PatchError",
    "Set",
    "apply_patch",
]

PathSegment = str | int
Path = tuple[PathSegment, ...]


class PatchError(Exception):
    """A patch operation could not be applied.

    Patching is atomic: when this error is raised, the target item is
    guaranteed to be left unchanged.
    """


@dataclass(frozen=True)
class Set:
    """Set the value at ``path``, e.g. ``Set(("price",), 9.99)``.

    Use ``None`` as the value to explicitly blank a field.
    """

    path: Path
    value: Any


@dataclass(frozen=True)
class Delete:
    """Delete the value at ``path``: a mapping key, an item field or a
    list element."""

    path: Path


PatchOp = Set | Delete


def _format_path(path: Path) -> str:
    parts = []
    for segment in path:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        elif parts:
            parts.append(f".{segment}")
        else:
            parts.append(str(segment))
    return "".join(parts)


def _normalize_op(op: PatchOp) -> PatchOp:
    if not isinstance(op, (Set, Delete)):
        raise PatchError(f"Unknown patch operation: {op!r}")
    path = tuple(op.path)
    if not path:
        raise PatchError(f"Empty path in {op!r}")
    for segment in path:
        if not isinstance(segment, (str, int)):
            raise PatchError(f"Invalid path segment {segment!r} in {op!r}")
    if isinstance(op, Set):
        return Set(path, op.value)
    return Delete(path)


def _normalize_index(
    sequence: list[Any] | tuple[Any, ...], segment: PathSegment, path: Path
) -> int:
    if not isinstance(segment, int):
        raise PatchError(
            f"{_format_path(path)}: {segment!r} is not a valid index "
            f"for a sequence of type {type(sequence).__name__}"
        )
    index = segment + len(sequence) if segment < 0 else segment
    if not 0 <= index < len(sequence):
        raise PatchError(
            f"{_format_path(path)}: index {segment} is out of range "
            f"for a sequence of length {len(sequence)}"
        )
    return index


def _get_child(container: Any, segment: PathSegment, path: Path) -> Any:
    if isinstance(container, (list, tuple)):
        return container[_normalize_index(container, segment, path)]
    if isinstance(container, dict):
        try:
            return container[segment]
        except KeyError:
            raise PatchError(f"{_format_path(path)}: key {segment!r} does not exist") from None
    if ItemAdapter.is_item(container):
        adapter = ItemAdapter(container)
        try:
            return adapter[cast("str", segment)]
        except (KeyError, TypeError):
            raise PatchError(f"{_format_path(path)}: field {segment!r} does not exist") from None
    raise PatchError(
        f"{_format_path(path)}: cannot descend into an object of type {type(container).__name__}"
    )


def _navigate(root: Any, path: Path) -> Any:
    current = root
    for position, segment in enumerate(path):
        current = _get_child(current, segment, path[: position + 1])
    return current


def _set_child(container: Any, segment: PathSegment, value: Any, path: Path) -> None:
    if isinstance(container, list):
        container[_normalize_index(container, segment, path)] = value
        return
    if isinstance(container, tuple):
        raise PatchError(f"{_format_path(path)}: tuples are read-only")
    if isinstance(container, dict):
        container[segment] = value
        return
    if ItemAdapter.is_item(container):
        adapter = ItemAdapter(container)
        if not isinstance(segment, str) or segment not in adapter.field_names():
            raise PatchError(
                f"{_format_path(path)}: {type(container).__name__} "
                f"does not support field: {segment}"
            )
        try:
            adapter[segment] = value
        except (AttributeError, TypeError) as ex:
            raise PatchError(f"{_format_path(path)}: field {segment!r} is read-only") from ex
        return
    raise PatchError(
        f"{_format_path(path)}: cannot set a value on an object of type {type(container).__name__}"
    )


def _apply_set(root: Any, op: Set) -> None:
    parent = _navigate(root, op.path[:-1])
    _set_child(parent, op.path[-1], op.value, op.path)


def _collect_deletions(root: Any, deletes: list[Delete]) -> list[tuple[Any, PathSegment, Path]]:
    """Resolve every deletion target against the pre-modification state."""
    deletions: list[tuple[Any, PathSegment, Path]] = []
    seen = set()
    for op in deletes:
        parent = _navigate(root, op.path[:-1])
        segment = op.path[-1]
        if isinstance(parent, list):
            segment = _normalize_index(parent, segment, op.path)
        elif isinstance(parent, tuple):
            raise PatchError(f"{_format_path(op.path)}: tuples are read-only")
        elif isinstance(parent, dict):
            if segment not in parent:
                raise PatchError(f"{_format_path(op.path)}: key {segment!r} does not exist")
        elif ItemAdapter.is_item(parent):
            adapter = ItemAdapter(parent)
            if not isinstance(segment, str) or segment not in adapter.field_names():
                raise PatchError(
                    f"{_format_path(op.path)}: {type(parent).__name__} "
                    f"does not support field: {segment}"
                )
            if segment not in adapter:
                raise PatchError(f"{_format_path(op.path)}: field {segment!r} does not exist")
        else:
            raise PatchError(
                f"{_format_path(op.path)}: cannot delete a value on an object of type "
                f"{type(parent).__name__}"
            )
        key = (id(parent), segment)
        if key in seen:
            raise PatchError(f"{_format_path(op.path)}: duplicate delete operation")
        seen.add(key)
        deletions.append((parent, segment, op.path))
    return deletions


def _apply_deletions(deletions: list[tuple[Any, PathSegment, Path]]) -> None:
    list_deletions: dict[int, tuple[list[Any], list[int]]] = {}
    for parent, segment, path in deletions:
        if isinstance(parent, list):
            list_deletions.setdefault(id(parent), (parent, []))[1].append(cast("int", segment))
        elif isinstance(parent, dict):
            del parent[segment]
        else:
            adapter = ItemAdapter(parent)
            try:
                del adapter[cast("str", segment)]
            except (AttributeError, KeyError, TypeError) as ex:
                raise PatchError(f"{_format_path(path)}: field {segment!r} is read-only") from ex
    for parent, indices in list_deletions.values():
        for index in sorted(indices, reverse=True):
            del parent[index]


def _apply(item: Any, sets: list[Set], deletes: list[Delete]) -> None:
    for op in sets:
        _apply_set(item, op)
    _apply_deletions(_collect_deletions(item, deletes))


def apply_patch(item: Any, operations: Iterable[PatchOp]) -> None:
    """Atomically apply a batch of patch operations to ``item``.

    ``operations`` is an iterable of :class:`Set` and :class:`Delete`
    operations. Fields not referenced by any operation are left untouched,
    and so are the types of the item and its nested objects. If any
    operation fails (missing path, out-of-range index, read-only target),
    :class:`PatchError` is raised and ``item`` is left unchanged.
    """
    ops = [_normalize_op(op) for op in operations]
    sets = [op for op in ops if isinstance(op, Set)]
    deletes = [op for op in ops if isinstance(op, Delete)]
    trial = copy.deepcopy(item)
    _apply(trial, sets, deletes)
    _apply(item, sets, deletes)
