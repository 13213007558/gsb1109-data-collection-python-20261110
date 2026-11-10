from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from itemadapter.adapter import ItemAdapter
from itemadapter.utils import is_item

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "CompareConfig",
    "CompareReport",
    "FieldChange",
    "Pairing",
    "PairingIssue",
    "Tolerance",
    "compare_records",
]


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


MISSING = _Missing()


@dataclass(frozen=True)
class Tolerance:
    """Accepted deviation for a numeric field.

    ``absolute`` is an upper bound for ``abs(new - old)``, ``relative`` is an
    upper bound for the deviation relative to ``max(abs(old), abs(new))``.
    A difference is accepted if it fits either bound.
    """

    absolute: float | None = None
    relative: float | None = None

    def allows(self, old: Any, new: Any) -> bool:
        if not (isinstance(old, (int, float)) and isinstance(new, (int, float))):
            return bool(old == new)
        diff = abs(new - old)
        if self.absolute is not None and diff <= self.absolute:
            return True
        if self.relative is not None:
            scale: float = max(abs(old), abs(new))
            if diff <= self.relative * scale:
                return True
        return self.absolute is None and self.relative is None and old == new


@dataclass(frozen=True)
class CompareConfig:
    """Comparison rules for :func:`compare_records`.

    ``key_fields`` maps a collection field name (or full dotted path) to a
    business key: either the name of the key attribute or a callable that
    extracts it from an element. Elements of those collections are paired by
    key instead of by position. Collections not listed here are compared
    element by element, in order.

    ``tolerances`` maps a numeric field name (or full dotted path) to a
    :class:`Tolerance`.
    """

    key_fields: Mapping[str, str | Callable[[Any], Any]] = field(default_factory=dict)
    tolerances: Mapping[str, Tolerance] = field(default_factory=dict)


@dataclass(frozen=True)
class FieldChange:
    """A single field-level difference between two records."""

    path: str
    kind: str  # "added" | "removed" | "changed"
    old: Any
    new: Any


@dataclass(frozen=True)
class PairingIssue:
    """A problem that prevented clean key-based pairing."""

    path: str
    kind: str  # "duplicate_key" | "missing_key" | "incomparable_type"
    message: str
    key: Any = None


@dataclass(frozen=True)
class Pairing:
    """Pairing basis: which elements were matched by business key."""

    path: str
    key: Any
    old_index: int
    new_index: int


@dataclass
class CompareReport:
    """Outcome of :func:`compare_records`."""

    changes: list[FieldChange] = field(default_factory=list)
    issues: list[PairingIssue] = field(default_factory=list)
    pairings: list[Pairing] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    @property
    def needs_notification(self) -> bool:
        """True if there is anything worth pushing: real value changes or
        pairing problems. Pure reordering does not qualify."""
        return bool(self.changes or self.issues)


def _resolve(spec: Mapping[str, Any], path: str, leaf: str) -> Any:
    if path in spec:
        return spec[path]
    return spec.get(leaf)


def _is_collection(value: Any) -> bool:
    return isinstance(value, (list, tuple))


def _as_record(value: Any) -> ItemAdapter | None:
    if is_item(value):
        return ItemAdapter(value)
    return None


def _child_path(path: str, field_name: str) -> str:
    return f"{path}.{field_name}" if path else field_name


class _Comparer:
    def __init__(self, config: CompareConfig, report: CompareReport) -> None:
        self.config = config
        self.report = report

    def compare_records(self, old: Any, new: Any, path: str) -> None:
        old_rec = _as_record(old)
        new_rec = _as_record(new)
        if old_rec is None or new_rec is None:
            self.report.issues.append(
                PairingIssue(
                    path=path,
                    kind="incomparable_type",
                    message=(
                        f"cannot compare {type(old).__name__} with {type(new).__name__} as records"
                    ),
                )
            )
            return
        field_names = list(old_rec.keys())
        field_names += [name for name in new_rec if name not in old_rec]
        for field_name in field_names:
            old_value = old_rec.get(field_name, MISSING)
            new_value = new_rec.get(field_name, MISSING)
            self.compare_field(old_value, new_value, _child_path(path, field_name), field_name)

    def compare_field(self, old: Any, new: Any, path: str, leaf: str) -> None:
        if old is MISSING or new is MISSING:
            if old is not new:
                kind = "removed" if new is MISSING else "added"
                self.report.changes.append(FieldChange(path=path, kind=kind, old=old, new=new))
            return
        if old is None or new is None:
            # None is a distinct state, different from a missing field or an
            # empty collection, but still comparable as a state transition.
            if old is not new:
                self.report.changes.append(
                    FieldChange(path=path, kind="changed", old=old, new=new)
                )
            return
        old_rec = _as_record(old)
        new_rec = _as_record(new)
        if old_rec is not None or new_rec is not None:
            if old_rec is None or new_rec is None:
                self._incomparable(path, old, new)
            else:
                self.compare_records(old, new, path)
            return
        if _is_collection(old) or _is_collection(new):
            if not (_is_collection(old) and _is_collection(new)):
                self._incomparable(path, old, new)
                return
            key_spec = _resolve(self.config.key_fields, path, leaf)
            if key_spec is None:
                self.compare_sequence(old, new, path, leaf)
            else:
                self.compare_keyed(old, new, path, leaf, key_spec)
            return
        tolerance = _resolve(self.config.tolerances, path, leaf)
        equal = tolerance.allows(old, new) if tolerance is not None else old == new
        if not equal:
            self.report.changes.append(FieldChange(path=path, kind="changed", old=old, new=new))

    def _incomparable(self, path: str, old: Any, new: Any) -> None:
        self.report.issues.append(
            PairingIssue(
                path=path,
                kind="incomparable_type",
                message=(f"cannot compare {type(old).__name__} with {type(new).__name__}"),
            )
        )

    def compare_sequence(self, old: list[Any], new: list[Any], path: str, leaf: str) -> None:
        for index, (old_el, new_el) in enumerate(zip(old, new, strict=False)):
            self.compare_field(old_el, new_el, f"{path}[{index}]", leaf)
        for index in range(min(len(old), len(new)), max(len(old), len(new))):
            el_path = f"{path}[{index}]"
            if index < len(old):
                self.report.changes.append(
                    FieldChange(path=el_path, kind="removed", old=old[index], new=MISSING)
                )
            else:
                self.report.changes.append(
                    FieldChange(path=el_path, kind="added", old=MISSING, new=new[index])
                )

    def _extract_key(self, element: Any, key_spec: Any, path: str, side: str) -> Any:
        record = _as_record(element)
        if record is None:
            self.report.issues.append(
                PairingIssue(
                    path=path,
                    kind="incomparable_type",
                    message=(
                        f"{side} element of type {type(element).__name__} is not "
                        "a record, cannot extract a business key"
                    ),
                )
            )
            return MISSING
        if callable(key_spec):
            try:
                key = key_spec(element)
            except Exception as exc:
                self.report.issues.append(
                    PairingIssue(
                        path=path,
                        kind="missing_key",
                        message=f"{side} element: key extractor failed: {exc}",
                    )
                )
                return MISSING
        elif key_spec in record:
            key = record[key_spec]
        else:
            self.report.issues.append(
                PairingIssue(
                    path=path,
                    kind="missing_key",
                    message=f"{side} element has no key field {key_spec!r}",
                )
            )
            return MISSING
        try:
            hash(key)
        except TypeError:
            self.report.issues.append(
                PairingIssue(
                    path=path,
                    kind="incomparable_type",
                    message=(
                        f"{side} element key {key!r} of type {type(key).__name__} is not hashable"
                    ),
                )
            )
            return MISSING
        return key

    def compare_keyed(
        self, old: list[Any], new: list[Any], path: str, leaf: str, key_spec: Any
    ) -> None:
        old_pairs = [(self._extract_key(el, key_spec, path, "old"), i) for i, el in enumerate(old)]
        new_pairs = [(self._extract_key(el, key_spec, path, "new"), i) for i, el in enumerate(new)]
        old_index, old_duplicated = self._index_by_key(old_pairs, path, "old")
        new_index, new_duplicated = self._index_by_key(new_pairs, path, "new")
        for key in old_duplicated | new_duplicated:
            old_index.pop(key, None)
            new_index.pop(key, None)
        keys = list(old_index.keys())
        keys += [key for key in new_index if key not in old_index]
        for key in keys:
            old_pos = old_index.get(key)
            new_pos = new_index.get(key)
            el_path = f"{path}[{key!r}]"
            if old_pos is None:
                assert new_pos is not None
                self.report.changes.append(
                    FieldChange(path=el_path, kind="added", old=MISSING, new=new[new_pos])
                )
            elif new_pos is None:
                self.report.changes.append(
                    FieldChange(path=el_path, kind="removed", old=old[old_pos], new=MISSING)
                )
            else:
                self.report.pairings.append(
                    Pairing(path=path, key=key, old_index=old_pos, new_index=new_pos)
                )
                self.compare_field(old[old_pos], new[new_pos], el_path, leaf)

    def _index_by_key(
        self, pairs: list[tuple[Any, int]], path: str, side: str
    ) -> tuple[dict[Any, int], set[Any]]:
        index: dict[Any, int] = {}
        duplicated: set[Any] = set()
        for key, position in pairs:
            if key is MISSING:
                continue
            if key in index:
                duplicated.add(key)
            else:
                index[key] = position
        for key in sorted(duplicated, key=repr):
            positions = [position for k, position in pairs if k == key]
            self.report.issues.append(
                PairingIssue(
                    path=path,
                    kind="duplicate_key",
                    key=key,
                    message=(
                        f"key {key!r} appears {len(positions)} times in the {side} "
                        f"collection (positions {positions}), elements left unpaired"
                    ),
                )
            )
            del index[key]
        return index, duplicated


def compare_records(old: Any, new: Any, config: CompareConfig | None = None) -> CompareReport:
    """Compare two scraped records without modifying them.

    Returns a :class:`CompareReport` with field-level changes, the pairing
    basis used for keyed collections, and any pairing problems (duplicate
    keys, missing keys, incomparable types) reported separately.
    """
    config = config or CompareConfig()
    report = CompareReport()
    _Comparer(config, report).compare_records(old, new, "")
    return report
