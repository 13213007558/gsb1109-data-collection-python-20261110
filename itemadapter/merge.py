"""Source-aware merging of repeated observations of the same record.

The same logical record (e.g. a product) is often collected more than once from
different sources: a listing page, a detail page, a merchant API, and so on. A
plain ``dict.update()``-style merge is unsafe in that situation, because an
observation that does not know a field, or that reports it as empty, would
overwrite a valid value collected elsewhere.

:func:`merge_records` merges such observations field by field, keeping track of
the source that each adopted value comes from and of every conflict that could
not be resolved automatically. Missing fields, empty values (``None`` or empty
strings) and empty lists are three different kinds of "emptiness", and each is
governed by its own policy instead of being treated as the same thing.

The merge is deterministic: shuffling the input records never changes the
result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from itemadapter.adapter import ItemAdapter

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "OBSERVED_AT_KEY",
    "SOURCE_KEY",
    "AbsencePolicy",
    "Conflict",
    "EmptyListMode",
    "EmptyListPolicy",
    "EmptyValuePolicy",
    "FieldDecision",
    "FieldPolicies",
    "MergeResult",
    "MergeStrategy",
    "Observation",
    "ObservationKind",
    "merge_records",
]

#: Reserved item key holding the source identifier of a whole observation record.
SOURCE_KEY = "_source"
#: Reserved item key holding the observation time of a whole observation record.
OBSERVED_AT_KEY = "_observed_at"

_RESERVED_KEYS = frozenset({SOURCE_KEY, OBSERVED_AT_KEY})

_NAN_SENTINEL = object()

EmptyListMode = Literal["elements", "empty_value"]


class MergeStrategy(StrEnum):
    """Strategy used to pick a value when several sources disagree."""

    #: Prefer the highest-priority source, see ``source_priority``.
    TRUSTED_SOURCE = "trusted_source"
    #: Prefer the most recently observed value.
    LATEST = "latest"
    #: Adopt a value only when every non-empty observation reports the same one.
    REQUIRE_CONSISTENT = "require_consistent"


class ObservationKind(StrEnum):
    """What an observation reports for a given field."""

    #: A regular, non-empty value.
    VALUE = "value"
    #: An explicit empty value: ``None`` or an empty string.
    EMPTY = "empty"
    #: An empty list, tuple, set or dictionary.
    EMPTY_LIST = "empty_list"


class AbsencePolicy(StrEnum):
    """What to do when a field is missing from some observation records."""

    #: Missing fields never participate in the merge and never raise conflicts.
    IGNORE = "ignore"
    #: Record a conflict whenever at least one record does not contain the field.
    CONFLICT = "conflict"


class EmptyValuePolicy(StrEnum):
    """What to do with explicit empty values (``None`` or empty strings)."""

    #: Empty values never overwrite non-empty values and never raise conflicts.
    IGNORE = "ignore"
    #: Record a conflict whenever an empty value is observed for the field.
    CONFLICT = "conflict"


class EmptyListPolicy(StrEnum):
    """What to do with empty containers (empty lists, tuples, sets or dicts)."""

    #: Empty containers never overwrite non-empty values and never raise conflicts.
    IGNORE = "ignore"
    #: Record a conflict whenever an empty container is observed for the field.
    CONFLICT = "conflict"
    #: Adopt the empty container when no non-empty value is available.
    ADOPT = "adopt"


@dataclass(frozen=True)
class FieldPolicies:
    """Per-field policies for the different kinds of emptiness.

    Fields without an explicit entry fall back to the ``default_policies``
    passed to :func:`merge_records`, which itself ignores every kind of
    emptiness by default.
    """

    absence: AbsencePolicy = AbsencePolicy.IGNORE
    empty_value: EmptyValuePolicy = EmptyValuePolicy.IGNORE
    empty_list: EmptyListPolicy = EmptyListPolicy.IGNORE


@dataclass(frozen=True)
class Observation:
    """A single reported value for a field."""

    field_name: str
    value: Any
    source: str | None
    observed_at: datetime | None
    kind: ObservationKind


@dataclass(frozen=True)
class Conflict:
    """An unresolved disagreement for a field."""

    field_name: str
    #: Machine-readable reason, e.g. ``"same_timestamp"`` or ``"empty_value"``.
    reason: str
    #: All observations involved, in a deterministic order.
    candidates: tuple[Observation, ...]


@dataclass(frozen=True)
class FieldDecision:
    """The full decision trail for a field."""

    field_name: str
    #: The observation whose value was adopted, if any.
    adopted: Observation | None
    #: The unresolved conflict for the field, if any.
    conflict: Conflict | None
    #: Every observation considered for the field, in a deterministic order.
    considered: tuple[Observation, ...]


@dataclass(frozen=True)
class MergeResult:
    """The output of :func:`merge_records`.

    ``record`` may still be handed to the business layer when ``has_conflicts``
    is true: conflict-free fields are merged normally, and the conflicting
    fields are simply left out of it pending a manual decision.
    """

    record: dict[str, Any]
    #: Adopted observation (including its source) for every field in ``record``.
    sources: dict[str, Observation]
    #: Unresolved conflicts keyed by field name.
    conflicts: dict[str, Conflict]
    #: Per-field decision trails, including fields without an adopted value.
    decisions: dict[str, FieldDecision]

    @property
    def has_conflicts(self) -> bool:
        """Return True if at least one field has an unresolved conflict."""
        return bool(self.conflicts)


def merge_records(
    records: Iterable[Any],
    *,
    strategy: MergeStrategy | str = MergeStrategy.LATEST,
    source_priority: Sequence[str] | None = None,
    field_policies: Mapping[str, FieldPolicies] | None = None,
    default_policies: FieldPolicies | None = None,
    empty_list_mode: EmptyListMode = "elements",
) -> MergeResult:
    """Merge repeated observations of the same record into one candidate.

    :param records: any objects supported by :class:`~itemadapter.ItemAdapter`.
        Each record is one observation; it may carry record-level provenance in
        the reserved ``_source`` and ``_observed_at`` keys, while field metadata
        keys ``source`` and ``observed_at`` provide per-field provenance on
        typed items.
    :param strategy: how to choose between different non-empty values, see
        :class:`MergeStrategy`.
    :param source_priority: source identifiers, from most to least trusted.
        Required for (and only used by) ``TRUSTED_SOURCE``. Sources not listed
        are all considered less trusted than every listed source.
    :param field_policies: per-field :class:`FieldPolicies`, overriding the
        defaults for the named fields only.
    :param default_policies: policies used for fields absent from
        ``field_policies``. Defaults to ignoring every kind of emptiness.
    :param empty_list_mode: ``"elements"`` (the default) treats only empty
        containers as empty lists; ``"empty_value"`` also treats ``None`` and
        empty strings as empty-list observations.
    """
    strategy = MergeStrategy(strategy)
    if empty_list_mode not in ("elements", "empty_value"):
        msg = f"invalid empty_list_mode: {empty_list_mode!r}"
        raise ValueError(msg)
    priority_list = list(source_priority or [])
    if len(set(priority_list)) != len(priority_list):
        msg = "source_priority contains duplicated sources"
        raise ValueError(msg)
    if strategy is MergeStrategy.TRUSTED_SOURCE and not priority_list:
        msg = "source_priority is required for the TRUSTED_SOURCE strategy"
        raise ValueError(msg)
    priority = {source: index for index, source in enumerate(priority_list)}
    low_priority = len(priority_list)
    policies_by_field = dict(field_policies or {})
    default = default_policies or FieldPolicies()

    observations_by_field: dict[str, list[Observation]] = {}
    present_indexes: dict[str, set[int]] = {}
    record_count = 0
    for record_index, record in enumerate(records):
        record_count += 1
        adapter = ItemAdapter(record)
        record_source = adapter.get(SOURCE_KEY)
        record_observed_at = _coerce_datetime(adapter.get(OBSERVED_AT_KEY))
        for field_name, value in adapter.items():
            if field_name in _RESERVED_KEYS:
                continue
            meta = adapter.get_field_meta(field_name)
            source = meta.get("source", record_source)
            observed_at = _coerce_datetime(meta.get("observed_at", record_observed_at))
            observation = Observation(
                field_name=field_name,
                value=value,
                source=source,
                observed_at=observed_at,
                kind=_classify(value, empty_list_mode),
            )
            observations_by_field.setdefault(field_name, []).append(observation)
            present_indexes.setdefault(field_name, set()).add(record_index)

    merged_record: dict[str, Any] = {}
    sources: dict[str, Observation] = {}
    conflicts: dict[str, Conflict] = {}
    decisions: dict[str, FieldDecision] = {}
    for field_name in sorted(observations_by_field):
        observations = observations_by_field[field_name]
        policies = policies_by_field.get(field_name, default)
        decision = _decide_field(
            field_name=field_name,
            observations=observations,
            absent_count=record_count - len(present_indexes[field_name]),
            strategy=strategy,
            priority=priority,
            low_priority=low_priority,
            policies=policies,
        )
        decisions[field_name] = decision
        if decision.conflict is not None:
            conflicts[field_name] = decision.conflict
        if decision.adopted is not None:
            merged_record[field_name] = decision.adopted.value
            sources[field_name] = decision.adopted

    return MergeResult(
        record=merged_record,
        sources=sources,
        conflicts=conflicts,
        decisions=decisions,
    )


def _decide_field(
    *,
    field_name: str,
    observations: list[Observation],
    absent_count: int,
    strategy: MergeStrategy,
    priority: Mapping[str, int],
    low_priority: int,
    policies: FieldPolicies,
) -> FieldDecision:
    considered = tuple(sorted(observations, key=lambda obs: _obs_order_key(obs, priority)))
    value_observations = [obs for obs in observations if obs.kind is ObservationKind.VALUE]
    empty_observations = [obs for obs in observations if obs.kind is ObservationKind.EMPTY]
    empty_list_observations = [
        obs for obs in observations if obs.kind is ObservationKind.EMPTY_LIST
    ]

    conflict = _value_conflict(field_name, value_observations, strategy, priority)
    adopted: Observation | None = None
    if conflict is None and value_observations:
        adopted = _select(value_observations, strategy, priority, low_priority)

    if conflict is None and not value_observations:
        conflict = _emptiness_conflict(
            field_name,
            empty_observations,
            empty_list_observations,
            absent_count,
            policies,
        )
        if (
            conflict is None
            and empty_list_observations
            and policies.empty_list is EmptyListPolicy.ADOPT
        ):
            adopted = _select(empty_list_observations, strategy, priority, low_priority)

    return FieldDecision(
        field_name=field_name,
        adopted=adopted,
        conflict=conflict,
        considered=considered,
    )


def _value_conflict(
    field_name: str,
    value_observations: list[Observation],
    strategy: MergeStrategy,
    priority: Mapping[str, int],
) -> Conflict | None:
    if not value_observations:
        return None
    distinct_values = {_freeze(obs.value) for obs in value_observations}
    if len(distinct_values) == 1:
        return None
    same_time = _same_timestamp_conflict(field_name, value_observations)
    if same_time is not None:
        return same_time
    if strategy is MergeStrategy.REQUIRE_CONSISTENT:
        return Conflict(
            field_name=field_name,
            reason="unresolved_values",
            candidates=tuple(
                sorted(value_observations, key=lambda obs: _obs_order_key(obs, priority))
            ),
        )
    return None


def _same_timestamp_conflict(
    field_name: str, value_observations: list[Observation]
) -> Conflict | None:
    by_timestamp: dict[datetime, list[Observation]] = {}
    for observation in value_observations:
        if observation.observed_at is not None:
            by_timestamp.setdefault(observation.observed_at, []).append(observation)
    for timed_observations in by_timestamp.values():
        distinct = {_freeze(obs.value) for obs in timed_observations}
        if len(distinct) > 1:
            return Conflict(
                field_name=field_name,
                reason="same_timestamp",
                candidates=tuple(sorted(timed_observations, key=_obs_order_key_no_priority)),
            )
    return None


def _emptiness_conflict(
    field_name: str,
    empty_observations: list[Observation],
    empty_list_observations: list[Observation],
    absent_count: int,
    policies: FieldPolicies,
) -> Conflict | None:
    candidates: list[Observation] = []
    reasons: list[str] = []
    if absent_count and policies.absence is AbsencePolicy.CONFLICT:
        reasons.append("absent")
    if empty_observations and policies.empty_value is EmptyValuePolicy.CONFLICT:
        candidates.extend(empty_observations)
        reasons.append("empty_value")
    if empty_list_observations and policies.empty_list is EmptyListPolicy.CONFLICT:
        candidates.extend(empty_list_observations)
        reasons.append("empty_list")
    if not reasons:
        return None
    return Conflict(
        field_name=field_name,
        reason="+".join(reasons),
        candidates=tuple(sorted(candidates, key=_obs_order_key_no_priority)),
    )


def _select(
    observations: list[Observation],
    strategy: MergeStrategy,
    priority: Mapping[str, int],
    low_priority: int,
) -> Observation:
    if strategy is MergeStrategy.TRUSTED_SOURCE:

        def key(obs: Observation) -> tuple[Any, ...]:
            return (
                _priority_of(obs, priority, low_priority),
                _time_preference_key(obs),
                obs.source or "",
                _stable_value_key(obs),
            )
    else:

        def key(obs: Observation) -> tuple[Any, ...]:
            return (
                _time_preference_key(obs),
                _priority_of(obs, priority, low_priority),
                obs.source or "",
                _stable_value_key(obs),
            )

    return min(observations, key=key)


def _priority_of(obs: Observation, priority: Mapping[str, int], default: int) -> int:
    if obs.source is None:
        return default
    return priority.get(obs.source, default)


def _stable_value_key(obs: Observation) -> str:
    """Order-independent tiebreak: a stable textual rendering of the value."""
    return repr(_freeze(obs.value))


def _time_preference_key(obs: Observation) -> tuple[int, float]:
    """Smaller means preferred: known timestamps first, then most recent first."""
    if obs.observed_at is None:
        return (1, 0.0)
    return (0, -_timestamp(obs.observed_at))


def _obs_order_key(obs: Observation, priority: Mapping[str, int]) -> tuple[Any, ...]:
    return (
        _timestamp_or_none(obs.observed_at),
        _priority_of(obs, priority, len(priority)),
        obs.source or "",
        obs.kind.value,
        _stable_value_key(obs),
    )


def _obs_order_key_no_priority(obs: Observation) -> tuple[Any, ...]:
    return (
        _timestamp_or_none(obs.observed_at),
        obs.source or "",
        obs.kind.value,
        _stable_value_key(obs),
    )


def _timestamp_or_none(observed_at: datetime | None) -> float:
    if observed_at is None:
        return float("-inf")
    return _timestamp(observed_at)


def _timestamp(observed_at: datetime) -> float:
    # Naive datetimes are interpreted as UTC for ordering purposes only.
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return observed_at.timestamp()


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    msg = f"observed_at must be a datetime or ISO-8601 string, got {type(value)!r}"
    raise TypeError(msg)


def _classify(value: Any, empty_list_mode: EmptyListMode) -> ObservationKind:
    if _is_empty_container(value):
        return ObservationKind.EMPTY_LIST
    if empty_list_mode == "empty_value" and _is_empty_scalar(value):
        return ObservationKind.EMPTY_LIST
    if _is_empty_scalar(value):
        return ObservationKind.EMPTY
    return ObservationKind.VALUE


def _is_empty_scalar(value: Any) -> bool:
    return value is None or value == ""


def _is_empty_container(value: Any) -> bool:
    return isinstance(value, (list, tuple, set, dict, frozenset)) and len(value) == 0


def _freeze(value: Any) -> Any:
    """Return a hashable, order-insensitive snapshot of a value for equality."""
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, frozenset):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, float) and math.isnan(value):
        return _NAN_SENTINEL
    return value
