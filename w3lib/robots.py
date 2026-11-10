"""
Indexing permission analysis.

Merge ``X-Robots-Tag`` HTTP response headers with HTML robots ``<meta>`` tags
into a single answer about what a crawler may do with a page it was allowed to
fetch: whether the page may be indexed, whether its links may be followed, and
how much of it may be kept as a snippet. Crawling permission (robots.txt) is
out of scope; nothing here collapses into a single "allowed" boolean.

Declarations aimed at a specific crawler (``X-Robots-Tag: googlebot: ...`` or
``<meta name="googlebot" ...>``) apply only when they match the user agent the
caller reports, and when any of them match they replace the generic
declarations (``robots``) entirely, as Google documents. Within the applicable
set, repeated or conflicting declarations resolve conservatively: any
``noindex``/``nofollow``/``nosnippet`` wins over any allowance, the smallest
``max-snippet`` wins, and the earliest ``unavailable_after`` wins. Directives
that are not understood are kept verbatim in
:attr:`IndexingPermission.unknown_directives` instead of being dropped.

Whether an ``unavailable_after`` deadline has passed is computed against a
moment the caller provides, never against the wall clock of the machine
running the analysis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

from w3lib._util import iter_tag_attributes, to_unicode
from w3lib.html import _build_meta_scan_pattern, replace_entities

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "IndexingPermission",
    "RobotsRule",
    "indexing_permission",
    "merge_robots_rules",
    "parse_meta_robots",
    "parse_x_robots_tag",
]

#: Meta names and X-Robots-Tag targets that declare rules for every crawler.
_GENERIC_TARGETS = frozenset({"robots", "all", "*"})

# Directive names that carry a ": value" part, so that the token before the
# first ":" of a declaration is not mistaken for a crawler name. Only some of
# them are interpreted; the rest are kept as unknown directives, but their
# values must not be read as directives of their own.
_VALUE_DIRECTIVES = frozenset(
    {"max_snippet", "unavailable_after", "max_image_preview", "max_video_preview"}
)

_target_re = re.compile(r"[a-z0-9_*.\-]+")
_unavailable_after_re = re.compile(
    r"unavailable[_-]after\s*:\s*(?P<value>.+)$", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class RobotsRule:
    """Indexing directives of a single declaration target.

    A declaration target is one ``X-Robots-Tag`` header addressable set of
    directives (generic, or prefixed with a crawler name) or one robots
    ``<meta>`` tag. ``max_snippet`` of ``-1`` means "no limit", as it does in
    the ``max-snippet: -1`` directive. ``unknown`` holds the directives that
    could not be interpreted, as written.
    """

    source: str
    specific: bool = False
    noindex: bool = False
    nofollow: bool = False
    nosnippet: bool = False
    max_snippet: int | None = None
    unavailable_after: datetime | None = None
    unknown: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndexingPermission:
    """What a crawler may do with a fetched page, and why.

    Every effective value is paired with the ``source`` of the declaration it
    came from; a ``None`` source means the value is the default rather than a
    declared one. ``max_snippet`` is the number of characters a textual
    snippet may keep, ``0`` when snippets are forbidden and ``None`` when
    they are unlimited. ``unavailable`` is ``None`` when no deadline was
    declared or no moment was given to compare it with.
    """

    can_index: bool = True
    index_source: str | None = None
    can_follow: bool = True
    follow_source: str | None = None
    nosnippet: bool = False
    nosnippet_source: str | None = None
    max_snippet: int | None = None
    max_snippet_source: str | None = None
    unavailable_after: datetime | None = None
    unavailable_after_source: str | None = None
    unavailable: bool | None = None
    unknown_directives: tuple[str, ...] = ()


def _parse_date(value: str) -> datetime | None:
    """Return the moment *value* declares, assuming UTC when it names no zone,
    or ``None`` when it is not a date in any of the formats ``unavailable_after``
    is written in (RFC 2822, RFC 850 or asctime)."""
    try:
        moment = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _split_directives(text: str) -> list[str]:
    """Split *text* into single directives at commas.

    An ``unavailable_after`` value may itself contain a comma (RFC 850 dates
    do), so a piece that follows an ``unavailable_after`` whose value does not
    yet parse as a date is joined back onto it.
    """
    parts: list[str] = []
    for piece in text.split(","):
        directive = piece.strip()
        if not directive:
            continue
        if (
            parts
            and (m := _unavailable_after_re.search(parts[-1]))
            and _parse_date(m.group("value")) is None
        ):
            parts[-1] = f"{parts[-1]}, {directive}"
            continue
        parts.append(directive)
    return parts


def _split_target(part: str) -> tuple[str | None, str]:
    """Split an ``X-Robots-Tag`` part into a crawler name and the directive
    that follows it, returning ``(None, part)`` when there is no crawler name.

    The token before the first ":" is a crawler name only when it is not
    itself a directive that carries a value, e.g. ``max-snippet: 50``.
    """
    name, sep, rest = part.partition(":")
    token = name.strip().lower()
    if not sep or not _target_re.fullmatch(token):
        return None, part
    if token.replace("-", "_") in _VALUE_DIRECTIVES:
        return None, part
    return token, rest


def _parse_max_snippet(value: str) -> int | None:
    """Return the ``max-snippet`` limit *value* declares, where -1 means no
    limit, or ``None`` when it is not a valid one."""
    try:
        limit = int(value.strip())
    except ValueError:
        return None
    return limit if limit >= -1 else None


def _merge_snippet(current: int | None, new: int) -> int:
    """Merge two ``max-snippet`` values conservatively: the smallest limit
    wins, and -1 (no limit) loses to any numbered limit."""
    if current is None or current < 0:
        return new
    if new < 0:
        return current
    return min(current, new)


@dataclass
class _RuleBuilder:
    source: str
    specific: bool
    noindex: bool = False
    nofollow: bool = False
    nosnippet: bool = False
    max_snippet: int | None = None
    unavailable_after: datetime | None = None
    unknown: list[str] = field(default_factory=list)

    def apply(self, directive: str) -> None:
        name, sep, value = directive.partition(":")
        key = name.strip().lower().replace("-", "_")
        if key == "none":
            self.noindex = True
            self.nofollow = True
        elif key == "noindex":
            self.noindex = True
        elif key == "nofollow":
            self.nofollow = True
        elif key == "nosnippet":
            self.nosnippet = True
        elif key in ("all", "index", "follow"):
            # Explicit allowances: they state the defaults, and lose to any
            # restriction the same target declares.
            pass
        elif key == "max_snippet":
            if sep and (limit := _parse_max_snippet(value)) is not None:
                self.max_snippet = _merge_snippet(self.max_snippet, limit)
            else:
                self.unknown.append(" ".join(directive.split()))
        elif key == "unavailable_after":
            if sep and (moment := _parse_date(value)) is not None:
                if self.unavailable_after is None or moment < self.unavailable_after:
                    self.unavailable_after = moment
            else:
                self.unknown.append(" ".join(directive.split()))
        else:
            self.unknown.append(" ".join(directive.split()))

    def freeze(self) -> RobotsRule:
        return RobotsRule(
            source=self.source,
            specific=self.specific,
            noindex=self.noindex,
            nofollow=self.nofollow,
            nosnippet=self.nosnippet,
            max_snippet=self.max_snippet,
            unavailable_after=self.unavailable_after,
            unknown=tuple(self.unknown),
        )


def _target_applies(target: str, user_agent: str | None) -> bool:
    """Return whether a declaration for the crawler *target* applies to a
    crawl made as *user_agent*: the target token must appear in the user
    agent, case-insensitively, the way Google matches crawler names."""
    return user_agent is not None and target in user_agent.lower()


def parse_x_robots_tag(
    values: str | bytes | Iterable[str | bytes],
    user_agent: str | None = None,
) -> list[RobotsRule]:
    """Parse ``X-Robots-Tag`` header *values* into rules.

    *values* is a single header value or every value the response carried;
    they are merged per declaration target. A ``crawler:`` prefix applies the
    directives that follow it on the same header to that crawler, and such
    rules are returned only when the crawler name matches *user_agent*.
    """
    if isinstance(values, (str, bytes)):
        values = (values,)
    builders: dict[str | None, _RuleBuilder] = {}
    for value in values:
        text = to_unicode(value) if isinstance(value, bytes) else value
        target: str | None = None
        for part in _split_directives(text):
            prefix, directive = _split_target(part)
            if prefix is not None:
                target = None if prefix in _GENERIC_TARGETS else prefix
                if not directive.strip():
                    continue
            if target not in builders:
                builders[target] = _RuleBuilder(
                    "X-Robots-Tag" if target is None else f"X-Robots-Tag ({target})",
                    target is not None,
                )
            builders[target].apply(directive)
    return [
        builder.freeze()
        for target, builder in builders.items()
        if target is None or _target_applies(target, user_agent)
    ]


def parse_meta_robots(
    html: str | bytes,
    user_agent: str | None = None,
    encoding: str = "utf-8",
    ignore_tags: Iterable[str] = ("script", "noscript"),
) -> list[RobotsRule]:
    """Parse the robots ``<meta>`` tags of the HTML document *html* into rules.

    ``<meta name="robots">`` declares generic rules. A tag named after a
    crawler declares rules for that crawler only, and is read only when the
    name matches *user_agent*; other named tags (``description``, ``viewport``,
    ...) are not robots declarations and are ignored. *ignore_tags* names the
    elements whose content is not markup, as in
    :func:`w3lib.html.get_meta_refresh`.
    """
    utext = to_unicode(html, encoding)
    ignored = tuple(sorted({tag.lower() for tag in ignore_tags}))
    rules = []
    for tag in _build_meta_scan_pattern(ignored).finditer(utext):
        attrs = tag.group("attrs")
        if attrs is None:
            continue
        name: str | None = None
        content: str | None = None
        for attr_name, raw_value in iter_tag_attributes(attrs):
            value = replace_entities(raw_value) if "&" in raw_value else raw_value
            if attr_name == "name" and name is None:
                name = value.strip()
            elif attr_name == "content" and content is None:
                content = value
        if not name or content is None:
            continue
        token = name.lower()
        if token in _GENERIC_TARGETS:
            specific = False
        elif _target_applies(token, user_agent):
            specific = True
        else:
            continue
        builder = _RuleBuilder(f'<meta name="{name}">', specific)
        for directive in _split_directives(content):
            builder.apply(directive)
        rules.append(builder.freeze())
    return rules


def merge_robots_rules(
    rules: Iterable[RobotsRule],
    now: datetime | None = None,
) -> IndexingPermission:
    """Merge *rules* into a single :class:`IndexingPermission`.

    When any crawler-specific rules apply, generic rules are ignored, as
    Google documents; otherwise the generic ones apply. Within the applicable
    rules, conflicts resolve conservatively: any restriction wins over any
    allowance, the smallest ``max-snippet`` wins, and the earliest
    ``unavailable_after`` wins. *now* is the moment deadlines are compared
    against; a naive *now* is read as UTC. Without *now*, ``unavailable`` is
    ``None`` even when a deadline was declared.
    """
    applicable = list(rules)
    specific = [rule for rule in applicable if rule.specific]
    if specific:
        applicable = specific

    noindex_source = next((r.source for r in applicable if r.noindex), None)
    nofollow_source = next((r.source for r in applicable if r.nofollow), None)
    nosnippet_source = next((r.source for r in applicable if r.nosnippet), None)

    snippet_source = None
    snippet: int | None = None
    for rule in applicable:
        if rule.max_snippet is not None:
            snippet = _merge_snippet(snippet, rule.max_snippet)
    if snippet is not None and snippet >= 0:
        snippet_source = next(
            r.source for r in applicable if r.max_snippet == snippet
        )
    elif snippet is not None:
        # Only "no limit" (-1) declarations: the default stands.
        snippet = None

    deadline_source = None
    deadline = None
    for rule in applicable:
        if rule.unavailable_after is not None and (
            deadline is None or rule.unavailable_after < deadline
        ):
            deadline = rule.unavailable_after
            deadline_source = rule.source

    unavailable: bool | None = None
    if deadline is not None and now is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        unavailable = now >= deadline

    unknown = tuple(
        dict.fromkeys(directive for rule in applicable for directive in rule.unknown)
    )

    if nosnippet_source is not None:
        snippet, snippet_source = 0, nosnippet_source

    index_source = noindex_source
    if index_source is None and unavailable:
        index_source = deadline_source

    return IndexingPermission(
        can_index=noindex_source is None and unavailable is not True,
        index_source=index_source,
        can_follow=nofollow_source is None,
        follow_source=nofollow_source,
        nosnippet=nosnippet_source is not None,
        nosnippet_source=nosnippet_source,
        max_snippet=snippet,
        max_snippet_source=snippet_source,
        unavailable_after=deadline,
        unavailable_after_source=deadline_source,
        unavailable=unavailable,
        unknown_directives=unknown,
    )


def indexing_permission(
    *,
    x_robots_tag: str | bytes | Iterable[str | bytes] | None = None,
    html: str | bytes | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
    encoding: str = "utf-8",
) -> IndexingPermission:
    """Return what a crawler may do with a fetched page, beyond fetching it.

    *x_robots_tag* is the value, or every value, of the ``X-Robots-Tag``
    response header; *html* is the response body, whose robots ``<meta>`` tags
    are read. *user_agent* is the user agent the crawl identifies as, used to
    select crawler-specific declarations. *now* is the moment
    ``unavailable_after`` deadlines are compared against. The result keeps
    indexing, link following and snippet retention as separate answers, each
    with the source of the declaration that decided it.
    """
    rules: list[RobotsRule] = []
    if x_robots_tag is not None:
        rules.extend(parse_x_robots_tag(x_robots_tag, user_agent))
    if html is not None:
        rules.extend(parse_meta_robots(html, user_agent, encoding))
    return merge_robots_rules(rules, now=now)
