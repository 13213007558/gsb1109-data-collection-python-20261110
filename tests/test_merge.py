from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import permutations

import pytest

from itemadapter import (
    OBSERVED_AT_KEY,
    SOURCE_KEY,
    AbsencePolicy,
    EmptyListPolicy,
    EmptyValuePolicy,
    FieldPolicies,
    MergeStrategy,
    ObservationKind,
    merge_records,
)
from tests import AttrsItem, PydanticModel, requires_attrs, requires_pydantic

T1 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
T3 = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)


def _surface(result):
    """Order-independent view of the deterministic merge output, including the
    exact ordering of conflict candidates and considered observations."""
    return (
        result.record,
        {name: (obs.source, obs.observed_at, obs.value) for name, obs in result.sources.items()},
        {
            name: (
                conflict.reason,
                # Intentionally not re-sorted: candidate ordering must itself be
                # independent of the input record order.
                [(obs.source, obs.observed_at, obs.value) for obs in conflict.candidates],
            )
            for name, conflict in result.conflicts.items()
        },
        {
            name: [
                (obs.source, obs.observed_at, obs.kind.value, obs.value)
                for obs in decision.considered
            ]
            for name, decision in result.decisions.items()
        },
    )


def _assert_order_independent(records, **kwargs):
    reference = _surface(merge_records(records, **kwargs))
    for permutation in permutations(records):
        assert _surface(merge_records(list(permutation), **kwargs)) == reference
    return merge_records(records, **kwargs)


class TestEmptyDoesNotOverwrite:
    def test_valid_price_survives_missing_and_empty_observations(self):
        # Same product seen from a listing page, a detail page and a merchant API.
        listing = {SOURCE_KEY: "listing", "price": None, "name": "widget"}
        detail = {SOURCE_KEY: "detail", "price": "", "name": "widget"}
        merchant = {SOURCE_KEY: "merchant", "price": 100, "name": "widget"}

        result = merge_records([listing, detail, merchant], strategy=MergeStrategy.LATEST)

        assert result.record == {"name": "widget", "price": 100}
        assert result.sources["price"].source == "merchant"
        assert not result.has_conflicts

    def test_empty_list_does_not_overwrite_populated_list(self):
        records = [
            {SOURCE_KEY: "a", "images": []},
            {SOURCE_KEY: "b", "images": ["x.png"]},
        ]
        result = merge_records(records)
        assert result.record["images"] == ["x.png"]
        assert result.sources["images"].source == "b"

    def test_missing_field_never_overrides_anything(self):
        records = [{"price": 100, SOURCE_KEY: "a"}, {"name": "x", SOURCE_KEY: "b"}]
        result = merge_records(records)
        assert result.record["price"] == 100
        assert result.record["name"] == "x"
        assert result.conflicts == {}


class TestStrategies:
    def test_latest_prefers_most_recent_observation(self):
        records = [
            {SOURCE_KEY: "listing", "price": 100, OBSERVED_AT_KEY: T1},
            {SOURCE_KEY: "detail", "price": 90, OBSERVED_AT_KEY: T3},
            {SOURCE_KEY: "merchant", "price": 95, OBSERVED_AT_KEY: T2},
        ]
        result = merge_records(records, strategy=MergeStrategy.LATEST)
        assert result.record["price"] == 90
        assert result.sources["price"].source == "detail"

    def test_trusted_source_prefers_priority_over_recency(self):
        records = [
            {"_source": "merchant", "price": 100, "_observed_at": T1},
            {"_source": "listing", "price": 90, "_observed_at": T3},
        ]
        result = merge_records(
            records,
            strategy=MergeStrategy.TRUSTED_SOURCE,
            source_priority=["merchant", "detail", "listing"],
        )
        assert result.record["price"] == 100
        assert result.sources["price"].source == "merchant"

    def test_trusted_source_unknown_sources_rank_below_listed_ones(self):
        records = [
            {"_source": "unknown", "price": 90, "_observed_at": T3},
            {"_source": "merchant", "price": 100, "_observed_at": T1},
        ]
        result = merge_records(
            records,
            strategy=MergeStrategy.TRUSTED_SOURCE,
            source_priority=["merchant"],
        )
        assert result.record["price"] == 100

    def test_require_consistent_adopts_unanimous_value(self):
        records = [
            {"_source": "a", "price": 100, "_observed_at": T1},
            {"_source": "b", "price": 100, "_observed_at": T2},
        ]
        result = merge_records(records, strategy=MergeStrategy.REQUIRE_CONSISTENT)
        assert result.record["price"] == 100
        assert not result.has_conflicts

    def test_require_consistent_keeps_conflict_on_disagreement(self):
        records = [
            {"_source": "a", "price": 100, "_observed_at": T1},
            {"_source": "b", "price": 101, "_observed_at": T2},
        ]
        result = merge_records(records, strategy=MergeStrategy.REQUIRE_CONSISTENT)
        assert "price" not in result.record
        assert "price" in result.conflicts
        conflict = result.conflicts["price"]
        assert conflict.reason == "unresolved_values"
        assert {obs.value for obs in conflict.candidates} == {100, 101}

    def test_trusted_source_requires_priority(self):
        with pytest.raises(ValueError, match="source_priority is required"):
            merge_records([{"price": 1}], strategy=MergeStrategy.TRUSTED_SOURCE)

    def test_duplicated_priority_is_rejected(self):
        with pytest.raises(ValueError, match="duplicated sources"):
            merge_records(
                [{"price": 1}],
                strategy=MergeStrategy.TRUSTED_SOURCE,
                source_priority=["a", "a"],
            )

    def test_invalid_strategy_and_mode(self):
        with pytest.raises(ValueError, match="is not a valid MergeStrategy"):
            merge_records([{"price": 1}], strategy="nonsense")
        with pytest.raises(ValueError, match="invalid empty_list_mode"):
            merge_records([{"price": 1}], empty_list_mode="nonsense")


class TestSameTimestampConflict:
    @pytest.mark.parametrize(
        "strategy",
        [MergeStrategy.LATEST, MergeStrategy.TRUSTED_SOURCE, MergeStrategy.REQUIRE_CONSISTENT],
    )
    def test_same_timestamp_different_values_always_conflict(self, strategy):
        kwargs = {}
        if strategy is MergeStrategy.TRUSTED_SOURCE:
            kwargs["source_priority"] = ["a", "b"]
        records = [
            {"_source": "a", "price": 100, "_observed_at": T2},
            {"_source": "b", "price": 200, "_observed_at": T2},
        ]
        result = merge_records(records, strategy=strategy, **kwargs)
        assert result.conflicts["price"].reason == "same_timestamp"
        assert "price" not in result.record
        assert {obs.value for obs in result.conflicts["price"].candidates} == {100, 200}

    def test_same_timestamp_same_value_is_not_a_conflict(self):
        records = [
            {"_source": "a", "price": 100, "_observed_at": T2},
            {"_source": "b", "price": 100, "_observed_at": T2},
        ]
        result = merge_records(records)
        assert result.record["price"] == 100
        assert not result.has_conflicts

    def test_conflict_only_for_the_sharing_timestamp_group(self):
        records = [
            {"_source": "a", "price": 100, "_observed_at": T1},
            {"_source": "b", "price": 200, "_observed_at": T1},
            {"_source": "c", "price": 300, "_observed_at": T2},
        ]
        result = merge_records(records, strategy=MergeStrategy.LATEST)
        assert result.conflicts["price"].reason == "same_timestamp"


class TestEmptinessPolicies:
    def test_three_kinds_of_emptiness_are_distinguished(self):
        records = [
            {"_source": "a", "price": None},  # empty value
            {"_source": "b", "images": []},  # empty list
            {"_source": "c"},  # missing both fields
        ]
        result = merge_records(records)
        assert result.record == {}
        assert result.conflicts == {}
        kinds_price = {obs.kind for obs in result.decisions["price"].considered}
        kinds_images = {obs.kind for obs in result.decisions["images"].considered}
        assert kinds_price == {ObservationKind.EMPTY}
        assert kinds_images == {ObservationKind.EMPTY_LIST}

    def test_absence_conflict_fires_only_for_missing_fields(self):
        policies = FieldPolicies(absence=AbsencePolicy.CONFLICT)
        records = [
            {"_source": "a", "price": 100, "stock": None},
            {"_source": "b", "price": 100},
        ]
        result = merge_records(
            records,
            field_policies={"stock": policies},
            default_policies=policies,
        )
        assert "stock" in result.conflicts
        assert result.conflicts["stock"].reason == "absent"
        assert "price" not in result.conflicts
        assert result.record["price"] == 100

    def test_empty_value_conflict_ignores_missing_and_empty_lists(self):
        policies = FieldPolicies(
            absence=AbsencePolicy.IGNORE,
            empty_value=EmptyValuePolicy.CONFLICT,
            empty_list=EmptyListPolicy.IGNORE,
        )
        records = [
            {"_source": "a", "name": None, "tags": []},
            {"_source": "b", "tags": []},
        ]
        result = merge_records(records, default_policies=policies)
        assert result.conflicts["name"].reason == "empty_value"
        assert "tags" not in result.conflicts

    def test_empty_list_conflict_and_adopt(self):
        conflict_policies = FieldPolicies(empty_list=EmptyListPolicy.CONFLICT)
        records = [{"_source": "a", "tags": []}, {"_source": "b", "tags": []}]
        result = merge_records(records, default_policies=conflict_policies)
        assert result.conflicts["tags"].reason == "empty_list"
        assert "tags" not in result.record

        adopt_policies = FieldPolicies(empty_list=EmptyListPolicy.ADOPT)
        adopted = merge_records(records, default_policies=adopt_policies)
        assert adopted.record == {"tags": []}
        assert adopted.sources["tags"].source == "a"

    def test_adopt_does_not_beat_a_real_value(self):
        policies = FieldPolicies(empty_list=EmptyListPolicy.ADOPT)
        records = [{"_source": "a", "tags": []}, {"_source": "b", "tags": ["x"]}]
        result = merge_records(records, default_policies=policies)
        assert result.record["tags"] == ["x"]
        assert not result.has_conflicts

    def test_combined_emptiness_reasons(self):
        policies = FieldPolicies(
            absence=AbsencePolicy.CONFLICT,
            empty_value=EmptyValuePolicy.CONFLICT,
        )
        records = [{"_source": "a", "name": None}, {"_source": "b"}]
        result = merge_records(records, default_policies=policies)
        assert result.conflicts["name"].reason == "absent+empty_value"

    def test_empty_list_mode_treats_scalars_as_lists(self):
        policies = FieldPolicies(empty_list=EmptyListPolicy.CONFLICT)
        records = [{"_source": "a", "name": None}]
        result = merge_records(
            records,
            default_policies=policies,
            empty_list_mode="empty_value",
        )
        assert result.conflicts["name"].reason == "empty_list"


class TestDeterminism:
    def test_input_order_does_not_change_result(self):
        records = [
            {"_source": "listing", "price": 100, "name": "w", "_observed_at": T1},
            {"_source": "detail", "price": 90, "name": "w", "_observed_at": T3},
            {"_source": "merchant", "price": None, "images": [], "_observed_at": T2},
        ]
        _assert_order_independent(records, strategy=MergeStrategy.LATEST)
        _assert_order_independent(
            records,
            strategy=MergeStrategy.TRUSTED_SOURCE,
            source_priority=["merchant", "detail", "listing"],
        )
        _assert_order_independent(records, strategy=MergeStrategy.REQUIRE_CONSISTENT)

    def test_set_compares_order_insensitive(self):
        records = [
            {"_source": "a", "tags": {"x", "y"}, "_observed_at": T1},
            {"_source": "b", "tags": {"y", "x"}, "_observed_at": T2},
        ]
        result = merge_records(records, strategy=MergeStrategy.REQUIRE_CONSISTENT)
        assert result.record["tags"] == {"x", "y"}
        assert not result.has_conflicts

    def test_full_ties_are_resolved_by_value_not_input_position(self):
        # Same source and no timestamps: time and source keys tie, so only the
        # value can break ties. Shuffling must not change which value is adopted.
        records = [
            {"_source": "s", "price": 30},
            {"_source": "s", "price": 10},
            {"_source": "s", "price": 20},
        ]
        result = _assert_order_independent(records, strategy=MergeStrategy.LATEST)
        assert result.record["price"] == 10
        assert result.sources["price"].source == "s"

    def test_conflict_candidate_order_is_input_order_independent(self):
        records = [
            {"_source": "a", "price": 100, "_observed_at": T1},
            {"_source": "b", "price": 200, "_observed_at": T1},
            {"_source": "c", "price": 300, "_observed_at": T1},
        ]
        result = _assert_order_independent(records)
        assert [obs.value for obs in result.conflicts["price"].candidates] == [100, 200, 300]


class TestProvenance:
    def test_every_adopted_field_keeps_its_source_and_timestamp(self):
        records = [
            {"_source": "a", "price": 100, "_observed_at": T1},
            {"_source": "b", "price": 90, "_observed_at": T2},
        ]
        result = merge_records(records)
        adopted = result.sources["price"]
        assert adopted.source == "b"
        assert adopted.observed_at == T2
        assert adopted.value == 90

    def test_iso_string_timestamps_are_accepted(self):
        records = [
            {"_source": "a", "price": 100, "_observed_at": "2026-11-01T10:00:00+00:00"},
            {"_source": "b", "price": 90, "_observed_at": "2026-11-02T10:00:00+00:00"},
        ]
        result = merge_records(records)
        assert result.record["price"] == 90

    def test_invalid_timestamp_type_raises(self):
        with pytest.raises(TypeError):
            merge_records([{"_source": "a", "price": 1, "_observed_at": 12345}])

    def test_reserved_keys_are_not_merged_as_fields(self):
        result = merge_records([{"_source": "a", "_observed_at": T1, "price": 1}])
        assert SOURCE_KEY not in result.record
        assert "_observed_at" not in result.record
        assert set(result.record) == {"price"}

    def test_per_field_metadata_overrides_record_provenance(self):
        @dataclass
        class Observed:
            price: int | None = field(
                default=None,
                metadata={"source": "field-source", "observed_at": T3},
            )

        records = [
            {"_source": "record-source", "_observed_at": T1, "price": 100},
            Observed(price=90),
        ]
        result = merge_records(records, strategy=MergeStrategy.LATEST)
        assert result.record["price"] == 90
        assert result.sources["price"].source == "field-source"
        assert result.sources["price"].observed_at == T3


class TestItemTypes:
    def test_dict_records(self):
        result = merge_records([{"price": 100}, {"price": 100}])
        assert result.record["price"] == 100

    def test_dataclass_records(self):
        @dataclass
        class Product:
            price: int | None = None
            name: str | None = None

        result = merge_records([Product(price=100, name="x"), Product(price=None, name="x")])
        assert result.record == {"name": "x", "price": 100}

    @requires_attrs
    def test_attrs_records(self):
        result = merge_records([AttrsItem(name="x", value=1), AttrsItem(name="x", value=1)])
        assert result.record == {"name": "x", "value": 1}

    @requires_pydantic
    def test_pydantic_records(self):
        result = merge_records(
            [PydanticModel(name="x", value=1), PydanticModel(name="x", value=2)],
            strategy=MergeStrategy.REQUIRE_CONSISTENT,
        )
        assert result.conflicts["value"].reason == "unresolved_values"
        assert result.record["name"] == "x"


class TestResultShape:
    def test_empty_input(self):
        result = merge_records([])
        assert result.record == {}
        assert result.sources == {}
        assert result.conflicts == {}
        assert result.decisions == {}
        assert not result.has_conflicts

    def test_conflicting_field_is_excluded_others_merged(self):
        records = [
            {"_source": "a", "price": 100, "name": "w", "_observed_at": T1},
            {"_source": "b", "price": 200, "name": "w", "_observed_at": T1},
        ]
        result = merge_records(records)
        assert result.has_conflicts
        assert result.record == {"name": "w"}
        decision = result.decisions["price"]
        assert decision.adopted is None
        assert decision.conflict is result.conflicts["price"]
        assert len(decision.considered) == 2

    def test_candidate_record_can_be_returned_with_conflicts(self):
        records = [
            {"_source": "a", "price": 100, "_observed_at": T1},
            {"_source": "b", "price": 200, "_observed_at": T1},
            {"_source": "c", "name": "widget", "_observed_at": T1},
        ]
        result = merge_records(records)
        # Business layer receives a usable partial record plus the conflict trail,
        # and decides itself whether anything is written to storage.
        assert result.record == {"name": "widget"}
        assert set(result.conflicts) == {"price"}
        assert {obs.source for obs in result.conflicts["price"].candidates} == {"a", "b"}
