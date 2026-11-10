from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Mapping
from contextlib import suppress
from types import UnionType
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin, get_type_hints

from itemadapter._imports import _is_typeddict, attrs
from itemadapter._utils import _is_attrs_class, _is_pydantic_model

if TYPE_CHECKING:
    from itemadapter.adapter import ItemAdapter

__all__ = ["ItemConstructionError", "build_item"]

_UNION_ORIGINS = {Union, UnionType}
_SEQUENCE_ORIGINS = {list, set, frozenset, tuple}


class ItemConstructionError(TypeError):
    """Raised when an item cannot be built from the given input.

    Messages always identify the offending field with a dotted path that starts
    at the top-level item class, and explain the reason.
    """


def build_item(item_class: type, data: Mapping[str, Any] | None = None, /) -> Any:
    """Build an instance of *item_class* from *data* through its constructor.

    Unlike setting attributes on an already created instance, this goes through
    the constructor of the target type, so converters, validators and any other
    initialization logic defined by the type are executed exactly once, as in a
    direct construction.

    * Missing keys and keys explicitly set to ``None`` are not equivalent: a
      missing key is not passed to the constructor, so the field keeps its
      default or default factory, while an explicit ``None`` is passed as-is.
    * Fields whose type is another supported item class are built recursively
      from mapping inputs. The same applies to item classes nested inside
      containers (``list``, ``set``, ``frozenset``, ``tuple`` and ``dict``) and
      to optional (``T | None``) fields.
    * Fields that cannot be set through the constructor, e.g. dataclass or
      attrs fields declared with ``init=False``, are never assigned directly:
      they are left to the constructor, which is also what a direct, valid
      construction does.

    Structural problems detected before calling the constructor raise
    :class:`ItemConstructionError` with the path of the offending field. Errors
    raised by the constructor itself (validation, conversion, missing required
    arguments, ...) are propagated unchanged, so each type keeps its own
    initialization conventions.

    :raises ItemConstructionError: if *item_class* cannot be built from a
        mapping, if *data* is not a mapping, if it contains an unknown field, if
        a non-initializable field receives a value, or if a nested item cannot
        be built.
    """
    from itemadapter.adapter import ItemAdapter

    return build_item_for_adapter(ItemAdapter, item_class, data)


def build_item_for_adapter(
    adapter_cls: type[ItemAdapter],
    item_class: type,
    data: Mapping[str, Any] | None,
) -> Any:
    if not isinstance(item_class, type):
        msg = f"expected an item class, got {item_class!r}"
        raise ItemConstructionError(msg)
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        msg = f"{item_class.__name__}: input must be a mapping, got {type(data).__name__}"
        raise ItemConstructionError(msg)
    if not adapter_cls.is_item_class(item_class):
        msg = (
            f"{item_class.__name__}: no adapter registered on "
            f"{adapter_cls.__name__} handles this item class, it cannot be built"
        )
        raise ItemConstructionError(msg)
    return _build_item(adapter_cls, item_class, data, item_class.__name__)


def _build_item(
    adapter_cls: type[ItemAdapter], item_class: type, data: Mapping[str, Any], prefix: str
) -> Any:
    if dataclasses.is_dataclass(item_class):
        return _build_dataclass(adapter_cls, item_class, data, prefix)
    if _is_attrs_class(item_class):
        return _build_attrs(adapter_cls, item_class, data, prefix)
    if _is_pydantic_model(item_class):
        return _build_pydantic(adapter_cls, item_class, data, prefix)
    msg = (
        f"{prefix}: constructing items of type {item_class.__name__} is not "
        "supported, only dataclass, attrs and pydantic item classes can be "
        "built from a mapping"
    )
    raise ItemConstructionError(msg)


def _runtime_field_names(item_class: type) -> list[str]:
    if _is_pydantic_model(item_class):
        return list(item_class.model_fields)  # type: ignore[attr-defined]
    if _is_attrs_class(item_class):
        return [attribute.name for attribute in attrs.fields(item_class)]
    if dataclasses.is_dataclass(item_class):
        return [field.name for field in dataclasses.fields(item_class)]
    return []


def _unknown_field_error(
    item_class: type, prefix: str, field_name: str
) -> ItemConstructionError:
    known = _runtime_field_names(item_class)
    known_str = ", ".join(known) if known else "none"
    msg = (
        f"{prefix}.{field_name}: unknown input field, {item_class.__name__} "
        f"defines: {known_str}"
    )
    return ItemConstructionError(msg)


def _non_initializable_field_error(prefix: str, field_name: str) -> ItemConstructionError:
    msg = (
        f"{prefix}.{field_name}: field cannot be set from the input because it "
        "is not part of the constructor (init=False); it is only set through "
        "the constructor's own logic"
    )
    return ItemConstructionError(msg)


def _convert_value(
    adapter_cls: type[ItemAdapter],
    value: Any,
    type_hint: Any,
    prefix: str,
) -> Any:
    if value is None:
        return None
    if isinstance(type_hint, dataclasses.InitVar):
        # dataclasses.InitVar only exists as a constructor parameter; the inner
        # type is what the constructor actually receives.
        return _convert_value(adapter_cls, value, type_hint.type, prefix)
    origin = get_origin(type_hint)
    if origin in _UNION_ORIGINS:
        return _convert_union(adapter_cls, value, type_hint, prefix)
    if origin is not None:
        return _convert_generic(adapter_cls, value, type_hint, origin, prefix)
    return _convert_single(adapter_cls, value, type_hint, prefix)


def _init_keyword_parameters(item_class: type) -> set[str]:
    """Names of the constructor parameters that can be passed by keyword.
    Used to accept constructor-only inputs such as dataclasses ``InitVar``
    fields, which are not listed as regular fields."""
    try:
        parameters = inspect.signature(item_class).parameters
    except (TypeError, ValueError):
        return set()
    return {
        name
        for name, parameter in parameters.items()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }


def _is_constructible_item_class(target: Any) -> bool:
    return (
        dataclasses.is_dataclass(target)
        or _is_attrs_class(target)
        or _is_pydantic_model(target)
    )


def _resolve_type_hints(item_class: type) -> dict[str, Any] | None:
    """Return the resolved type hints of *item_class*, or ``None`` if they
    cannot be resolved, e.g. for classes defined inside another function with
    postponed annotations. When hints are unavailable, nested items are simply
    not built automatically and values are passed to the constructor as-is."""
    try:
        return get_type_hints(item_class)
    except NameError:
        return None


def _convert_single(
    adapter_cls: type[ItemAdapter], value: Any, target: Any, prefix: str
) -> Any:
    if not isinstance(target, type) or _is_typeddict(target):
        # TypedDict fields are plain dicts at run time and cannot be built.
        return value
    if not _is_constructible_item_class(target) or not adapter_cls.is_item_class(target):
        return value
    if isinstance(value, target):
        return value
    if not isinstance(value, Mapping):
        msg = (
            f"{prefix}: expected a mapping to build a {target.__name__} item, "
            f"got {type(value).__name__}"
        )
        raise ItemConstructionError(msg)
    return _build_item(adapter_cls, target, value, prefix)


def _convert_union(
    adapter_cls: type[ItemAdapter], value: Any, type_hint: Any, prefix: str
) -> Any:
    # Only unambiguous unions are converted: those with a single non-None
    # member. Unions of several item classes are passed through untouched, so no
    # constructor is ever called speculatively to guess the right member.
    members = [member for member in get_args(type_hint) if member is not type(None)]
    if len(members) == 1:
        return _convert_value(adapter_cls, value, members[0], prefix)
    return value


def _convert_generic(
    adapter_cls: type[ItemAdapter],
    value: Any,
    type_hint: Any,
    origin: Any,
    prefix: str,
) -> Any:
    normalized_origin = getattr(origin, "__origin__", origin)
    args = get_args(type_hint)
    if normalized_origin in _SEQUENCE_ORIGINS:
        return _convert_sequence(adapter_cls, value, args, prefix)
    if normalized_origin is dict:
        if not isinstance(value, Mapping) or len(args) != 2:
            return value
        value_hint = args[1]
        return {
            key: _convert_value(
                adapter_cls, element, value_hint, f"{prefix}.{key}"
            )
            for key, element in value.items()
        }
    return value


def _convert_sequence(
    adapter_cls: type[ItemAdapter],
    value: Any,
    args: tuple[Any, ...],
    prefix: str,
) -> Any:
    # The concrete input container type is preserved, as a direct constructor
    # call would do: only the elements are converted.
    if not isinstance(value, (list, set, frozenset, tuple)) or not args:
        return value
    if isinstance(value, tuple) and args[-1] is not Ellipsis:
        # Fixed-length tuple: each position has its own type hint.
        if len(args) != len(value):
            return value
        return tuple(
            _convert_value(
                adapter_cls, element, hint, f"{prefix}.{index}"
            )
            for index, (element, hint) in enumerate(zip(value, args, strict=True))
        )
    item_hints = args[:-1] if args[-1] is Ellipsis else args
    hint = item_hints[0] if len(item_hints) == 1 else Any
    converted = (
        _convert_value(adapter_cls, element, hint, f"{prefix}.{index}")
        for index, element in enumerate(value)
    )
    if isinstance(value, tuple):
        return tuple(converted)
    if isinstance(value, list):
        return list(converted)
    return type(value)(converted)


def _has_no_constructor(item_class: type) -> bool:
    # dataclasses and attrs leave object.__init__ in place when the class is
    # declared with init=False and does not define its own __init__.
    return getattr(item_class, "__init__", object.__init__) is object.__init__


def _build_dataclass(
    adapter_cls: type[ItemAdapter], item_class: type, data: Mapping[str, Any], prefix: str
) -> Any:
    if _has_no_constructor(item_class):
        msg = (
            f"{prefix}: dataclass is declared with init=False, it has no "
            "constructor to build it from a mapping"
        )
        raise ItemConstructionError(msg)
    type_hints = _resolve_type_hints(item_class)
    fields = {field.name: field for field in dataclasses.fields(item_class)}
    init_only_parameters = _init_keyword_parameters(item_class)
    kwargs: dict[str, Any] = {}
    for field_name, raw_value in data.items():
        field = fields.get(field_name)
        if field is not None:
            if not field.init:
                raise _non_initializable_field_error(prefix, field_name)
        elif field_name not in init_only_parameters:
            raise _unknown_field_error(item_class, prefix, field_name)
        value = raw_value
        if type_hints is not None:
            value = _convert_value(
                adapter_cls,
                value,
                type_hints.get(field_name, Any),
                f"{prefix}.{field_name}",
            )
        kwargs[field_name] = value
    return item_class(**kwargs)


def _build_attrs(
    adapter_cls: type[ItemAdapter], item_class: type, data: Mapping[str, Any], prefix: str
) -> Any:
    if _has_no_constructor(item_class):
        msg = (
            f"{prefix}: attrs class is declared with init=False, it has no "
            "generated constructor to build it from a mapping"
        )
        raise ItemConstructionError(msg)
    with suppress(NameError):
        attrs.resolve_types(item_class)
    type_hints = _resolve_type_hints(item_class)
    attributes = list(attrs.fields(item_class))
    by_alias = {attribute.alias: attribute for attribute in attributes}
    by_name = {attribute.name: attribute for attribute in attributes}
    kwargs: dict[str, Any] = {}
    for field_name, raw_value in data.items():
        attribute = by_alias.get(field_name) or by_name.get(field_name)
        if attribute is None:
            raise _unknown_field_error(item_class, prefix, field_name)
        if not attribute.init:
            raise _non_initializable_field_error(prefix, field_name)
        if attribute.alias in kwargs:
            msg = (
                f"{prefix}.{field_name}: input field maps to constructor "
                f"argument {attribute.alias!r}, which is already set"
            )
            raise ItemConstructionError(msg)
        value = raw_value
        if type_hints is not None:
            value = _convert_value(
                adapter_cls,
                value,
                type_hints.get(attribute.name, Any),
                f"{prefix}.{field_name}",
            )
        kwargs[attribute.alias] = value
    return item_class(**kwargs)


def _build_pydantic(
    adapter_cls: type[ItemAdapter], item_class: type, data: Mapping[str, Any], prefix: str
) -> Any:
    input_fields = _pydantic_input_fields(item_class)
    kwargs: dict[str, Any] = {}
    for field_name, value in data.items():
        field_info = input_fields.get(field_name)
        if field_info is None:
            raise _unknown_field_error(item_class, prefix, field_name)
        kwargs[field_name] = _convert_value(
            adapter_cls,
            value,
            field_info.annotation,
            f"{prefix}.{field_name}",
        )
    return item_class(**kwargs)


def _pydantic_input_fields(item_class: type) -> dict[str, Any]:
    """Map every accepted constructor input name (field names and aliases) to
    its pydantic field info."""
    populate_by_name = item_class.model_config.get(  # type: ignore[attr-defined]
        "populate_by_name", False
    )
    input_fields: dict[str, Any] = {}
    for field_name, field_info in item_class.model_fields.items():  # type: ignore[attr-defined]
        accepts_field_name = populate_by_name
        validation_alias = getattr(field_info, "validation_alias", None)
        choices = getattr(validation_alias, "choices", None)
        if choices:
            for choice in choices:
                input_fields[str(choice)] = field_info
        elif validation_alias is not None:
            input_fields[str(validation_alias)] = field_info
        elif getattr(field_info, "alias", None) is not None:
            input_fields[str(field_info.alias)] = field_info
        else:
            accepts_field_name = True
        if accepts_field_name:
            input_fields.setdefault(field_name, field_info)
    return input_fields
