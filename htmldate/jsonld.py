"""
Structured JSON-LD parsing for targeted date extraction.

A single JSON-LD block (or several of them on the same page) can describe
a whole graph: the web site, the organization behind it, the main article
and any number of related or recommended articles. This module builds an
entity graph from such descriptions so that a date is attributed to the
one entity the caller is interested in — selected by page URL or entity
identifier — instead of whatever happens to come first in document order.
"""

import json
import logging

from collections import deque
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)

# date properties according to the requested extraction mode
ORIGINAL_DATE_PROPERTIES = ("datePublished",)
MODIFIED_DATE_PROPERTIES = ("dateModified",)

# relations whose entities may legitimately share the target's dates,
# e.g. a web page and the article it mainly consists of
ALLOWED_RELATIONS = ("mainEntity", "mainEntityOfPage")

# entity types which carry an article's dates rather than site metadata
ARTICLE_TYPES = frozenset(
    {
        "AdvertiserContentArticle",
        "AnalysisNewsArticle",
        "Article",
        "AskPublicNewsArticle",
        "BackgroundNewsArticle",
        "BlogPosting",
        "LiveBlogPosting",
        "NewsArticle",
        "OpinionNewsArticle",
        "Report",
        "ReportageNewsArticle",
        "ReviewNewsArticle",
        "SatiricalArticle",
        "ScholarlyArticle",
        "SocialMediaPosting",
        "TechArticle",
    }
)


def _as_list(value: Any) -> list[Any]:
    "Normalize a JSON-LD value to a list."
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _iter_top_nodes(data: Any) -> Iterator[dict]:
    "Yield entity descriptions from parsed JSON-LD, unfolding @graph containers."
    if isinstance(data, list):
        for item in data:
            yield from _iter_top_nodes(item)
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_top_nodes(item)
        # a pure @context/@graph container is not an entity of its own
        if any(key not in ("@context", "@graph") for key in data):
            yield data


def _normalize_url(url: str) -> str:
    "Make URLs comparable: drop scheme and fragment, lowercase the host."
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.netloc:
        return url
    path = parts.path.rstrip("/") or "/"
    normalized = f"{parts.netloc.lower()}{path}"
    if parts.query:
        normalized += f"?{parts.query}"
    return normalized


def _node_urls(node: dict) -> Iterator[str]:
    "Yield the URLs identifying a node: @id, url and mainEntityOfPage."
    for key in ("@id", "url", "mainEntityOfPage"):
        for value in _as_list(node.get(key)):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                ref = value.get("@id") or value.get("url")
                if isinstance(ref, str):
                    yield ref


def _is_article(node: dict) -> bool:
    "Check whether the node is of an article-like type."
    types = _as_list(node.get("@type"))
    return not ARTICLE_TYPES.isdisjoint(t for t in types if isinstance(t, str))


def _date_properties(original: bool) -> tuple[str, ...]:
    "Date properties to look for according to the extraction mode."
    return ORIGINAL_DATE_PROPERTIES if original else MODIFIED_DATE_PROPERTIES


def _bears_date(node: dict, original: bool) -> bool:
    "Check whether the node carries a date of the requested kind."
    return any(
        isinstance(value, str) and value
        for prop in _date_properties(original)
        for value in _as_list(node.get(prop))
    )


class EntityGraph:
    """JSON-LD entities of a page, indexed by @id.

    Duplicate descriptions of the same entity are merged, with a warning
    on conflicting property values.
    """

    def __init__(self) -> None:
        self.entities: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self._reverse: dict[int, list[dict]] | None = None

    def add(self, data: Any) -> None:
        "Add parsed JSON-LD data (a document, a list or a @graph) to the graph."
        for node in _iter_top_nodes(data):
            canonical = self._index_node(node)
            if all(canonical is not entity for entity in self.entities):
                self.entities.append(canonical)

    def _index_node(self, node: dict) -> dict:
        "Register a node under its @id, merging duplicates, and index embedded nodes."
        node_id = node.get("@id")
        if isinstance(node_id, str) and node_id:
            existing = self.by_id.get(node_id)
            if existing is not None and existing is not node:
                self._merge(existing, node, node_id)
                # embedded entities of the duplicate still need indexing
                self._index_values(node)
                return existing
            self.by_id[node_id] = node
        self._index_values(node)
        return node

    def _index_values(self, node: dict) -> None:
        # snapshot: merging a nested duplicate may grow the node itself
        for value in list(node.values()):
            for item in _as_list(value):
                if isinstance(item, dict):
                    self._index_node(item)

    @staticmethod
    def _merge(target: dict, source: dict, node_id: str) -> None:
        "Merge a duplicate description into the canonical node (first value wins)."
        for key, value in source.items():
            if key == "@context":
                continue
            if key not in target:
                target[key] = value
            elif target[key] != value:
                LOGGER.warning(
                    "JSON-LD: conflicting descriptions of entity %s, "
                    "property %s: keeping %r, discarding %r",
                    node_id,
                    key,
                    target[key],
                    value,
                )

    def resolve(self, ref: Any) -> dict | None:
        "Resolve an @id reference or embedded node to a known entity."
        if isinstance(ref, str):
            return self.by_id.get(ref)
        if isinstance(ref, dict):
            ref_id = ref.get("@id")
            if isinstance(ref_id, str):
                return self.by_id.get(ref_id, ref if len(ref) > 1 else None)
            return ref
        return None

    def _references(self, node: dict) -> Iterator[dict]:
        "Yield the entities a node points to along the allowed relations."
        for relation in ALLOWED_RELATIONS:
            for ref in _as_list(node.get(relation)):
                target = self.resolve(ref)
                if target is not None:
                    yield target

    def diagnose(self) -> None:
        "Warn about circular references along the allowed relations."
        color: dict[int, int] = {}  # 1 = on the DFS path, 2 = done

        def visit(node: dict) -> None:
            color[id(node)] = 1
            for other in self._references(node):
                state = color.get(id(other), 0)
                if state == 1:
                    LOGGER.warning(
                        "JSON-LD: circular reference detected involving entity %s",
                        other.get("@id") or node.get("@id") or "?",
                    )
                elif state == 0:
                    visit(other)
            color[id(node)] = 2

        for entity in self.entities:
            if color.get(id(entity), 0) == 0:
                visit(entity)

    @staticmethod
    def _own_date_values(node: dict, original: bool) -> list[str]:
        return [
            value
            for prop in _date_properties(original)
            for value in _as_list(node.get(prop))
            if isinstance(value, str) and value
        ]

    def _reverse_map(self) -> dict[int, list[dict]]:
        "Map entities to the ones referencing them along allowed relations."
        if self._reverse is None:
            self._reverse = {}
            for entity in self.entities:
                for other in self._references(entity):
                    if other is not entity:
                        self._reverse.setdefault(id(other), []).append(entity)
        return self._reverse

    def _cluster(self, target: dict) -> list[dict]:
        "Target entity plus everything reachable along allowed relations (both ways)."
        cluster = [target]
        visited = {id(target)}
        queue = deque([target])
        while queue:
            node = queue.popleft()
            neighbors = [
                other
                for other in (
                    *self._references(node),
                    *self._reverse_map().get(id(node), []),
                )
                if id(other) not in visited
            ]
            # deterministic order, independent of the JSON node order
            neighbors.sort(key=lambda n: n.get("@id") or "")
            for other in neighbors:
                visited.add(id(other))
                cluster.append(other)
                queue.append(other)
        return cluster

    def candidate_values(self, target: dict, original: bool = True) -> list[str]:
        """Raw date strings allowed for the target entity.

        The target's own description decides; only if it carries no date
        may explicitly allowed relations (e.g. the web page of the article)
        contribute theirs. Neighbouring articles are never consulted.
        """
        own = self._own_date_values(target, original)
        if own:
            return own
        values = []
        for node in self._cluster(target)[1:]:
            values.extend(self._own_date_values(node, original))
        return values

    @staticmethod
    def _rank(node: dict, original: bool) -> tuple:
        "Deterministic ranking key: date-bearing articles with the smallest @id first."
        return (
            not _bears_date(node, original),
            not _is_article(node),
            node.get("@id") or "",
            # full content as final tie-break: never the JSON node order
            json.dumps(node, sort_keys=True),
        )

    def select_target(
        self,
        url: str | None = None,
        target_id: str | None = None,
        original: bool = True,
    ) -> dict | None:
        """Select the entity the caller is after.

        Each rule is authoritative and deterministic: once an entity is
        selected, its dates come only from the entity itself and the
        explicitly allowed relations — never from a neighbouring entity
        picked by document order.
        """
        # explicit entity identifier
        if target_id is not None:
            node = self.by_id.get(target_id)
            if node is None:
                LOGGER.warning("JSON-LD: target entity not found: %s", target_id)
            return node
        # page URL match
        if url:
            normalized = _normalize_url(url)
            matches = [
                entity
                for entity in self.entities
                if any(
                    _normalize_url(entity_url) == normalized
                    for entity_url in _node_urls(entity)
                )
            ]
            if matches:
                return min(matches, key=lambda n: self._rank(n, original))
        # entity designated as the main content of a page-like entity
        linked: list[dict] = []
        for entity in self.entities:
            for ref in _as_list(entity.get("mainEntity")):
                node = self.resolve(ref)
                if (
                    node is not None
                    and node is not entity
                    and all(node is not other for other in linked)
                ):
                    linked.append(node)
        if linked:
            return min(linked, key=lambda n: self._rank(n, original))
        # last resort: a single date-bearing entity is unambiguous
        bearing = [e for e in self.entities if _bears_date(e, original)]
        if len(bearing) == 1:
            return bearing[0]
        if len(bearing) > 1:
            LOGGER.debug(
                "JSON-LD: %d date-bearing entities and no target selector, "
                "refusing to pick one by document order",
                len(bearing),
            )
        return None


def build_entity_graph(texts: list[str]) -> tuple[EntityGraph, list[str]]:
    "Parse JSON-LD script contents into an entity graph, returning it and the unparseable texts."
    graph = EntityGraph()
    broken = []
    for text in texts:
        try:
            data = json.loads(text)
        except ValueError as err:  # JSONDecodeError included
            LOGGER.debug("JSON-LD parsing failed: %s", err)
            broken.append(text)
        else:
            graph.add(data)
    return graph, broken
