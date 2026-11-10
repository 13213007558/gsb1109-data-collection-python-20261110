from __future__ import annotations

from collections.abc import Iterator, KeysView
from types import MappingProxyType
from typing import Any

import pytest

from itemadapter.adapter import (
    AdapterInterface,
    AdapterRegistry,
    AttrsAdapter,
    DataclassAdapter,
    DictAdapter,
    ItemAdapter,
    PydanticAdapter,
    ScrapyItemAdapter,
)
from tests import DataClassItem


class SharedRecord:
    """The same object kind for both collection plugins."""

    def __init__(self, **values: Any) -> None:
        self.values = dict(values)


class _BasePluginAdapter(AdapterInterface):
    field_names_: tuple[str, ...] = ()
    metadata_plugin: str = ""

    @classmethod
    def is_item_class(cls, item_class: type) -> bool:
        return issubclass(item_class, SharedRecord)

    @classmethod
    def get_field_names_from_class(cls, item_class: type) -> list[str]:
        return list(cls.field_names_)

    @classmethod
    def get_field_meta_from_class(
        cls, item_class: type, field_name: str
    ) -> MappingProxyType[str, Any]:
        if field_name not in cls.field_names_:
            raise KeyError(field_name)
        return MappingProxyType({"plugin": cls.metadata_plugin, "field": field_name})

    def field_names(self) -> KeysView[str]:
        return KeysView(dict.fromkeys(self.field_names_))

    def __getitem__(self, field_name: str) -> Any:
        if field_name not in self.field_names_:
            raise KeyError(field_name)
        return self.item.values[field_name]

    def __setitem__(self, field_name: str, value: Any) -> None:
        if field_name not in self.field_names_:
            raise KeyError(field_name)
        self.item.values[field_name] = self._store(value)

    def __delitem__(self, field_name: str) -> None:
        if field_name not in self.field_names_:
            raise KeyError(field_name)
        del self.item.values[field_name]

    def __iter__(self) -> Iterator[str]:
        return iter(name for name in self.field_names_ if name in self.item.values)

    def __len__(self) -> int:
        return len(list(iter(self)))

    def _store(self, value: Any) -> Any:
        return value


class PluginAlphaAdapter(_BasePluginAdapter):
    """Plugin A: only the public fields, values are stored untouched."""

    field_names_ = ("id", "name")
    metadata_plugin = "alpha"


class PluginBetaAdapter(_BasePluginAdapter):
    """Plugin B: also exposes the handle field and rewrites string writes."""

    field_names_ = ("id", "handle", "name")
    metadata_plugin = "beta"

    def _store(self, value: Any) -> Any:
        if isinstance(value, str):
            return f"beta:{value}"
        return value


@pytest.fixture
def alpha_registry() -> AdapterRegistry:
    return AdapterRegistry([PluginAlphaAdapter, PluginBetaAdapter, DictAdapter])


@pytest.fixture
def beta_registry() -> AdapterRegistry:
    return AdapterRegistry([PluginBetaAdapter, PluginAlphaAdapter, DictAdapter])


@pytest.fixture
def record() -> SharedRecord:
    return SharedRecord(id=1, name="original", handle="secret")


class TestAdapterRegistry:
    def test_adapter_classes_are_frozen_and_ordered(self) -> None:
        registry = AdapterRegistry([DictAdapter, DataclassAdapter])
        assert registry.adapter_classes == (DictAdapter, DataclassAdapter)
        assert isinstance(registry.adapter_classes, tuple)
        assert list(registry) == [DictAdapter, DataclassAdapter]
        assert len(registry) == 2
        with pytest.raises(AttributeError):
            registry.adapter_classes = (DictAdapter,)  # type: ignore[misc]
        assert "DictAdapter" in repr(registry)

    def test_accepts_any_iterable(self) -> None:
        from collections import deque

        from_iterable = AdapterRegistry(iter([DictAdapter]))
        from_deque = AdapterRegistry(deque([DictAdapter]))
        assert from_iterable.adapter_classes == (DictAdapter,)
        assert from_deque.adapter_classes == (DictAdapter,)

    def test_priority_decides_the_adapter(self, record: SharedRecord) -> None:
        alpha_registry = AdapterRegistry([PluginAlphaAdapter, PluginBetaAdapter])
        beta_registry = AdapterRegistry([PluginBetaAdapter, PluginAlphaAdapter])
        assert alpha_registry.find_adapter_class(SharedRecord) is PluginAlphaAdapter
        assert beta_registry.find_adapter_class(SharedRecord) is PluginBetaAdapter
        assert isinstance(
            ItemAdapter(record, adapter_registry=alpha_registry).adapter, PluginAlphaAdapter
        )
        assert isinstance(
            ItemAdapter(record, adapter_registry=beta_registry).adapter, PluginBetaAdapter
        )

    def test_distinct_field_views_and_metadata(self, record: SharedRecord) -> None:
        alpha = ItemAdapter(record, adapter_registry=AdapterRegistry([PluginAlphaAdapter]))
        beta = ItemAdapter(record, adapter_registry=AdapterRegistry([PluginBetaAdapter]))

        assert list(alpha.field_names()) == ["id", "name"]
        assert list(beta.field_names()) == ["id", "handle", "name"]
        assert PluginAlphaAdapter.get_field_names_from_class(SharedRecord) == ["id", "name"]
        assert PluginBetaAdapter.get_field_names_from_class(SharedRecord) == [
            "id",
            "handle",
            "name",
        ]

        assert alpha.get_field_meta("id") == MappingProxyType({"plugin": "alpha", "field": "id"})
        assert beta.get_field_meta("id") == MappingProxyType({"plugin": "beta", "field": "id"})
        with pytest.raises(KeyError):
            alpha.get_field_meta("handle")
        assert beta.get_field_meta("handle") == MappingProxyType(
            {"plugin": "beta", "field": "handle"}
        )

    def test_distinct_read_and_write_behavior(self, record: SharedRecord) -> None:
        alpha = ItemAdapter(record, adapter_registry=AdapterRegistry([PluginAlphaAdapter]))
        beta = ItemAdapter(record, adapter_registry=AdapterRegistry([PluginBetaAdapter]))

        assert beta["handle"] == "secret"
        with pytest.raises(KeyError):
            alpha["handle"]

        alpha["name"] = "via-alpha"
        assert record.values["name"] == "via-alpha"
        beta["name"] = "via-beta"
        assert record.values["name"] == "beta:via-beta"
        assert alpha["name"] == "beta:via-beta"

        with pytest.raises(KeyError):
            alpha["handle"] = "leak"
        beta["handle"] = "rotated"
        assert record.values["handle"] == "beta:rotated"

        del alpha["name"]
        assert "name" not in record.values
        with pytest.raises(KeyError):
            del alpha["handle"]

    def test_caches_are_not_shared_between_registries(self, record: SharedRecord) -> None:
        alpha_registry = AdapterRegistry([PluginAlphaAdapter, PluginBetaAdapter])
        beta_registry = AdapterRegistry([PluginBetaAdapter, PluginAlphaAdapter])

        # Populate the beta registry first: a shared cache would make the alpha
        # registry wrongly reuse PluginBetaAdapter.
        assert beta_registry.find_adapter_class(SharedRecord) is PluginBetaAdapter
        assert alpha_registry.find_adapter_class(SharedRecord) is PluginAlphaAdapter
        assert alpha_registry._cache is not beta_registry._cache
        assert alpha_registry._cache[SharedRecord] is PluginAlphaAdapter
        assert beta_registry._cache[SharedRecord] is PluginBetaAdapter

        assert isinstance(
            ItemAdapter(record, adapter_registry=alpha_registry).adapter, PluginAlphaAdapter
        )
        assert isinstance(
            ItemAdapter(record, adapter_registry=beta_registry).adapter, PluginBetaAdapter
        )

    def test_cache_clear_is_local(self) -> None:
        alpha_registry = AdapterRegistry([PluginAlphaAdapter])
        beta_registry = AdapterRegistry([PluginBetaAdapter])
        assert alpha_registry.find_adapter_class(SharedRecord) is PluginAlphaAdapter
        assert beta_registry.find_adapter_class(SharedRecord) is PluginBetaAdapter

        alpha_registry.cache_clear()
        assert SharedRecord not in alpha_registry._cache
        assert beta_registry._cache[SharedRecord] is PluginBetaAdapter
        assert alpha_registry.find_adapter_class(SharedRecord) is PluginAlphaAdapter

    def test_batch_keeps_a_fixed_view(self, alpha_registry: AdapterRegistry) -> None:
        records = [SharedRecord(id=index) for index in range(4)]
        adapters = [ItemAdapter(item, adapter_registry=alpha_registry) for item in records]

        # Later registrations, including a higher-priority beta registry, must
        # not swap the adapter used by the batch being processed.
        late_beta_registry = AdapterRegistry([PluginBetaAdapter])
        late_global_registry = AdapterRegistry(
            [PluginBetaAdapter, PluginAlphaAdapter, DictAdapter]
        )
        assert late_beta_registry is not alpha_registry
        assert late_global_registry is not alpha_registry

        for adapter in adapters:
            assert adapter.adapter_registry is alpha_registry
            assert isinstance(adapter.adapter, PluginAlphaAdapter)
            assert list(adapter.field_names()) == ["id", "name"]
            adapter["name"] = "fixed"
            assert adapter.item.values["name"] == "fixed"

    def test_global_reregistration_does_not_affect_local_registry(
        self, record: SharedRecord, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local_registry = AdapterRegistry([PluginAlphaAdapter])
        adapter = ItemAdapter(record, adapter_registry=local_registry)
        original_classes = ItemAdapter.ADAPTER_CLASSES
        monkeypatch.setattr(ItemAdapter, "ADAPTER_CLASSES", (DictAdapter,))

        assert adapter.adapter_registry is local_registry
        assert isinstance(adapter.adapter, PluginAlphaAdapter)
        assert not ItemAdapter.is_item(record)
        assert local_registry.is_item(record)
        assert isinstance(
            ItemAdapter(record, adapter_registry=local_registry).adapter, PluginAlphaAdapter
        )

        monkeypatch.setattr(ItemAdapter, "ADAPTER_CLASSES", original_classes)

    def test_interleaved_batches(self) -> None:
        alpha_registry = AdapterRegistry([PluginAlphaAdapter, DictAdapter])
        beta_registry = AdapterRegistry([PluginBetaAdapter, DictAdapter])
        alpha_records = [SharedRecord(id=index) for index in range(3)]
        beta_records = [SharedRecord(id=index) for index in range(3)]

        alpha_adapters: list[ItemAdapter] = []
        beta_adapters: list[ItemAdapter] = []
        for alpha_record, beta_record in zip(alpha_records, beta_records, strict=True):
            alpha_adapter = ItemAdapter(alpha_record, adapter_registry=alpha_registry)
            beta_adapter = ItemAdapter(beta_record, adapter_registry=beta_registry)
            alpha_adapter["name"] = f"a{alpha_record.values['id']}"
            beta_adapter["name"] = f"b{beta_record.values['id']}"
            alpha_adapters.append(alpha_adapter)
            beta_adapters.append(beta_adapter)

        for index, adapter in enumerate(alpha_adapters):
            assert adapter.adapter_registry is alpha_registry
            assert isinstance(adapter.adapter, PluginAlphaAdapter)
            assert adapter["name"] == f"a{index}"
            assert list(adapter.field_names()) == ["id", "name"]
        for index, adapter in enumerate(beta_adapters):
            assert adapter.adapter_registry is beta_registry
            assert isinstance(adapter.adapter, PluginBetaAdapter)
            assert adapter["name"] == f"beta:b{index}"
            assert list(adapter.field_names()) == ["id", "handle", "name"]

    def test_unsupported_item_raises(self) -> None:
        registry = AdapterRegistry([DictAdapter])
        with pytest.raises(TypeError, match="No adapter found"):
            ItemAdapter(object(), adapter_registry=registry)
        assert registry.find_adapter_class(object) is None
        assert not registry.is_item(object())

    def test_empty_registry_does_not_fall_back_to_the_class_registry(self) -> None:
        empty_registry = AdapterRegistry(())
        assert len(empty_registry) == 0
        with pytest.raises(TypeError, match="No adapter found"):
            ItemAdapter({"foo": "bar"}, adapter_registry=empty_registry)
        assert not empty_registry.is_item({"foo": "bar"})

    def test_is_item_override_warns(self) -> None:
        class EmptyDictAdapter(DictAdapter):
            @classmethod
            def is_item(cls, item: Any) -> bool:
                return isinstance(item, dict) and not item

        registry = AdapterRegistry([EmptyDictAdapter])
        with pytest.warns(DeprecationWarning, match="EmptyDictAdapter"):
            assert registry.is_item({})
        assert not registry.is_item({"foo": "bar"})
        assert isinstance(ItemAdapter({}, adapter_registry=registry).adapter, EmptyDictAdapter)
        with pytest.raises(TypeError, match="No adapter found"):
            ItemAdapter({"foo": "bar"}, adapter_registry=registry)

    def test_asdict_uses_the_injected_registry(self) -> None:
        registry = AdapterRegistry([PluginAlphaAdapter, DictAdapter])
        nested = SharedRecord(id=7, name="nested", handle="hidden")
        container = {"record": nested, "plain": [1, 2]}
        adapter = ItemAdapter(container, adapter_registry=registry)
        assert adapter.asdict() == {
            "record": {"id": 7, "name": "nested"},
            "plain": [1, 2],
        }


class TestItemAdapterRegistryInheritance:
    def test_default_global_registry(self) -> None:
        registry = ItemAdapter.get_adapter_registry()
        assert registry.adapter_classes == (
            ScrapyItemAdapter,
            DictAdapter,
            DataclassAdapter,
            AttrsAdapter,
            PydanticAdapter,
        )
        assert ItemAdapter.is_item({"foo": "bar"})
        assert ItemAdapter.is_item(DataClassItem())
        assert not ItemAdapter.is_item(object())
        assert isinstance(ItemAdapter({"foo": "bar"}).adapter, DictAdapter)
        assert isinstance(ItemAdapter(DataClassItem()).adapter, DataclassAdapter)

    def test_subclass_inherits_the_registry(self) -> None:
        class InheritingItemAdapter(ItemAdapter):
            pass

        class GrandchildItemAdapter(InheritingItemAdapter):
            pass

        assert InheritingItemAdapter.get_adapter_registry() is ItemAdapter.get_adapter_registry()
        assert GrandchildItemAdapter.get_adapter_registry() is ItemAdapter.get_adapter_registry()

    def test_subclass_override_replaces_instead_of_extending(self) -> None:
        class DictOnlyItemAdapter(ItemAdapter):
            ADAPTER_CLASSES = [DictAdapter]

        registry = DictOnlyItemAdapter.get_adapter_registry()
        assert registry is not ItemAdapter.get_adapter_registry()
        assert registry.adapter_classes == (DictAdapter,)
        assert DictOnlyItemAdapter.is_item({"foo": "bar"})
        assert not DictOnlyItemAdapter.is_item(DataClassItem())
        with pytest.raises(TypeError, match="No adapter found"):
            DictOnlyItemAdapter(DataClassItem())

    def test_override_is_independent_from_sibling_and_parent(self) -> None:
        class AlphaItemAdapter(ItemAdapter):
            ADAPTER_CLASSES = [PluginAlphaAdapter, DictAdapter]

        class BetaItemAdapter(ItemAdapter):
            ADAPTER_CLASSES = [PluginBetaAdapter, DictAdapter]

        record = SharedRecord(id=1, handle="t", name="n")
        assert isinstance(AlphaItemAdapter(record).adapter, PluginAlphaAdapter)
        assert isinstance(BetaItemAdapter(record).adapter, PluginBetaAdapter)
        assert (
            AlphaItemAdapter.get_adapter_registry() is not BetaItemAdapter.get_adapter_registry()
        )
        assert isinstance(ItemAdapter({"foo": "bar"}).adapter, DictAdapter)
        assert not ItemAdapter.is_item(record)

    def test_class_level_helpers_use_the_class_registry(self) -> None:
        class AlphaItemAdapter(ItemAdapter):
            ADAPTER_CLASSES = [PluginAlphaAdapter]

        assert AlphaItemAdapter.get_field_names_from_class(SharedRecord) == ["id", "name"]
        assert AlphaItemAdapter.get_field_meta_from_class(SharedRecord, "id") == MappingProxyType(
            {"plugin": "alpha", "field": "id"}
        )
        assert AlphaItemAdapter.is_item_class(SharedRecord)
        assert not AlphaItemAdapter.is_item_class(dict)

    def test_reassigning_adapter_classes_rebuilds_the_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_registry = ItemAdapter.get_adapter_registry()
        record = DataClassItem()
        existing_adapter = ItemAdapter(record)

        monkeypatch.setattr(ItemAdapter, "ADAPTER_CLASSES", (DictAdapter,))
        rebuilt_registry = ItemAdapter.get_adapter_registry()
        assert rebuilt_registry is not original_registry
        assert rebuilt_registry.adapter_classes == (DictAdapter,)
        assert not ItemAdapter.is_item(record)

        # The batch started before the reassignment keeps its fixed registry.
        assert existing_adapter.adapter_registry is original_registry
        assert isinstance(existing_adapter.adapter, DataclassAdapter)

        monkeypatch.setattr(ItemAdapter, "ADAPTER_CLASSES", original_registry.adapter_classes)
        restored_registry = ItemAdapter.get_adapter_registry()
        assert restored_registry.adapter_classes == original_registry.adapter_classes
        assert ItemAdapter.is_item(record)
        assert isinstance(ItemAdapter(record).adapter, DataclassAdapter)

    def test_inheriting_subclass_follows_parent_reassignment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class InheritingItemAdapter(ItemAdapter):
            pass

        assert InheritingItemAdapter.get_adapter_registry() is ItemAdapter.get_adapter_registry()
        monkeypatch.setattr(ItemAdapter, "ADAPTER_CLASSES", (DictAdapter,))
        assert InheritingItemAdapter.get_adapter_registry() is ItemAdapter.get_adapter_registry()
        assert InheritingItemAdapter.get_adapter_registry().adapter_classes == (DictAdapter,)
