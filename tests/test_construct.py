from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, TypedDict

import pytest

from itemadapter import ItemAdapter, ItemConstructionError, build_item

try:
    import attrs
except ImportError:
    attrs = None

try:
    from pydantic import BaseModel, ConfigDict, Field, ValidationError
except ImportError:
    BaseModel = None

requires_attrs = pytest.mark.skipif(attrs is None, reason="attrs module is not available")
requires_pydantic = pytest.mark.skipif(BaseModel is None, reason="pydantic module is not available")


@dataclass
class DCInner:
    v: int
    label: str | None = None


@dataclass
class DCOuter:
    inner: DCInner
    items: list[DCInner]
    mapping: dict[str, DCInner]
    optional: DCInner | None
    name: str = "default"


@dataclass(frozen=True)
class FrozenInner:
    v: int

    def __hash__(self) -> int:
        return hash(self.v)


@dataclass
class ContainerRecord:
    as_list: list[FrozenInner]
    as_tuple: tuple[FrozenInner, ...]
    as_set: set[FrozenInner]
    as_map: dict[str, FrozenInner]


@dataclass
class OtherInner:
    text: str


@dataclass
class UnionRecord:
    ambiguous: FrozenInner | OtherInner
    optional: FrozenInner | None


class PlainTable(TypedDict):
    v: int


@dataclass
class TypedDictRecord:
    table: PlainTable


@dataclass
class InitVarRecord:
    base: int
    doubled: int = field(init=False)
    nested_init: dataclasses.InitVar[DCInner | None] = None

    def __post_init__(self, nested_init: DCInner | None) -> None:
        self.doubled = self.base * 2
        self.seen = nested_init


if attrs is not None:

    @attrs.define
    class AttrsInner:
        v: int

    @attrs.define
    class AttrsOuter:
        inner: AttrsInner
        items: list[AttrsInner]
        optional: AttrsInner | None

    @attrs.define
    class AttrsAliasRecord:
        _value: int = attrs.field(alias="value")

    @attrs.frozen
    class AttrsFrozenRecord:
        value: int

else:
    AttrsInner = None
    AttrsOuter = None
    AttrsAliasRecord = None
    AttrsFrozenRecord = None


if BaseModel is not None:

    class PydanticInner(BaseModel):
        v: int

    class PydanticOuter(BaseModel):
        inner: PydanticInner
        items: list[PydanticInner]
        optional: PydanticInner | None = None

    class PydanticFrozenRecord(BaseModel):
        model_config = ConfigDict(frozen=True)
        value: int

else:
    PydanticInner = None
    PydanticOuter = None
    PydanticFrozenRecord = None


def test_entrypoints_are_equivalent() -> None:
    data = {"inner": {"v": 1}, "items": [], "mapping": {}, "optional": None}
    assert ItemAdapter.build_item(DCOuter, data) == build_item(DCOuter, data)


def test_none_and_empty_input_are_equivalent() -> None:
    @dataclass
    class WithDefaults:
        a: int = 1
        b: list = field(default_factory=list)

    assert build_item(WithDefaults) == build_item(WithDefaults, None) == build_item(WithDefaults, {})


def test_dataclass_matches_direct_construction() -> None:
    data = {
        "inner": {"v": 1, "label": "x"},
        "items": [{"v": 2}, {"v": 3, "label": "z"}],
        "mapping": {"k": {"v": 4}},
        "optional": {"v": 5},
        "name": "built",
    }
    built = build_item(DCOuter, data)
    direct = DCOuter(
        inner=DCInner(v=1, label="x"),
        items=[DCInner(v=2), DCInner(v=3, label="z")],
        mapping={"k": DCInner(v=4)},
        optional=DCInner(v=5),
        name="built",
    )
    assert built == direct
    assert type(built) is type(direct)
    assert isinstance(built.inner, DCInner)
    assert all(isinstance(item, DCInner) for item in built.items)
    assert isinstance(built.mapping["k"], DCInner)
    assert isinstance(built.optional, DCInner)


def test_existing_instances_are_passed_through_unchanged() -> None:
    inner = DCInner(v=1)
    data = {"inner": inner, "items": [inner], "mapping": {"k": inner}, "optional": inner}
    built = build_item(DCOuter, data)
    assert built.inner is inner
    assert built.items[0] is inner
    assert built.mapping["k"] is inner
    assert built.optional is inner


def test_container_types_are_preserved_but_elements_are_built() -> None:
    data = {
        "as_list": ({"v": 1},),
        "as_tuple": [{"v": 2}],
        "as_set": [{"v": 3}],
        "as_map": {"k": {"v": 4}},
    }
    built = build_item(ContainerRecord, data)
    direct = ContainerRecord(
        as_list=(FrozenInner(v=1),),
        as_tuple=[FrozenInner(v=2)],
        as_set=[FrozenInner(v=3)],
        as_map={"k": FrozenInner(v=4)},
    )
    assert built == direct
    assert type(built.as_list) is tuple
    assert type(built.as_tuple) is list
    assert type(built.as_set) is list
    assert type(built.as_map) is dict
    assert isinstance(built.as_list[0], FrozenInner)
    assert isinstance(built.as_tuple[0], FrozenInner)
    assert isinstance(next(iter(built.as_set)), FrozenInner)
    assert isinstance(built.as_map["k"], FrozenInner)


def test_ambiguous_union_is_not_guessed_but_optional_union_is_built() -> None:
    ambiguous_mapping = {"v": 1}
    built = build_item(UnionRecord, {"ambiguous": ambiguous_mapping, "optional": {"v": 2}})
    # No constructor is called speculatively for a union of several item types.
    assert built.ambiguous is ambiguous_mapping
    assert built.optional == FrozenInner(v=2)
    assert isinstance(built.optional, FrozenInner)


def test_explicit_none_on_optional_nested_field_is_passed_through() -> None:
    built = build_item(UnionRecord, {"ambiguous": FrozenInner(v=1), "optional": None})
    assert built.optional is None


def test_missing_key_uses_default_but_explicit_none_is_passed() -> None:
    @dataclass
    class Record:
        required: str
        with_default: str = "d"
        with_factory: list = field(default_factory=list)

    missing = build_item(Record, {"required": "r"})
    assert missing.with_default == "d"
    assert missing.with_factory == []

    explicit_none = build_item(
        Record, {"required": "r", "with_default": None, "with_factory": None}
    )
    assert explicit_none.with_default is None
    assert explicit_none.with_factory is None



def test_default_factory_is_called_once_per_construction() -> None:
    calls = {"n": 0}

    def factory() -> list:
        calls["n"] += 1
        return []

    @dataclass
    class Record:
        made: list = field(default_factory=factory)

    first = build_item(Record, {})
    second = build_item(Record, {})
    assert calls["n"] == 2
    assert first.made is not second.made
    assert build_item(Record, {"made": ["x"]}).made == ["x"]
    assert build_item(Record, {"made": None}).made is None
    assert calls["n"] == 2  # providing a value, including None, skips the factory


def test_constructor_runs_exactly_once_and_native_error_propagates() -> None:
    runs = {"n": 0}

    @dataclass
    class Record:
        value: int

        def __post_init__(self) -> None:
            runs["n"] += 1

    built = build_item(Record, {"value": 1})
    assert built.value == 1
    assert runs["n"] == 1

    class Boom(RuntimeError):
        pass

    @dataclass
    class Failing:
        def __post_init__(self) -> None:
            raise Boom("post init failure")

    with pytest.raises(Boom, match="post init failure"):
        build_item(Failing, {})
    assert runs["n"] == 1  # the failing record uses a different constructor


def test_kw_only_field_is_supported() -> None:
    @dataclass
    class Record:
        positional: int
        keyword_only: str = field(kw_only=True, default="k")

    assert build_item(Record, {"positional": 1}).keyword_only == "k"
    assert build_item(Record, {"positional": 1, "keyword_only": "x"}).keyword_only == "x"


def test_init_false_field_is_left_to_the_constructor() -> None:
    @dataclass
    class Record:
        shown: int
        hidden: int = field(init=False, default=42)

    built = build_item(Record, {"shown": 1})
    assert built == Record(shown=1)
    assert built.hidden == 42

    with pytest.raises(ItemConstructionError, match=r"Record\.hidden.*init=False"):
        build_item(Record, {"shown": 1, "hidden": 99})


def test_frozen_dataclass_behaves_like_direct_construction() -> None:
    @dataclass(frozen=True)
    class FrozenRecord:
        value: int

    built = build_item(FrozenRecord, {"value": 1})
    direct = FrozenRecord(value=1)
    assert built == direct
    with pytest.raises(dataclasses.FrozenInstanceError):
        built.value = 2


def test_dataclass_init_false_class_is_not_supported() -> None:
    @dataclass(init=False)
    class NoInit:
        value: int

    with pytest.raises(ItemConstructionError, match="init=False"):
        build_item(NoInit, {"value": 1})


def test_unknown_field_reports_path_and_known_fields() -> None:
    with pytest.raises(ItemConstructionError) as exc_info:
        build_item(DCOuter, {"inner": {"v": 1}, "items": [], "mapping": {}, "optional": None, "nope": 1})
    message = str(exc_info.value)
    assert "DCOuter.nope" in message
    assert "unknown input field" in message
    assert "inner" in message
    assert "items" in message


def test_nested_failures_report_their_full_path() -> None:
    with pytest.raises(ItemConstructionError, match=r"DCOuter\.inner\.bogus"):
        build_item(
            DCOuter,
            {"inner": {"v": 1, "bogus": 2}, "items": [], "mapping": {}, "optional": None},
        )
    with pytest.raises(ItemConstructionError, match=r"DCOuter\.items\.0\.bogus"):
        build_item(
            DCOuter,
            {"inner": {"v": 1}, "items": [{"v": 2, "bogus": 3}], "mapping": {}, "optional": None},
        )
    with pytest.raises(ItemConstructionError, match=r"DCOuter\.mapping\.k\.bogus"):
        build_item(
            DCOuter,
            {"inner": {"v": 1}, "items": [], "mapping": {"k": {"v": 4, "bogus": 5}}, "optional": None},
        )


def test_nested_non_mapping_value_reports_path_and_reason() -> None:
    with pytest.raises(ItemConstructionError, match=r"DCOuter\.inner.*expected a mapping.*got list"):
        build_item(
            DCOuter,
            {"inner": ["not", "a", "mapping"], "items": [], "mapping": {}, "optional": None},
        )


def test_native_constructor_error_is_not_wrapped() -> None:
    with pytest.raises(TypeError, match="required"):
        build_item(DCOuter, {})


def test_typed_dict_field_is_left_as_a_plain_dict() -> None:
    built = build_item(TypedDictRecord, {"table": {"v": 1}})
    assert built.table == {"v": 1}
    assert type(built.table) is dict


def test_unresolved_hints_fall_back_to_passing_values_through() -> None:
    # Classes defined locally with postponed annotations cannot resolve their
    # type hints: building must still work through the constructor, without
    # attempting nested construction.
    @dataclass
    class Record:
        value: int

    assert build_item(Record, {"value": 1}).value == 1


def test_init_var_is_accepted_and_its_nested_value_is_built() -> None:
    built = build_item(InitVarRecord, {"base": 3, "nested_init": {"v": 7}})
    direct = InitVarRecord(base=3, nested_init=DCInner(v=7))
    assert built == direct
    assert built.doubled == 6
    assert built.seen == DCInner(v=7)
    assert isinstance(built.seen, DCInner)

    missing = build_item(InitVarRecord, {"base": 3})
    assert missing.seen is None
    explicit_none = build_item(InitVarRecord, {"base": 3, "nested_init": None})
    assert explicit_none.seen is None


def test_unsupported_and_invalid_inputs() -> None:
    with pytest.raises(ItemConstructionError, match=r"is not supported"):
        build_item(dict, {"a": 1})
    with pytest.raises(ItemConstructionError, match="input must be a mapping"):
        build_item(DCOuter, [("inner", {"v": 1})])
    with pytest.raises(ItemConstructionError, match="expected an item class"):
        build_item("not a class", {})  # type: ignore[arg-type]


def test_custom_adapter_only_builds_item_classes_it_handles() -> None:
    from itemadapter.adapter import DataclassAdapter

    class DcOnlyAdapter(ItemAdapter):
        ADAPTER_CLASSES = (DataclassAdapter,)

    class Plain:
        pass

    assert DcOnlyAdapter.build_item(DCInner, {"v": 1}) == DCInner(v=1)
    with pytest.raises(ItemConstructionError, match="no adapter registered"):
        DcOnlyAdapter.build_item(Plain, {})


@requires_attrs
class TestAttrs:
    def test_matches_direct_construction(self) -> None:
        data = {"inner": {"v": 1}, "items": [{"v": 2}], "optional": None}
        built = build_item(AttrsOuter, data)
        direct = AttrsOuter(inner=AttrsInner(v=1), items=[AttrsInner(v=2)], optional=None)
        assert built == direct
        assert isinstance(built.inner, AttrsInner)
        assert isinstance(built.items[0], AttrsInner)

    def test_converter_runs_once_and_validator_error_propagates(self) -> None:
        conversions = {"n": 0}

        def counting_converter(value: Any) -> int:
            conversions["n"] += 1
            return int(value)

        @attrs.define
        class Record:
            number: int = attrs.field(converter=counting_converter)
            checked: int = attrs.field(validator=attrs.validators.instance_of(int))

        built = build_item(Record, {"number": "7", "checked": 1})
        assert built.number == 7
        assert conversions["n"] == 1

        with pytest.raises(TypeError):
            build_item(Record, {"number": "7", "checked": "bad"})
        assert conversions["n"] == 2  # the constructor ran once for each call

    def test_factory_counting(self) -> None:
        calls = {"n": 0}

        def factory() -> list:
            calls["n"] += 1
            return []

        @attrs.define
        class Record:
            made: list = attrs.field(factory=factory)

        first = build_item(Record, {})
        second = build_item(Record, {})
        assert calls["n"] == 2
        assert first.made is not second.made
        assert build_item(Record, {"made": None}).made is None
        assert calls["n"] == 2

    def test_init_false_field_is_left_to_the_constructor(self) -> None:
        @attrs.define
        class Record:
            shown: int
            hidden: int = attrs.field(init=False, default=42)

        assert build_item(Record, {"shown": 1}).hidden == 42
        with pytest.raises(ItemConstructionError, match=r"Record\.hidden.*init=False"):
            build_item(Record, {"shown": 1, "hidden": 9})

    def test_kw_only_field_is_supported(self) -> None:
        @attrs.define
        class Record:
            positional: int
            keyword_only: int = attrs.field(kw_only=True, default=5)

        assert build_item(Record, {"positional": 1}).keyword_only == 5
        assert build_item(Record, {"positional": 1, "keyword_only": 9}).keyword_only == 9

    def test_field_alias_is_used_for_the_constructor(self) -> None:
        built = build_item(AttrsAliasRecord, {"value": 3})
        assert built == AttrsAliasRecord(value=3)
        assert built._value == 3

    def test_alias_and_name_for_the_same_field_cannot_both_be_given(self) -> None:
        with pytest.raises(ItemConstructionError, match=r"already set"):
            build_item(AttrsAliasRecord, {"value": 1, "_value": 2})

    def test_frozen_record_behaves_like_direct_construction(self) -> None:
        built = build_item(AttrsFrozenRecord, {"value": 1})
        assert built == AttrsFrozenRecord(value=1)
        with pytest.raises(attrs.exceptions.FrozenInstanceError):
            built.value = 2

    def test_init_false_class_is_not_supported(self) -> None:
        @attrs.define(init=False)
        class NoInit:
            value: int

        with pytest.raises(ItemConstructionError, match="init=False"):
            build_item(NoInit, {"value": 1})

    def test_nested_failure_reports_path(self) -> None:
        with pytest.raises(ItemConstructionError, match=r"AttrsOuter\.inner\.bogus"):
            build_item(AttrsOuter, {"inner": {"v": 1, "bogus": 2}})

    def test_unknown_field_reports_path(self) -> None:
        @attrs.define
        class Record:
            value: int

        with pytest.raises(ItemConstructionError, match=r"Record\.nope.*unknown input field"):
            build_item(Record, {"value": 1, "nope": 2})


@requires_pydantic
class TestPydantic:
    def test_matches_direct_construction(self) -> None:
        data = {"inner": {"v": 1}, "items": [{"v": 2}], "optional": None}
        built = build_item(PydanticOuter, data)
        assert built == PydanticOuter(inner=PydanticInner(v=1), items=[PydanticInner(v=2)])
        assert isinstance(built.inner, PydanticInner)

    def test_missing_and_explicit_none_differ(self) -> None:
        class Record(BaseModel):
            optional: int | None = 5

        assert build_item(Record, {}).optional == 5
        assert build_item(Record, {"optional": None}).optional is None

    def test_field_names_and_aliases_are_accepted(self) -> None:
        class Record(BaseModel):
            extra_: int = Field(default=0, alias="extra")
            named: int = Field(default=0, validation_alias="named_alias")
            model_config = ConfigDict(populate_by_name=True)

        by_alias = build_item(Record, {"extra": 1, "named_alias": 2})
        assert by_alias.extra_ == 1
        assert by_alias.named == 2
        by_name = build_item(Record, {"extra_": 3, "named": 4})
        assert by_name.extra_ == 3
        assert by_name.named == 4

    def test_validation_error_is_propagated_unchanged(self) -> None:
        class Record(BaseModel):
            value: int

        with pytest.raises(ValidationError):
            build_item(Record, {"value": "not an int"})

    def test_frozen_model_behaves_like_direct_construction(self) -> None:
        built = build_item(PydanticFrozenRecord, {"value": 1})
        assert built == PydanticFrozenRecord(value=1)
        with pytest.raises(ValidationError):
            built.value = 2

    def test_unknown_field_is_a_structural_error(self) -> None:
        class Record(BaseModel):
            value: int
            model_config = ConfigDict(extra="forbid")

        with pytest.raises(ItemConstructionError, match=r"Record\.nope.*unknown input field"):
            build_item(Record, {"value": 1, "nope": 2})

    def test_nested_failure_reports_path(self) -> None:
        with pytest.raises(ItemConstructionError, match=r"PydanticOuter\.inner\.bogus"):
            build_item(PydanticOuter, {"inner": {"v": 1, "bogus": 2}})
