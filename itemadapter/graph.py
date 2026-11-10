from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, cast

from itemadapter.adapter import ItemAdapter

__all__ = ["GRAPH_FORMAT", "GraphExportError", "dump_graph", "load_graph"]

GRAPH_FORMAT = "itemadapter-graph/1"

_FORMAT = "format"
_ROOT = "root"
_NODES = "nodes"
_TYPE = "type"
_FIELDS = "fields"
_CONTAINER = "container"
_VALUE = "value"
_REF = "ref"

_ITEM_NODE_KEYS = frozenset({_TYPE, _FIELDS})
_CONTAINER_NODE_KEYS = frozenset({_CONTAINER, _VALUE})
_REF_KEYS = frozenset({_REF})
_DICT_KIND = "dict"
_SEQUENCE_KINDS = frozenset({"list", "tuple", "set", "frozenset"})
_CONTAINER_KINDS = frozenset({_DICT_KIND, *_SEQUENCE_KINDS})


class GraphExportError(ValueError):
    """Raised when a graph document cannot be exported or restored."""


def _is_ref(value: object) -> bool:
    return isinstance(value, dict) and frozenset(value.keys()) == _REF_KEYS


def _build_registry(
    registry: Mapping[str, type] | Iterable[type],
) -> dict[str, type]:
    if isinstance(registry, Mapping):
        pairs = list(registry.items())
    else:
        pairs = [(record_class.__name__, record_class) for record_class in registry]
    resolved: dict[str, type] = {}
    for name, record_class in pairs:
        if not isinstance(name, str) or not isinstance(record_class, type):
            raise TypeError("registry must contain type classes or str-to-type mappings")
        if name in resolved:
            raise GraphExportError(f"duplicate record type name in registry: {name!r}")
        resolved[name] = record_class
    return resolved


class _GraphDumper:
    """Serializes an object graph into referenced nodes.

    Each item or container is assigned one stable identifier per export, based
    on traversal order rather than on memory addresses. Shared objects and
    cycles are represented by repeated references to the same node, while
    equal-but-distinct objects get separate nodes.
    """

    def __init__(self) -> None:
        self._ids: dict[int, str] = {}
        self._nodes: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def dump(self, root: object) -> dict[str, Any]:
        return {
            _FORMAT: GRAPH_FORMAT,
            _ROOT: self._encode(root),
            _NODES: self._nodes,
        }

    def _register(self, obj: object) -> str:
        self._counter += 1
        node_id = f"node-{self._counter}"
        self._ids[id(obj)] = node_id
        return node_id

    def _encode(self, obj: object) -> Any:
        if isinstance(obj, ItemAdapter):
            return self._encode(obj.item)
        if ItemAdapter.is_item(obj) and type(obj) is not dict:
            return self._encode_item(obj)
        if isinstance(obj, dict):
            return self._encode_container(obj, _DICT_KIND, self._encode_dict_body)
        if isinstance(obj, list):
            return self._encode_container(obj, "list", self._encode_sequence_body)
        if isinstance(obj, tuple):
            return self._encode_container(obj, "tuple", self._encode_sequence_body)
        if isinstance(obj, set):
            return self._encode_container(obj, "set", self._encode_sequence_body)
        if isinstance(obj, frozenset):
            return self._encode_container(obj, "frozenset", self._encode_sequence_body)
        return obj

    def _encode_item(self, obj: object) -> dict[str, str]:
        cached = self._ids.get(id(obj))
        if cached is not None:
            return {_REF: cached}
        node_id = self._register(obj)
        node: dict[str, Any] = {_TYPE: obj.__class__.__name__, _FIELDS: {}}
        self._nodes[node_id] = node
        adapter = ItemAdapter(obj)
        node[_FIELDS] = {
            field_name: self._encode(field_value)
            for field_name, field_value in adapter.items()
        }
        return {_REF: node_id}

    def _encode_container(
        self,
        obj: object,
        kind: str,
        body_builder: Any,
    ) -> dict[str, str]:
        cached = self._ids.get(id(obj))
        if cached is not None:
            return {_REF: cached}
        node_id = self._register(obj)
        node: dict[str, Any] = {_CONTAINER: kind, _VALUE: None}
        self._nodes[node_id] = node
        node[_VALUE] = body_builder(obj)
        return {_REF: node_id}

    def _encode_dict_body(self, obj: dict[Any, Any]) -> dict[Any, Any]:
        return {key: self._encode(value) for key, value in obj.items()}

    def _encode_sequence_body(self, obj: Iterable[object]) -> list[Any]:
        return [self._encode(element) for element in obj]


class _GraphLoader:
    def __init__(self, registry: Mapping[str, type] | Iterable[type]) -> None:
        self._registry = _build_registry(registry)
        self._nodes: dict[str, dict[str, Any]] = {}
        self._objects: dict[str, object] = {}
        self._building_immutable: set[str] = set()

    def load(self, document: Any) -> object:
        self._validate_document(document)
        return self._decode_value(document[_ROOT])

    def _validate_document(self, document: Any) -> None:
        if not isinstance(document, dict):
            raise GraphExportError("graph document must be a dict")
        if frozenset(document.keys()) != frozenset({_FORMAT, _ROOT, _NODES}):
            raise GraphExportError("graph document has invalid top-level keys")
        if document[_FORMAT] != GRAPH_FORMAT:
            raise GraphExportError(
                f"unsupported graph format: {document[_FORMAT]!r}; expected {GRAPH_FORMAT!r}"
            )
        nodes = document[_NODES]
        if not isinstance(nodes, dict):
            raise GraphExportError("graph document nodes must be a dict")
        self._nodes = nodes
        for node_id, node in nodes.items():
            self._validate_node(node_id, node)
        root = document[_ROOT]
        if _is_ref(root):
            self._require_known_ref(root)
        elif isinstance(root, (dict, list, tuple, set, frozenset)):
            raise GraphExportError("graph root composites must reference a node")

    def _validate_node(self, node_id: object, node: object) -> None:
        if not isinstance(node_id, str):
            raise GraphExportError("graph node identifiers must be strings")
        if not isinstance(node, dict):
            raise GraphExportError(f"graph node {node_id!r} must be a dict")
        keys = frozenset(node.keys())
        if keys == _ITEM_NODE_KEYS:
            if not isinstance(node[_TYPE], str):
                raise GraphExportError(f"graph node {node_id!r} has an invalid type name")
            fields = node[_FIELDS]
            if not isinstance(fields, dict) or not all(
                isinstance(field_name, str) for field_name in fields
            ):
                raise GraphExportError(f"graph node {node_id!r} has invalid fields")
            return
        if keys == _CONTAINER_NODE_KEYS:
            kind = node[_CONTAINER]
            if kind not in _CONTAINER_KINDS:
                raise GraphExportError(f"graph node {node_id!r} has unknown container {kind!r}")
            body = node[_VALUE]
            if kind == _DICT_KIND:
                if not isinstance(body, dict):
                    raise GraphExportError(f"graph node {node_id!r} has an invalid dict body")
            elif not isinstance(body, list):
                raise GraphExportError(f"graph node {node_id!r} has an invalid sequence body")
            return
        raise GraphExportError(f"graph node {node_id!r} has invalid keys")

    def _require_known_ref(self, ref: dict[str, Any]) -> None:
        node_id = ref[_REF]
        if not isinstance(node_id, str) or node_id not in self._nodes:
            raise GraphExportError(f"graph document contains a bad reference: {ref!r}")

    def _decode_value(self, value: Any) -> Any:
        if _is_ref(value):
            self._require_known_ref(value)
            return self._load_node(value[_REF])
        if isinstance(value, (dict, list, tuple, set, frozenset)):
            raise GraphExportError("graph composites must be encoded as referenced nodes")
        return value

    def _load_node(self, node_id: str) -> object:
        cached = self._objects.get(node_id)
        if cached is not None:
            return cached
        node = self._nodes[node_id]
        if _TYPE in node:
            item = self._create_item_stub(node[_TYPE])
            self._objects[node_id] = item
            self._fill_item(item, node[_FIELDS])
            return item
        kind = node[_CONTAINER]
        if kind == _DICT_KIND:
            mapping: dict[Any, Any] = {}
            self._objects[node_id] = mapping
            for key, value in node[_VALUE].items():
                try:
                    mapping[key] = self._decode_value(value)
                except TypeError as ex:
                    raise GraphExportError(
                        f"graph node {node_id!r} has an unhashable dict key"
                    ) from ex
            return mapping
        if kind == "list":
            sequence: list[Any] = []
            self._objects[node_id] = sequence
            sequence.extend(self._decode_value(element) for element in node[_VALUE])
            return sequence
        if kind == "set":
            unordered: set[Any] = set()
            self._objects[node_id] = unordered
            for element in node[_VALUE]:
                try:
                    unordered.add(self._decode_value(element))
                except TypeError as ex:
                    raise GraphExportError(
                        f"graph node {node_id!r} contains an unhashable set element"
                    ) from ex
            return unordered
        return self._load_immutable(node_id, kind)

    def _load_immutable(self, node_id: str, kind: str) -> object:
        if node_id in self._building_immutable:
            raise GraphExportError(
                f"graph document contains a cycle through an immutable container: {node_id!r}"
            )
        self._building_immutable.add(node_id)
        try:
            elements = [
                self._decode_value(element) for element in self._nodes[node_id][_VALUE]
            ]
        finally:
            self._building_immutable.discard(node_id)
        try:
            if kind == "tuple":
                built: object = tuple(elements)
            else:
                built = frozenset(elements)
        except TypeError as ex:
            raise GraphExportError(
                f"graph node {node_id!r} contains an unhashable {kind} element"
            ) from ex
        self._objects[node_id] = built
        return built

    def _create_item_stub(self, type_name: str) -> object:
        record_class = self._registry.get(type_name)
        if record_class is None:
            raise GraphExportError(f"unregistered record type: {type_name!r}")
        model_construct = getattr(record_class, "model_construct", None)
        if callable(model_construct):
            return model_construct()
        return cast("Any", record_class).__new__(record_class)

    def _fill_item(self, obj: object, fields: dict[str, Any]) -> None:
        adapter = ItemAdapter(obj)
        for field_name, encoded_value in fields.items():
            try:
                adapter[field_name] = self._decode_value(encoded_value)
            except KeyError as ex:
                raise GraphExportError(
                    f"{obj.__class__.__name__} does not support field: {field_name}"
                ) from ex


def dump_graph(obj: object) -> dict[str, Any]:
    """Serialize an object graph into a plain-data document.

    Unlike :meth:`~itemadapter.adapter.ItemAdapter.asdict`, shared objects and
    reference cycles are preserved: every item and container becomes a node
    with a stable, export-local identifier, and references point at those
    nodes. Equal objects that are not the same instance get separate nodes.
    """
    return _GraphDumper().dump(obj)


def load_graph(
    document: Any,
    registry: Mapping[str, type] | Iterable[type],
) -> object:
    """Restore an object graph from a :func:`dump_graph` document.

    ``registry`` is either an iterable of allowed record classes or a mapping
    of record type names to classes. Record types absent from the registry,
    and documents containing unknown formats or dangling references, are
    rejected with :class:`GraphExportError`.
    """
    return _GraphLoader(registry).load(document)
