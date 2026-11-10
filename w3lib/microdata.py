"""
Extraction of HTML Microdata (``itemscope``/``itemprop`` markup).

The entry point is :func:`extract_microdata`. Unlike scrapers that look for
properties by CSS class names, it reads the Microdata attributes the page
actually declares, following the Microdata specification:

- every top-level ``itemscope`` element becomes one item, in document order;
- ``itemtype`` tokens and ``itemid`` are kept as the item's type and identity;
- every ``itemprop`` value is kept, so a property with several values is a list
  rather than its last value only;
- nested ``itemscope`` properties are returned as items themselves;
- ``itemref`` pulls properties in by ID, including elements that live outside
  the referencing item's subtree;
- element-specific value sources (``href``, ``src``, ``data``, ``value``,
  ``datetime``, ``content``, text content) are distinguished, and URL values
  are resolved against the base URL of the document.

No network access is performed: referenced documents are never fetched.
"""

from __future__ import annotations

import re
import warnings
from html.parser import HTMLParser
from typing import Any, TypeAlias

from w3lib._url import _SCHEME_RE
from w3lib._util import to_unicode
from w3lib.html import HTML5_WHITESPACE, get_base_url
from w3lib.url import _urljoin, safe_url_string

#: A property value is either text (URL values resolved to absolute strings)
#: or another, nested Microdata item.
MicrodataValue: TypeAlias = str | dict[str, Any]
#: A Microdata item: its ``properties`` mapping plus the optional ``type`` and
#: ``id`` entries, mirroring the Microdata JSON shape.
MicrodataItem: TypeAlias = dict[str, Any]

# Elements whose URL attribute supplies their Microdata value.
_URL_ATTRIBUTE: dict[str, str] = {
    "a": "href",
    "area": "href",
    "link": "href",
    "audio": "src",
    "embed": "src",
    "iframe": "src",
    "img": "src",
    "source": "src",
    "track": "src",
    "video": "src",
    "object": "data",
}
# Elements whose named non-URL attribute supplies their value.
_ATTR_ATTRIBUTE: dict[str, str] = {
    "meta": "content",
    "data": "value",
    "meter": "value",
    "time": "datetime",
}
_VOID_ELEMENTS = frozenset(
    (
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    )
)
_WHITESPACE_RE = re.compile(r"[\t\n\f\r ]+")


class _Node:
    """One parsed element, with its attributes and document-order content.

    ``nodes`` mixes child :class:`_Node` objects with the text chunks that sit
    between them, so the original element order is preserved when reading text
    content.
    """

    __slots__ = ("attrs", "is_scope", "nodes", "referenced", "tag")

    def __init__(self, tag: str, attrs: dict[str, str | None]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.nodes: list[_Node | str] = []
        self.is_scope = False
        # Set during the walk when another item names this scope as a
        # property; such an item is not returned as a top-level item.
        self.referenced = False


class _TreeBuilder(HTMLParser):
    """Build the small element tree the Microdata walk needs.

    Python's :class:`HTMLParser` already decodes character references,
    lowercases tag and attribute names and keeps attribute order.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self._open: list[_Node] = [self.root]
        self.scope_nodes: list[_Node] = []
        self.by_id: dict[str, _Node] = {}

    def _make_node(self, tag: str, attrs: list[tuple[str, str | None]]) -> _Node:
        attributes: dict[str, str | None] = {}
        for name, value in attrs:
            # On duplicate attribute names the first occurrence wins, as in
            # the HTML tokenizer.
            if name not in attributes:
                attributes[name] = value
        node = _Node(tag, attributes)
        node.is_scope = "itemscope" in attributes
        if node.is_scope:
            self.scope_nodes.append(node)
        element_id = attributes.get("id")
        if element_id is not None and element_id not in self.by_id:
            self.by_id[element_id] = node
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._make_node(tag, attrs)
        self._open[-1].nodes.append(node)
        if tag not in _VOID_ELEMENTS:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._make_node(tag, attrs)
        self._open[-1].nodes.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_ELEMENTS:
            return
        # Tolerate mismatched tags by popping up to the matching open element.
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth].tag != tag:
                continue
            del self._open[depth:]
            return

    def handle_data(self, data: str) -> None:
        self._open[-1].nodes.append(data)


def _tokens(value: str | None) -> list[str]:
    """Split an unordered set of space-separated tokens, without duplicates."""
    if not value:
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for token in value.split():
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip(HTML5_WHITESPACE)


def _text_content(node: _Node) -> str:
    """Return the text content of *node* as a Microdata property value.

    All descendant text is concatenated in document order, then whitespace
    sequences are collapsed into single spaces and stripped from both ends, as
    the Microdata algorithm prescribes for an element without a value
    attribute.
    """
    chunks: list[str] = []
    stack: list[_Node | str] = list(reversed(node.nodes))
    while stack:
        part = stack.pop()
        if isinstance(part, str):
            chunks.append(part)
        else:
            stack.extend(reversed(part.nodes))
    return _collapse_whitespace("".join(chunks))


def extract_microdata(
    text: str | bytes,
    baseurl: str | bytes = "",
    encoding: str = "utf-8",
) -> list[MicrodataItem]:
    """Extract the Microdata items of an HTML document.

    Every top-level ``itemscope`` element is returned as a mapping in document
    order. Each mapping holds:

    - ``"type"``: the tokens of the ``itemtype`` attribute, if present;
    - ``"id"``: the ``itemid`` value, resolved against the document base URL
      when relative, if present;
    - ``"properties"``: a mapping of ``itemprop`` names to the list of their
      values, in the order in which they are declared. Nested ``itemscope``
      properties are themselves item mappings, so every value keeps the item
      it belongs to.

    URL-valued properties (``href`` of ``a``/``area``/``link``, ``src`` of
    media and embedding elements, ``data`` of ``object``) and relative
    ``itemid`` values are resolved against *baseurl* and any ``<base>`` element
    of *text*. With no absolute *baseurl*, relative values are kept relative.

    Repeated ``itemref`` references and reference cycles terminate: an element
    is collected at most once per item, and a nested item already being built
    is returned as the same in-progress mapping.

    :param text: the HTML document, as ``str`` or ``bytes``.
    :param baseurl: the absolute URL of the document, used to resolve relative
        URLs together with any ``<base>`` tag.
    :param encoding: the encoding of *text* when it is ``bytes``.
    """
    markup = to_unicode(text, encoding)
    document_url = to_unicode(baseurl)
    if _SCHEME_RE.match(document_url):
        base = get_base_url(text, baseurl, encoding)
    else:
        # No absolute document URL was given. A page may still declare an
        # absolute <base> of its own; get_base_url() warns about the relative
        # fallback argument, which callers of this function never asked for,
        # so the warning is swallowed and only an absolute result is used.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            declared = get_base_url(text, baseurl, encoding)
        base = declared if _SCHEME_RE.match(declared) else ""

    builder = _TreeBuilder()
    builder.feed(markup)
    builder.close()

    memo: dict[_Node, MicrodataItem] = {}

    def resolve_url(value: str) -> str:
        url = value.strip(HTML5_WHITESPACE)
        if not base:
            return url
        if not url:
            # An empty URL attribute resolves to the document base itself.
            return base
        try:
            return safe_url_string(_urljoin(base, url), encoding)
        except ValueError:
            return url

    def property_value(node: _Node) -> MicrodataValue:
        if node.is_scope:
            return build_item(node)
        attributes = node.attrs
        if (url_attr := _URL_ATTRIBUTE.get(node.tag)) is not None:
            # The value is the URL attribute resolved against the base; with
            # no such attribute there is no URL, so the value is empty rather
            # than the base URL itself.
            raw = attributes.get(url_attr)
            if raw is None:
                return ""
            return resolve_url(raw)
        if (attr_name := _ATTR_ATTRIBUTE.get(node.tag)) is not None:
            # The Microdata spec pins these elements to their named attribute
            # and prescribes the empty string when it is absent.
            raw = attributes.get(attr_name)
            return raw if raw is not None else ""
        return _text_content(node)

    def attach(node: _Node, properties: dict[str, list[MicrodataValue]]) -> None:
        names = _tokens(node.attrs.get("itemprop"))
        if not names:
            return
        if node.is_scope:
            # Named as a property, so the nested item is not top-level.
            node.referenced = True
        value = property_value(node)
        for name in names:
            properties.setdefault(name, []).append(value)

    def walk_subtree(
        node: _Node,
        properties: dict[str, list[MicrodataValue]],
        visited: set[_Node],
    ) -> None:
        # Iterative pre-order walk; reversed pushes keep document order on pop.
        stack: list[_Node | str] = list(reversed(node.nodes))
        while stack:
            child = stack.pop()
            if not isinstance(child, _Node) or child in visited:
                continue
            visited.add(child)
            if child.is_scope:
                # No itemscope boundary is crossed: a nested item owns every
                # descendant. One with an itemprop is attached as a property,
                # one without it is simply not part of this item.
                if "itemprop" in child.attrs:
                    attach(child, properties)
                continue
            if "itemprop" in child.attrs:
                attach(child, properties)
            stack.extend(reversed(child.nodes))

    def build_item(node: _Node) -> MicrodataItem:
        if node in memo:
            return memo[node]
        item: MicrodataItem = {}
        # Publish before walking descendants so nested cycles settle on this
        # same mapping instead of recursing forever.
        memo[node] = item

        if item_types := _tokens(node.attrs.get("itemtype")):
            item["type"] = item_types
        if (raw_id := node.attrs.get("itemid")) is not None:
            item_id = raw_id.strip(HTML5_WHITESPACE)
            if item_id:
                item["id"] = resolve_url(item_id)

        properties: dict[str, list[MicrodataValue]] = {}
        visited: set[_Node] = {node}
        walk_subtree(node, properties, visited)

        # itemref contributes after the subtree, in reference order. Referenced
        # elements may sit outside this item's scope altogether.
        for reference in _tokens(node.attrs.get("itemref")):
            target = builder.by_id.get(reference)
            if target is None or target in visited:
                continue
            visited.add(target)
            if target.is_scope:
                if "itemprop" in target.attrs:
                    attach(target, properties)
                continue
            if "itemprop" in target.attrs:
                attach(target, properties)
            walk_subtree(target, properties, visited)

        item["properties"] = properties
        return item

    # Build every item first: building an item marks the scopes it names as
    # properties, including ones earlier in the document reached via itemref.
    # Only then can the top-level ones be told apart from the nested ones.
    for node in builder.scope_nodes:
        build_item(node)
    return [memo[node] for node in builder.scope_nodes if not node.referenced]
