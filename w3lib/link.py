"""
This module contains functions to parse HTTP ``Link`` header fields
(:rfc:`8288`, "Web Linking") and to pick a pagination link out of them.

A single response can carry links that belong to different collections
(e.g. a feed and its embedded comments), each with its own ``next`` page.
:func:`select_pagination_link` lets the caller state which collection the
pagination belongs to (through the link ``anchor`` parameter, which defaults
to the response URL) and which relation is wanted, and reports either the
unique matching target or an explicit, inspectable failure.  It only
interprets headers that are already available; it never issues requests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urljoin

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "Link",
    "LinkSelection",
    "LinkSelectionStatus",
    "parse_link_header",
    "select_pagination_link",
]

_QUOTED_PAIR_RE = re.compile(r"\\(.)")
_LINK_VALUE_RE = re.compile(r"\s*<([^>]*)>(.*)", re.DOTALL)


def _unquote(value: str) -> str:
    return _QUOTED_PAIR_RE.sub(r"\1", value)


def _split_top_level(value: str, delimiter: str) -> list[str]:
    """Split *value* on *delimiter* characters that are not inside a
    quoted string or a ``<...>`` target."""
    parts = []
    buf: list[str] = []
    in_quotes = False
    in_target = False
    escaped = False
    for char in value:
        if escaped:
            buf.append(char)
            escaped = False
        elif in_quotes and char == "\\":
            buf.append(char)
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
            buf.append(char)
        elif not in_quotes and char == "<":
            in_target = True
            buf.append(char)
        elif not in_quotes and char == ">":
            in_target = False
            buf.append(char)
        elif char == delimiter and not in_quotes and not in_target:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    parts.append("".join(buf))
    return parts


@dataclass
class Link:
    """A single link-value parsed from a ``Link`` header field.

    The original declaration is kept in :attr:`raw` so that any selection
    result can be traced back to what the server actually sent.
    """

    target: str
    """The URI-Reference between ``<`` and ``>``, unresolved."""

    params: dict[str, str | None]
    """Link parameters, lower-cased name to value (``None`` if the parameter
    had no value).  When a parameter is repeated the first occurrence wins,
    as :rfc:`8288` mandates for ``rel`` and ``anchor``."""

    raw: str
    """The original link-value text, as it appeared in the header field."""

    @property
    def relations(self) -> frozenset[str]:
        """Relation types of this link, lower-cased.  A single ``rel``
        parameter can carry several space-separated relation types."""
        value = self.params.get("rel")
        if not value:
            return frozenset()
        return frozenset(value.lower().split())

    @property
    def anchor(self) -> str | None:
        """The ``anchor`` parameter (the collection this link belongs to),
        or ``None`` if absent, in which case the link belongs to the
        response URL."""
        return self.params.get("anchor")


def parse_link_header(value: str | bytes) -> list[Link]:
    """Parse a ``Link`` header field value into a list of :class:`Link`.

    Accepts a single field value (which may itself contain several
    comma-separated link-values).  Malformed link-values are skipped.

    >>> links = parse_link_header('</page/2>; rel="next"; title="2, of 4"')
    >>> links[0].target
    '/page/2'
    >>> sorted(links[0].relations)
    ['next']
    >>> links[0].params['title']
    '2, of 4'
    """
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    links = []
    for text in _split_top_level(value, ","):
        match = _LINK_VALUE_RE.match(text)
        if match is None:
            continue
        target, rest = match.group(1), match.group(2)
        params: dict[str, str | None] = {}
        for raw_param in _split_top_level(rest, ";"):
            param_text = raw_param.strip()
            if not param_text:
                continue
            name, separator, param_value = param_text.partition("=")
            name = name.strip().lower()
            if not name:
                continue
            param_value = param_value.strip()
            parsed_value: str | None
            if not separator:
                parsed_value = None
            elif (
                len(param_value) >= 2
                and param_value.startswith('"')
                and (param_value.endswith('"'))
            ):
                parsed_value = _unquote(param_value[1:-1])
            else:
                parsed_value = param_value
            params.setdefault(name, parsed_value)
        links.append(Link(target=target, params=params, raw=text.strip()))
    return links


class LinkSelectionStatus(Enum):
    """Outcome of :func:`select_pagination_link`."""

    UNIQUE = "unique"
    """Exactly one distinct target matches: :attr:`LinkSelection.url` is set."""

    NOT_FOUND = "not_found"
    """No link matches the collection and relation."""

    AMBIGUOUS = "ambiguous"
    """Several links match but they point to different targets."""


@dataclass
class LinkSelection:
    """Result of :func:`select_pagination_link`."""

    status: LinkSelectionStatus
    url: str | None
    """The resolved, absolute target URL when
    :attr:`status` is :attr:`LinkSelectionStatus.UNIQUE`, else ``None``."""

    matches: list[Link] = field(default_factory=list)
    """Every link-value that matched the collection and relation, in
    declaration order, with their original text in :attr:`Link.raw`."""

    response_url: str = ""
    collection: str = ""
    """The resolved collection identifier the selection was scoped to."""

    rel: str = ""

    @property
    def ok(self) -> bool:
        """``True`` when a unique target was found."""
        return self.status is LinkSelectionStatus.UNIQUE


def _iter_links(link_headers: str | bytes | Sequence[str | bytes]) -> list[Link]:
    if isinstance(link_headers, (str, bytes)):
        link_headers = [link_headers]
    links = []
    for value in link_headers:
        links.extend(parse_link_header(value))
    return links


def select_pagination_link(
    response_url: str,
    link_headers: str | bytes | Sequence[str | bytes],
    collection: str | None = None,
    rel: str = "next",
) -> LinkSelection:
    """Select the pagination target for one collection out of the ``Link``
    header fields of a response, without following it.

    :param response_url: URL of the response the headers came from.  It is
        the base used to resolve relative targets and relative ``anchor``
        parameters, and it is the default collection identifier.
    :param link_headers: one or more ``Link`` header field values.
    :param collection: identifier of the collection being paginated,
        resolved against *response_url*.  A link belongs to this collection
        when its ``anchor`` parameter (resolved the same way) equals it;
        links without ``anchor`` belong to *response_url* itself.  Defaults
        to *response_url*.
    :param rel: the relation type to look for, e.g. ``"next"``.  Matching
        is case-insensitive and a link listing several relation types in
        one ``rel`` parameter matches any of them.

    The returned :class:`LinkSelection` distinguishes "no matching link"
    (:attr:`~LinkSelectionStatus.NOT_FOUND`) from "conflicting matching
    links" (:attr:`~LinkSelectionStatus.AMBIGUOUS`): the last matching
    ``next`` is never silently picked.  Duplicate declarations of the same
    target collapse into one unique result, and every matching declaration
    stays available in :attr:`LinkSelection.matches` for inspection.

    Two collections paginating in the same response do not interfere:

    >>> selection = select_pagination_link(
    ...     "https://shop.example/feed",
    ...     [
    ...         '</feed?page=2>; rel="next"',
    ...         '</comments?cursor=c2>; rel="next"; anchor="#comments"',
    ...     ],
    ... )
    >>> selection.status, selection.url
    (<LinkSelectionStatus.UNIQUE: 'unique'>, 'https://shop.example/feed?page=2')
    >>> select_pagination_link(
    ...     "https://shop.example/feed",
    ...     [
    ...         '</feed?page=2>; rel="next"',
    ...         '</comments?cursor=c2>; rel="next"; anchor="#comments"',
    ...     ],
    ...     collection="https://shop.example/feed#comments",
    ... ).url
    'https://shop.example/comments?cursor=c2'
    """
    wanted = rel.lower()
    resolved_collection = (
        urljoin(response_url, collection) if collection is not None else response_url
    )
    matches = []
    for link in _iter_links(link_headers):
        anchor = link.anchor
        context = urljoin(response_url, anchor) if anchor is not None else response_url
        if context != resolved_collection:
            continue
        if wanted not in link.relations:
            continue
        matches.append(link)

    targets: list[str] = []
    for link in matches:
        resolved = urljoin(response_url, link.target)
        if resolved not in targets:
            targets.append(resolved)

    if not targets:
        status = LinkSelectionStatus.NOT_FOUND
        url = None
    elif len(targets) == 1:
        status = LinkSelectionStatus.UNIQUE
        url = targets[0]
    else:
        status = LinkSelectionStatus.AMBIGUOUS
        url = None
    return LinkSelection(
        status=status,
        url=url,
        matches=matches,
        response_url=response_url,
        collection=resolved_collection,
        rel=wanted,
    )
