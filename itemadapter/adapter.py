from __future__ import annotations

import dataclasses
import warnings
import weakref
from abc import ABCMeta, abstractmethod
from collections.abc import Iterable, Iterator, KeysView, MutableMapping
from functools import lru_cache
from types import MappingProxyType
from typing import Any, ClassVar, cast

from itemadapter._imports import _is_typeddict, _scrapy_item_classes, attrs
from itemadapter._json_schema import (
    _json_schema_from_attrs,
    _json_schema_from_dataclass,
    _json_schema_from_item_class,
    _json_schema_from_pydantic,
    _json_schema_from_typed_dict,
    _JsonSchemaState,
    _setdefault_attribute_docstrings_on_json_schema,
    _setdefault_attribute_types_on_json_schema,
)
from itemadapter._utils import (
    _get_pydantic_model_metadata,
    _get_typed_dict_field_metadata,
    _is_attrs_class,
    _is_pydantic_model,
)

__all__ = [
    "AdapterInterface",
    "AdapterRegistry",
    "AttrsAdapter",
    "DataclassAdapter",
    "DictAdapter",
    "ItemAdapter",
    "PydanticAdapter",
    "ScrapyItemAdapter",
]


class AdapterInterface(MutableMapping[str, Any], metaclass=ABCMeta):
    """Abstract Base Class for adapters.

    An adapter that handles a specific type of item should inherit from this
    class and implement the abstract methods defined here, plus the
    abtract methods inherited from the MutableMapping base class.
    """

    def __init__(self, item: Any) -> None:
        self.item = item

    @classmethod
    @abstractmethod
    def is_item_class(cls, item_class: type) -> bool:
        """Return True if the adapter can handle the given item class, False otherwise."""
        raise NotImplementedError

    @classmethod
    def is_item(cls, item: object) -> bool:
        """Return True if the adapter can handle the given item, False otherwise."""
        return cls.is_item_class(item.__class__)

    @classmethod
    def get_field_meta_from_class(
        cls, item_class: type, field_name: str
    ) -> MappingProxyType[str, Any]:
        return MappingProxyType({})

    @classmethod
    def get_field_names_from_class(cls, item_class: type) -> list[str] | None:
        """Return a list of fields defined for ``item_class``.
        If a class doesn't support fields, None is returned."""
        return None

    @classmethod
    def get_json_schema(
        cls, item_class: type, *, _state: _JsonSchemaState | None = None
    ) -> dict[str, Any]:
        return _json_schema_from_item_class(cls, item_class, _state)

    def get_field_meta(self, field_name: str) -> MappingProxyType[str, Any]:
        """Return metadata for the given field name, if available."""
        return self.get_field_meta_from_class(self.item.__class__, field_name)

    def field_names(self) -> KeysView[str]:
        """Return a dynamic view of the item's field names."""
        return self.keys()


class _MixinAttrsDataclassAdapter:
    _fields_dict: dict[str, Any]
    item: Any

    def get_field_meta(self, field_name: str) -> MappingProxyType[str, Any]:
        return cast("MappingProxyType[str, Any]", self._fields_dict[field_name].metadata)

    def field_names(self) -> KeysView[str]:
        return KeysView(self._fields_dict)

    def __getitem__(self, field_name: str) -> Any:
        if field_name in self._fields_dict:
            try:
                return getattr(self.item, field_name)
            except AttributeError as ex:
                raise KeyError(field_name) from ex
        raise KeyError(field_name)

    def __setitem__(self, field_name: str, value: Any) -> None:
        if field_name in self._fields_dict:
            setattr(self.item, field_name, value)
        else:
            raise KeyError(f"{self.item.__class__.__name__} does not support field: {field_name}")

    def __delitem__(self, field_name: str) -> None:
        if field_name in self._fields_dict:
            try:
                if hasattr(self.item, field_name):
                    delattr(self.item, field_name)
                else:
                    raise AttributeError
            except AttributeError as ex:
                raise KeyError(field_name) from ex
        else:
            raise KeyError(f"{self.item.__class__.__name__} does not support field: {field_name}")

    def __iter__(self) -> Iterator[str]:
        return iter(attr for attr in self._fields_dict if hasattr(self.item, attr))

    def __len__(self) -> int:
        return len(list(iter(self)))


class AttrsAdapter(_MixinAttrsDataclassAdapter, AdapterInterface):
    def __init__(self, item: Any) -> None:
        super().__init__(item)
        if attrs is None:
            raise RuntimeError("attrs module is not available")
        # store a reference to the item's fields to avoid O(n) lookups and O(n^2) traversals
        self._fields_dict = attrs.fields_dict(self.item.__class__)

    @classmethod
    def is_item_class(cls, item_class: type) -> bool:
        return _is_attrs_class(item_class)

    @classmethod
    def get_field_meta_from_class(
        cls, item_class: type, field_name: str
    ) -> MappingProxyType[str, Any]:
        if attrs is None:
            raise RuntimeError("attrs module is not available")
        try:
            return cast(
                "MappingProxyType[str, Any]", attrs.fields_dict(item_class)[field_name].metadata
            )
        except KeyError as ex:
            raise KeyError(f"{item_class.__name__} does not support field: {field_name}") from ex

    @classmethod
    def get_field_names_from_class(cls, item_class: type) -> list[str] | None:
        if attrs is None:
            raise RuntimeError("attrs module is not available")
        return [a.name for a in attrs.fields(item_class)]

    @classmethod
    def get_json_schema(
        cls, item_class: type, *, _state: _JsonSchemaState | None = None
    ) -> dict[str, Any]:
        _state = _state or _JsonSchemaState(adapter=cls, containers={item_class})
        return _json_schema_from_attrs(item_class, _state)


class DataclassAdapter(_MixinAttrsDataclassAdapter, AdapterInterface):
    def __init__(self, item: Any) -> None:
        super().__init__(item)
        # store a reference to the item's fields to avoid O(n) lookups and O(n^2) traversals
        self._fields_dict = {field.name: field for field in dataclasses.fields(self.item)}

    @classmethod
    def is_item_class(cls, item_class: type) -> bool:
        return dataclasses.is_dataclass(item_class)

    @classmethod
    def get_field_meta_from_class(
        cls, item_class: type, field_name: str
    ) -> MappingProxyType[str, Any]:
        for field in dataclasses.fields(item_class):
            if field.name == field_name:
                return field.metadata
        raise KeyError(f"{item_class.__name__} does not support field: {field_name}")

    @classmethod
    def get_field_names_from_class(cls, item_class: type) -> list[str] | None:
        return [a.name for a in dataclasses.fields(item_class)]

    @classmethod
    def get_json_schema(
        cls, item_class: type, *, _state: _JsonSchemaState | None = None
    ) -> dict[str, Any]:
        _state = _state or _JsonSchemaState(adapter=cls, containers={item_class})
        return _json_schema_from_dataclass(item_class, _state)


class PydanticAdapter(AdapterInterface):
    @classmethod
    def is_item_class(cls, item_class: type) -> bool:
        return _is_pydantic_model(item_class)

    @classmethod
    def get_field_meta_from_class(
        cls, item_class: type, field_name: str
    ) -> MappingProxyType[str, Any]:
        try:
            return _get_pydantic_model_metadata(item_class, field_name)
        except KeyError as ex:
            raise KeyError(f"{item_class.__name__} does not support field: {field_name}") from ex

    @classmethod
    def get_field_names_from_class(cls, item_class: type) -> list[str] | None:
        return list(item_class.model_fields.keys())  # type: ignore[attr-defined]

    @classmethod
    def get_json_schema(
        cls, item_class: type, *, _state: _JsonSchemaState | None = None
    ) -> dict[str, Any]:
        return _json_schema_from_pydantic(cls, item_class, _state)

    def field_names(self) -> KeysView[str]:
        return KeysView(self.item.__class__.model_fields)

    def __getitem__(self, field_name: str) -> Any:
        if field_name not in self.item.__class__.model_fields:
            raise KeyError(field_name)
        try:
            return getattr(self.item, field_name)
        except AttributeError as ex:
            raise KeyError(field_name) from ex

    def __setitem__(self, field_name: str, value: Any) -> None:
        if field_name not in self.item.__class__.model_fields:
            raise KeyError(f"{self.item.__class__.__name__} does not support field: {field_name}")
        setattr(self.item, field_name, value)

    def __delitem__(self, field_name: str) -> None:
        if field_name not in self.item.__class__.model_fields:
            raise KeyError(f"{self.item.__class__.__name__} does not support field: {field_name}")
        try:
            if hasattr(self.item, field_name):
                delattr(self.item, field_name)
                return
            raise AttributeError
        except AttributeError as ex:
            raise KeyError(field_name) from ex

    def __iter__(self) -> Iterator[str]:
        return iter(attr for attr in self.item.__class__.model_fields if hasattr(self.item, attr))

    def __len__(self) -> int:
        return len(list(iter(self)))


class _MixinDictScrapyItemAdapter:
    item: Any

    def __getitem__(self, field_name: str) -> Any:
        return self.item[field_name]

    def __setitem__(self, field_name: str, value: Any) -> None:
        self.item[field_name] = value

    def __delitem__(self, field_name: str) -> None:
        del self.item[field_name]

    def __iter__(self) -> Iterator[str]:
        return iter(self.item)

    def __len__(self) -> int:
        return len(self.item)


class DictAdapter(_MixinDictScrapyItemAdapter, AdapterInterface):
    """Adapter for :class:`dict` items.

    :class:`~typing.TypedDict` subclasses are also supported as item classes.
    Their instances are plain dictionaries at run time, so they are handled
    like any other dictionary.
    """

    @classmethod
    def is_item_class(cls, item_class: type) -> bool:
        return issubclass(item_class, dict)

    @classmethod
    def get_field_meta_from_class(
        cls, item_class: type, field_name: str
    ) -> MappingProxyType[str, Any]:
        if _is_typeddict(item_class):
            return _get_typed_dict_field_metadata(item_class, field_name)
        return MappingProxyType({})

    @classmethod
    def get_field_names_from_class(cls, item_class: type) -> list[str] | None:
        if _is_typeddict(item_class):
            return list(item_class.__annotations__)
        return None

    @classmethod
    def get_json_schema(
        cls, item_class: type, *, _state: _JsonSchemaState | None = None
    ) -> dict[str, Any]:
        if _is_typeddict(item_class):
            _state = _state or _JsonSchemaState(adapter=cls, containers={item_class})
            return _json_schema_from_typed_dict(item_class, _state)
        return {"type": "object"}

    def field_names(self) -> KeysView[str]:
        return KeysView(self.item)


class ScrapyItemAdapter(_MixinDictScrapyItemAdapter, AdapterInterface):
    @classmethod
    def is_item_class(cls, item_class: type) -> bool:
        return issubclass(item_class, _scrapy_item_classes)

    @classmethod
    def get_field_meta_from_class(
        cls, item_class: type, field_name: str
    ) -> MappingProxyType[str, Any]:
        return MappingProxyType(item_class.fields[field_name])  # type: ignore[attr-defined]

    @classmethod
    def get_field_names_from_class(cls, item_class: type) -> list[str] | None:
        return list(item_class.fields.keys())  # type: ignore[attr-defined]

    @classmethod
    def get_json_schema(
        cls, item_class: type, *, _state: _JsonSchemaState | None = None
    ) -> dict[str, Any]:
        _state = _state or _JsonSchemaState(adapter=cls, containers={item_class})
        schema = super().get_json_schema(item_class, _state=_state)
        _setdefault_attribute_types_on_json_schema(schema, item_class, _state)
        _setdefault_attribute_docstrings_on_json_schema(schema, item_class)
        schema.pop("required", None)
        return schema

    def field_names(self) -> KeysView[str]:
        return KeysView(self.item.fields)


@lru_cache
def _has_item_overrides(adapter_classes: tuple[type[AdapterInterface], ...]) -> bool:
    """Return True if any of the given adapter classes overrides is_item(), i.e.
    decides on an item basis, and hence cannot be matched to an item by item
    class alone."""
    base_is_item = AdapterInterface.is_item.__func__  # type: ignore[attr-defined]
    overriding = [
        adapter_class
        for adapter_class in adapter_classes
        if getattr(adapter_class.is_item, "__func__", adapter_class.is_item) is not base_is_item
    ]
    if overriding:
        names = ", ".join(adapter_class.__qualname__ for adapter_class in overriding)
        warnings.warn(
            f"The following adapter classes override the is_item() class method: {names}. "
            f"Support for that will be removed, and items will then be matched to an "
            f"adapter class by item class alone. Override is_item_class() instead.",
            DeprecationWarning,
            stacklevel=4,
        )
    return bool(overriding)


class AdapterRegistry:
    """An ordered, immutable collection of adapter classes.

    Registries hold the adapter classes that :class:`ItemAdapter` uses to match
    items to adapters, in order of priority: the first adapter class whose
    :meth:`~AdapterInterface.is_item_class` method handles an item class is the
    one used for it. Each registry keeps its own match cache, so registering
    adapters on one registry never changes the matches cached for another one,
    and the adapter class resolved for a batch of items stays fixed for the
    lifetime of the registry even if other registries are created or changed in
    the meantime.

    Registries are immutable: there is no way to add or remove adapter classes
    after creation. To change the registered adapter classes, create a new
    registry (e.g. a subclass of :class:`ItemAdapter` with a different
    ``ADAPTER_CLASSES`` attribute gets its own registry) and use it for the new
    batch of items.
    """

    __slots__ = ("_adapter_classes", "_cache", "_has_overrides")

    def __init__(self, adapter_classes: Iterable[type[AdapterInterface]]) -> None:
        self._adapter_classes: tuple[type[AdapterInterface], ...] = tuple(adapter_classes)
        self._cache: dict[type, type[AdapterInterface] | None] = {}
        self._has_overrides: bool | None = None

    @property
    def adapter_classes(self) -> tuple[type[AdapterInterface], ...]:
        """The adapter classes registered in this registry, in priority order."""
        return self._adapter_classes

    def has_item_overrides(self) -> bool:
        """Return True if any registered adapter class overrides ``is_item()``.

        Such adapters decide on an item basis and hence disable matching items
        by item class alone.
        """
        if self._has_overrides is None:
            self._has_overrides = _has_item_overrides(self._adapter_classes)
        return self._has_overrides

    def find_adapter_class(self, item_class: type) -> type[AdapterInterface] | None:
        """Return the registered adapter class that handles ``item_class``.

        The first matching adapter class in priority order is returned, or
        ``None`` if none of the registered adapter classes handles the item
        class. The result is cached within this registry only.
        """
        try:
            return self._cache[item_class]
        except KeyError:
            adapter_class = next(
                (
                    candidate_class
                    for candidate_class in self._adapter_classes
                    if candidate_class.is_item_class(item_class)
                ),
                None,
            )
            self._cache[item_class] = adapter_class
            return adapter_class

    def is_item(self, item: object) -> bool:
        """Return True if any of the registered adapter classes handles the item."""
        if self.has_item_overrides():
            return any(adapter_class.is_item(item) for adapter_class in self)
        return self.find_adapter_class(item.__class__) is not None

    def cache_clear(self) -> None:
        """Drop the item-class matches cached by this registry."""
        self._cache.clear()
        self._has_overrides = None

    def __iter__(self) -> Iterator[type[AdapterInterface]]:
        return iter(self._adapter_classes)

    def __len__(self) -> int:
        return len(self._adapter_classes)

    def __repr__(self) -> str:
        names = ", ".join(adapter_class.__qualname__ for adapter_class in self._adapter_classes)
        return f"{self.__class__.__name__}([{names}])"


# For each ItemAdapter class that defines its own ADAPTER_CLASSES attribute,
# the registry built from that attribute, together with the exact object it was
# built from. A weak reference keeps classes that are redefined (e.g. when
# re-running test modules) from retaining their stale registries forever.
_ADAPTER_REGISTRIES: weakref.WeakKeyDictionary[
    type[ItemAdapter], tuple[Iterable[type[AdapterInterface]], AdapterRegistry]
] = weakref.WeakKeyDictionary()


def _get_adapter_registry(adapter_cls: type[ItemAdapter]) -> AdapterRegistry:
    """Return the registry for ``adapter_cls``.

    A class that defines its own ``ADAPTER_CLASSES`` attribute gets its own
    registry, built from that attribute: defining the attribute on a subclass
    fully overrides the registry inherited from its parent, it never extends it.
    A class without its own attribute inherits the registry of the closest
    parent class that defines one.

    The registry is rebuilt if the ``ADAPTER_CLASSES`` attribute is reassigned
    at run time, while registries already in use keep their own, fixed view.
    Each class keeps its own registry and its own match cache, so matches cached
    for one registry can never be reused through another one.
    """
    own_classes = adapter_cls.__dict__.get("ADAPTER_CLASSES")
    if own_classes is not None:
        entry = _ADAPTER_REGISTRIES.get(adapter_cls)
        if entry is None or entry[0] is not own_classes:
            entry = (own_classes, AdapterRegistry(own_classes))
            _ADAPTER_REGISTRIES[adapter_cls] = entry
        return entry[1]
    for base in adapter_cls.__mro__[1:]:
        if base.__dict__.get("ADAPTER_CLASSES") is not None:
            return _get_adapter_registry(base)
    raise RuntimeError(f"No adapter registry found for {adapter_cls!r}")


class ItemAdapter(MutableMapping[str, Any]):
    """Wrapper class to interact with data container objects. It provides a common interface
    to extract and set data without having to take the object's type into account.
    """

    ADAPTER_CLASSES: ClassVar[Iterable[type[AdapterInterface]]] = (
        ScrapyItemAdapter,
        DictAdapter,
        DataclassAdapter,
        AttrsAdapter,
        PydanticAdapter,
    )

    def __init__(self, item: Any, adapter_registry: AdapterRegistry | None = None) -> None:
        registry = (
            adapter_registry
            if adapter_registry is not None
            else _get_adapter_registry(self.__class__)
        )
        adapter_class = registry.find_adapter_class(item.__class__)
        if adapter_class is None or registry.has_item_overrides():
            adapter_class = next(
                (candidate_class for candidate_class in registry if candidate_class.is_item(item)),
                None,
            )
        if adapter_class is None:
            raise TypeError(f"No adapter found for objects of type: {type(item)} ({item})")
        self.adapter = adapter_class(item)
        self.adapter_registry = registry

    @classmethod
    def get_adapter_registry(cls) -> AdapterRegistry:
        """Return the adapter registry used by this class.

        Classes defining their own ``ADAPTER_CLASSES`` attribute have a registry
        of their own, other classes inherit the registry of their closest parent
        with one. Pass the result as the ``adapter_registry`` argument to handle
        a batch of items with a fixed, local set of adapter classes.
        """
        return _get_adapter_registry(cls)

    @classmethod
    def is_item(cls, item: Any) -> bool:
        return _get_adapter_registry(cls).is_item(item)

    @classmethod
    def is_item_class(cls, item_class: type) -> bool:
        return _get_adapter_registry(cls).find_adapter_class(item_class) is not None

    @classmethod
    def _get_adapter_class(cls, item_class: type) -> type[AdapterInterface]:
        adapter_class = _get_adapter_registry(cls).find_adapter_class(item_class)
        if adapter_class is None:
            raise TypeError(f"{item_class} is not a valid item class")
        return adapter_class

    @classmethod
    def get_field_meta_from_class(
        cls, item_class: type, field_name: str
    ) -> MappingProxyType[str, Any]:
        adapter_class = cls._get_adapter_class(item_class)
        return adapter_class.get_field_meta_from_class(item_class, field_name)

    @classmethod
    def get_field_names_from_class(cls, item_class: type) -> list[str] | None:
        adapter_class = cls._get_adapter_class(item_class)
        return adapter_class.get_field_names_from_class(item_class)

    @classmethod
    def get_json_schema(
        cls, item_class: type, *, _state: _JsonSchemaState | None = None
    ) -> dict[str, Any]:
        _state = _state or _JsonSchemaState(adapter=cls, containers={item_class})
        adapter_class = cls._get_adapter_class(item_class)
        return adapter_class.get_json_schema(item_class, _state=_state)

    @property
    def item(self) -> Any:
        return self.adapter.item

    def __repr__(self) -> str:
        values = ", ".join([f"{key}={value!r}" for key, value in self.items()])
        return f"<{self.__class__.__name__} for {self.item.__class__.__name__}({values})>"

    def __getitem__(self, field_name: str) -> Any:
        return self.adapter.__getitem__(field_name)

    def __setitem__(self, field_name: str, value: Any) -> None:
        self.adapter.__setitem__(field_name, value)

    def __delitem__(self, field_name: str) -> None:
        self.adapter.__delitem__(field_name)

    def __iter__(self) -> Iterator[str]:
        return self.adapter.__iter__()

    def __len__(self) -> int:
        return self.adapter.__len__()

    def get_field_meta(self, field_name: str) -> MappingProxyType[str, Any]:
        """Return metadata for the given field name."""
        return self.adapter.get_field_meta(field_name)

    def field_names(self) -> KeysView[str]:
        """Return read-only key view with the names of all the defined fields for the item."""
        return self.adapter.field_names()

    def asdict(self) -> dict[str, Any]:
        """Return a dict object with the contents of the adapter. This works slightly different
        than calling `dict(adapter)`: it's applied recursively to nested items (if there are any).
        """
        return {key: self._asdict(value, self.adapter_registry) for key, value in self.items()}

    @classmethod
    def _asdict(cls, obj: object, adapter_registry: AdapterRegistry | None = None) -> object:
        if isinstance(obj, dict):
            return {key: cls._asdict(value, adapter_registry) for key, value in obj.items()}
        if isinstance(obj, (list, set, tuple)):
            return obj.__class__(cls._asdict(x, adapter_registry) for x in obj)
        if isinstance(obj, cls):
            return obj.asdict()
        if adapter_registry is not None:
            if adapter_registry.is_item(obj):
                return cls(obj, adapter_registry=adapter_registry).asdict()
        elif cls.is_item(obj):
            return cls(obj).asdict()
        return obj
