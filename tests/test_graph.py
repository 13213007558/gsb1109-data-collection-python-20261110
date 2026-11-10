from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from itemadapter import GRAPH_FORMAT, GraphExportError, ItemAdapter, dump_graph, load_graph
from tests import requires_attrs, requires_pydantic


@dataclass
class Address:
    city: str


@dataclass
class Product:
    name: str
    price: int
    shop: object = None


@dataclass
class Shop:
    name: str
    address: object
    products: list = field(default_factory=list)


REGISTRY = [Address, Product, Shop]


def _build_shop() -> tuple[Shop, Address, Address]:
    shared_address = Address("Berlin")
    shop = Shop("megastore", shared_address)
    first = Product("phone", 999, shop)
    second = Product("case", 9, shop)
    shop.products.extend([first, second])
    equal_address = Address("Berlin")
    return shop, shared_address, equal_address


def _assert_product_shop_ring(restored: object) -> None:
    shop_adapter = ItemAdapter(restored)
    products = shop_adapter["products"]
    assert isinstance(products, list)
    first_adapter = ItemAdapter(products[0])
    second_adapter = ItemAdapter(products[1])
    assert first_adapter["name"] == "phone"
    assert first_adapter["price"] == 999
    assert second_adapter["name"] == "case"
    assert second_adapter["price"] == 9
    assert first_adapter["shop"] is restored
    assert second_adapter["shop"] is restored
    assert first_adapter["shop"] is second_adapter["shop"]
    assert shop_adapter["address"] is ItemAdapter(first_adapter["shop"])["address"]


def test_document_is_plain_data_with_stable_identifiers() -> None:
    shop, _, _ = _build_shop()
    document = dump_graph(shop)
    assert document["format"] == GRAPH_FORMAT
    assert set(document) == {"format", "root", "nodes"}
    json.dumps(document)
    node_ids = list(document["nodes"])
    assert node_ids == [f"node-{index}" for index in range(1, len(node_ids) + 1)]
    assert all("0x" not in node_id for node_id in node_ids)
    assert list(dump_graph(shop)["nodes"]) == node_ids


def test_dataclass_roundtrip_preserves_values_and_identity() -> None:
    shop, _, _ = _build_shop()
    restored = load_graph(dump_graph(shop), REGISTRY)
    assert type(restored) is Shop
    shop_adapter = ItemAdapter(restored)
    assert shop_adapter["name"] == "megastore"
    assert ItemAdapter(shop_adapter["address"])["city"] == "Berlin"
    _assert_product_shop_ring(restored)


def test_shared_node_vs_equal_nodes_are_distinguished() -> None:
    shop, shared_address, equal_address = _build_shop()
    document = dump_graph(shop)
    address_nodes = [
        node_id
        for node_id, node in document["nodes"].items()
        if node.get("type") == "Address"
    ]
    assert len(address_nodes) == 1
    combined = dump_graph([shared_address, equal_address])
    restored = load_graph(combined, REGISTRY)
    assert restored[0] is not restored[1]
    assert restored[0] == restored[1]
    assert len([node for node in combined["nodes"].values() if node.get("type") == "Address"]) == 2


def test_shared_reference_points_at_single_node() -> None:
    shop, _, _ = _build_shop()
    document = dump_graph(shop)
    root_id = document["root"]["ref"]
    product_nodes = [
        node for node in document["nodes"].values() if node.get("type") == "Product"
    ]
    assert all(node["fields"]["shop"] == {"ref": root_id} for node in product_nodes)
    restored = load_graph(document, REGISTRY)
    assert ItemAdapter(restored)["address"] is ItemAdapter(restored)["address"]


def test_two_records_sharing_one_address_object() -> None:
    @dataclass
    class Item:
        name: str
        address: object

    shared_address = Address("Berlin")
    shipment = {
        "first": Item("phone", shared_address),
        "second": Item("case", shared_address),
    }
    document = dump_graph(shipment)
    item_nodes = [node for node in document["nodes"].values() if node.get("type") == "Item"]
    assert len(item_nodes) == 2
    assert item_nodes[0]["fields"]["address"] == item_nodes[1]["fields"]["address"]
    assert len([node for node in document["nodes"].values() if node.get("type") == "Address"]) == 1
    restored = load_graph(document, [Address, Item])
    assert restored["first"].address is restored["second"].address
    assert ItemAdapter(restored["first"].address)["city"] == "Berlin"


def test_container_types_and_shared_container_identity() -> None:
    shared_list: list[Any] = []
    shared_dict: dict[str, Any] = {}

    @dataclass
    class ContainerHolder:
        a_list: list
        a_tuple: tuple
        a_set: set
        a_frozenset: frozenset
        a_dict: dict

    holder = ContainerHolder(
        a_list=shared_list,
        a_tuple=("x", shared_list),
        a_set={"s"},
        a_frozenset=frozenset({"f"}),
        a_dict=shared_dict,
    )
    shared_list.append(holder)
    shared_dict["back"] = holder
    document = dump_graph(holder)
    restored = load_graph(document, [ContainerHolder])
    assert type(restored) is ContainerHolder
    assert isinstance(restored.a_list, list)
    assert restored.a_list is restored.a_tuple[1]
    assert isinstance(restored.a_tuple, tuple)
    assert restored.a_tuple[0] == "x"
    assert isinstance(restored.a_set, set)
    assert restored.a_set == {"s"}
    assert isinstance(restored.a_frozenset, frozenset)
    assert restored.a_frozenset == frozenset({"f"})
    assert isinstance(restored.a_dict, dict)
    assert restored.a_dict["back"] is restored
    assert restored.a_list[0] is restored


def test_item_adapter_wrapper_is_transparent() -> None:
    shop, _, _ = _build_shop()
    document = dump_graph(ItemAdapter(shop))
    restored = load_graph(
        document,
        {"Address": Address, "Product": Product, "Shop": Shop},
    )
    assert type(restored) is Shop
    _assert_product_shop_ring(restored)


def test_survives_json_roundtrip() -> None:
    shop, _, _ = _build_shop()
    reloaded = json.loads(json.dumps(dump_graph(shop)))
    restored = load_graph(reloaded, REGISTRY)
    _assert_product_shop_ring(restored)


def test_scalar_roots() -> None:
    assert load_graph(dump_graph(7), []) == 7
    assert load_graph(dump_graph("text"), []) == "text"
    assert load_graph(dump_graph(None), []) is None


@requires_attrs
def test_attrs_roundtrip_preserves_values_and_identity() -> None:
    import attrs

    @attrs.define
    class Address:
        city: str

    @attrs.define
    class Product:
        name: str
        price: int
        shop: object | None = None

    @attrs.define
    class Shop:
        name: str
        address: object
        products: list = attrs.field(factory=list)

    address = Address("Berlin")
    shop = Shop("megastore", address)
    shop.products.append(Product("phone", 999, shop))
    shop.products.append(Product("case", 9, shop))
    restored = load_graph(dump_graph(shop), [Address, Product, Shop])
    assert type(restored) is Shop
    _assert_product_shop_ring(restored)


@requires_pydantic
def test_pydantic_roundtrip_preserves_values_and_identity() -> None:
    from pydantic import BaseModel

    class Address(BaseModel):
        city: str

    class Product(BaseModel):
        name: str
        price: int
        shop: object | None = None

    class Shop(BaseModel):
        name: str
        address: object
        products: list = []

    address = Address(city="Berlin")
    shop = Shop(name="megastore", address=address, products=[])
    shop.products.append(Product(name="phone", price=999, shop=shop))
    shop.products.append(Product(name="case", price=9, shop=shop))
    restored = load_graph(dump_graph(shop), [Address, Product, Shop])
    assert type(restored) is Shop
    _assert_product_shop_ring(restored)


def test_unregistered_record_type_is_rejected() -> None:
    shop, _, _ = _build_shop()
    document = dump_graph(shop)
    with pytest.raises(GraphExportError, match="unregistered record type"):
        load_graph(document, [Address, Product])


def test_unknown_field_of_registered_type_is_rejected() -> None:
    shop, _, _ = _build_shop()
    document = json.loads(json.dumps(dump_graph(shop)))
    address_id = next(
        node_id
        for node_id, node in document["nodes"].items()
        if node.get("type") == "Address"
    )
    document["nodes"][address_id]["fields"]["unexpected"] = "value"
    with pytest.raises(GraphExportError, match="does not support field"):
        load_graph(document, REGISTRY)


def test_duplicate_registry_name_is_rejected() -> None:
    shop, _, _ = _build_shop()
    with pytest.raises(GraphExportError, match="duplicate record type name"):
        load_graph(dump_graph(shop), [Address, Product, Shop, Shop])


def test_dangling_references_are_rejected() -> None:
    shop, _, _ = _build_shop()
    document = dump_graph(shop)
    bad_root = dict(document)
    bad_root["root"] = {"ref": "node-999"}
    with pytest.raises(GraphExportError, match="bad reference"):
        load_graph(bad_root, REGISTRY)
    bad_field = json.loads(json.dumps(document))
    product_id = next(
        node_id
        for node_id, node in bad_field["nodes"].items()
        if node.get("type") == "Product"
    )
    bad_field["nodes"][product_id]["fields"]["shop"] = {"ref": "missing"}
    with pytest.raises(GraphExportError, match="bad reference"):
        load_graph(bad_field, REGISTRY)


def test_inline_composites_are_rejected() -> None:
    shop, _, _ = _build_shop()
    document = json.loads(json.dumps(dump_graph(shop)))
    list_id = next(
        node_id
        for node_id, node in document["nodes"].items()
        if node.get("container") == "list"
    )
    document["nodes"][list_id]["value"] = [{"not": "a ref"}]
    with pytest.raises(GraphExportError, match="referenced nodes"):
        load_graph(document, REGISTRY)


def test_immutable_container_cycle_is_rejected() -> None:
    document = {
        "format": GRAPH_FORMAT,
        "root": {"ref": "node-1"},
        "nodes": {"node-1": {"container": "tuple", "value": [{"ref": "node-1"}]}},
    }
    with pytest.raises(GraphExportError, match="cycle through an immutable container"):
        load_graph(document, [])


@pytest.mark.parametrize(
    "document",
    [
        [],
        "not a document",
        42,
        {},
        {"format": "itemadapter-graph/2", "root": {"ref": "node-1"}, "nodes": {}},
        {"format": GRAPH_FORMAT, "root": {"ref": "node-1"}, "nodes": []},
        {
            "format": GRAPH_FORMAT,
            "root": {"ref": "node-1"},
            "nodes": {"node-1": {"container": "deque", "value": []}},
        },
        {
            "format": GRAPH_FORMAT,
            "root": {"ref": "node-1"},
            "nodes": {"node-1": {"unexpected": "shape"}},
        },
        {
            "format": GRAPH_FORMAT,
            "root": {"ref": 1},
            "nodes": {},
        },
    ],
)
def test_malformed_documents_are_rejected(document: object) -> None:
    with pytest.raises(GraphExportError):
        load_graph(document, REGISTRY)


def test_graph_export_error_is_value_error() -> None:
    assert issubclass(GraphExportError, ValueError)


def test_asdict_keeps_tree_expansion_contract() -> None:
    @dataclass
    class Price:
        value: int
        currency: str

    @dataclass
    class Item:
        name: str
        price: Price

    adapter = ItemAdapter(Item("stuff", Price(42, "UYU")))
    assert adapter.asdict() == {
        "name": "stuff",
        "price": {"value": 42, "currency": "UYU"},
    }

    shared_price = Price(42, "UYU")
    tree = ItemAdapter({"first": Item("a", shared_price), "second": Item("b", shared_price)})
    expanded = tree.asdict()
    assert expanded["first"]["price"] == expanded["second"]["price"]
    assert expanded["first"]["price"] is not expanded["second"]["price"]

    graph = dump_graph({"first": Item("a", shared_price), "second": Item("b", shared_price)})
    price_refs = [
        node["fields"]["price"]
        for node in graph["nodes"].values()
        if node.get("type") == "Item"
    ]
    assert price_refs[0] == price_refs[1]
