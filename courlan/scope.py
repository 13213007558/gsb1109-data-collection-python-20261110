"""
Reusable crawl scope rules: decide whether a URL belongs to the site
sections a crawl is allowed to visit, without string ``startswith`` on
raw URLs.

A :class:`ScopeRule` pins an exact host (optionally its subdomains), an
optional port and a path-segment prefix, and may exclude a few subtrees.
Hosts are compared in canonical form (lowercase, punycode, no trailing
dot, canonical IP literals) and paths after percent-decoding, so IDN
names, FQDN trailing dots, IPv6 spellings and encoded slashes all yield
consistent decisions. Query strings and fragments never change which
rule a path belongs to. Rules are validated at construction time, i.e.
before any crawling starts.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import NamedTuple
from urllib.parse import unquote, urlsplit, urlunsplit

from .clean import DEFAULT_PORTS
from .hosts import _ascii_host, _canonical_ip
from .urlutils import _strip_trailing_dot

__all__ = ["Scope", "ScopeMatch", "ScopeRule", "ScopeRuleError"]

# collapse repeated slashes after percent-decoding ("/news//x" == "/news/x")
_SLASHES = re.compile(r"/+")

# ASCII hostname label, letters/digits/hyphens, no leading or trailing hyphen
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

_SCHEMES = frozenset(DEFAULT_PORTS)


class ScopeRuleError(ValueError):
    "Raised when a scope rule is invalid, before any crawling starts."


def _normalize_host(host: str) -> str:
    "Return the canonical comparison form of a rule or URL host."
    # canonical IP literal (IPv4/IPv6), else lowercase punycode without FQDN dot
    return _canonical_ip(host) or _ascii_host(_strip_trailing_dot(host.lower()))


def _validate_host(host: str) -> str:
    "Normalize a rule host, rejecting anything that is not a bare host."
    if not isinstance(host, str) or not host.strip():
        raise ScopeRuleError(f"scope rule host must be a non-empty string: {host!r}")
    candidate = host.strip()
    # tolerate the bracketed IPv6 form used in netlocs
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if _canonical_ip(candidate):
        return _canonical_ip(candidate)  # type: ignore[return-value]
    if any(char in candidate for char in "/@?#: \\") or ".." in candidate:
        raise ScopeRuleError(
            f"scope rule host must be a bare host, not a URL or netloc: {host!r}"
        )
    ascii_host = _normalize_host(candidate)
    if len(ascii_host) > 253 or not all(
        _LABEL.fullmatch(label) for label in ascii_host.split(".")
    ):
        raise ScopeRuleError(f"invalid host in scope rule: {host!r}")
    return ascii_host


def _validate_port(port: int | None) -> int | None:
    if port is None:
        return None
    if isinstance(port, bool) or not isinstance(port, int):
        raise ScopeRuleError(f"scope rule port must be an integer: {port!r}")
    if not 1 <= port <= 65535:
        raise ScopeRuleError(f"scope rule port out of range: {port!r}")
    return port


def _remove_dot_segments(path: str) -> str:
    "Resolve '.' and '..' the way servers do (RFC 3986 §5.2.4)."
    segments = []
    for segment in path.split("/"):
        if segment == "..":
            if segments:
                segments.pop()
        elif segment != ".":
            segments.append(segment)
    # the path starts with "/", so the first split piece is "" and joins back to it
    return "/".join(segments) or "/"


def _normalize_path(path: str) -> str:
    """Percent-decode (encoded slashes included), collapse repeated slashes,
    resolve dot segments and drop trailing slashes, so equivalent spellings
    compare equal and traversal cannot escape the rule's subtree."""
    path = _SLASHES.sub("/", unquote(path, errors="replace"))
    if not path.startswith("/"):
        path = "/" + path
    return _remove_dot_segments(path).rstrip("/") or "/"


def _validate_path(path: str, what: str = "path") -> str:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ScopeRuleError(f"scope rule {what} must start with '/': {path!r}")
    if "?" in path or "#" in path:
        raise ScopeRuleError(
            f"scope rule {what} must not contain a query or fragment: {path!r}"
        )
    return _normalize_path(path)


def _path_under(path: str, prefix: str) -> bool:
    "Segment-prefix test: '/news' covers '/news' and '/news/x', not '/newsletter'."
    return prefix == "/" or path == prefix or path.startswith(prefix + "/")


@dataclass(frozen=True)
class ScopeRule:
    """One crawl scope rule: an exact host, an optional port and a
    path-segment prefix, with optional excluded subtrees.

    ``host`` is a bare host (no scheme, userinfo, port or path); IDN names
    may be given in unicode or punycode form, IPs in any valid spelling.
    ``port=None`` matches any port, otherwise the URL's effective port
    (explicit or scheme default) must equal it. ``include_subdomains``
    extends the rule to all subdomains of ``host`` (not allowed for IP
    hosts). Every entry of ``exclude`` is a path prefix strictly below
    ``path`` whose subtree is left out.
    """

    host: str
    port: int | None = None
    path: str = "/"
    exclude: tuple[str, ...] = ()
    include_subdomains: bool = False

    def __post_init__(self) -> None:
        host = _validate_host(self.host)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", _validate_port(self.port))
        path = _validate_path(self.path)
        object.__setattr__(self, "path", path)
        if self.include_subdomains and _canonical_ip(host):
            raise ScopeRuleError(
                f"IP hosts have no subdomains to include: {self.host!r}"
            )
        excludes = tuple(_validate_path(prefix, "exclusion") for prefix in self.exclude)
        for prefix in excludes:
            if not _path_under(prefix, path) or prefix == path:
                raise ScopeRuleError(
                    f"exclusion {prefix!r} is not a subtree of rule path {path!r}"
                )
        object.__setattr__(self, "exclude", excludes)

    @classmethod
    def from_url(cls, url: str, **kwargs) -> "ScopeRule":
        """Build a rule from ``[scheme://]host[:port][/path]``, e.g.
        ``"example.org:8080/news"``. Extra keyword arguments (``exclude``,
        ``include_subdomains``) are passed to the constructor."""
        if not isinstance(url, str) or not url.strip():
            raise ScopeRuleError(f"cannot parse scope rule from {url!r}")
        candidate = url.strip()
        if "://" not in candidate:
            candidate = "http://" + candidate
        try:
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError as exc:
            raise ScopeRuleError(f"invalid scope rule {url!r}: {exc}") from exc
        if parsed.query or parsed.fragment:
            raise ScopeRuleError(
                f"scope rule must not contain a query or fragment: {url!r}"
            )
        if parsed.username or parsed.password:
            raise ScopeRuleError(f"scope rule must not contain userinfo: {url!r}")
        return cls(
            host=parsed.hostname or "",
            port=port,
            path=parsed.path or "/",
            **kwargs,
        )

    def _host_matches(self, host: str) -> bool:
        if self.include_subdomains:
            return host == self.host or host.endswith("." + self.host)
        return host == self.host

    def _matches(self, host: str, port: int, path: str) -> bool:
        return (
            self._host_matches(host)
            and (self.port is None or self.port == port)
            and _path_under(path, self.path)
            and not any(_path_under(path, prefix) for prefix in self.exclude)
        )


class ScopeMatch(NamedTuple):
    "Result of a successful scope check: the rule that matched and the normalized URL."

    rule: ScopeRule
    url: str


def _specificity(rule: ScopeRule) -> tuple[int, int, int]:
    "Most specific rules win: deeper host, deeper path, explicit port."
    return (rule.host.count("."), len(rule.path), int(rule.port is not None))


class Scope:
    """An ordered set of scope rules checked against candidate URLs.

    Rules are evaluated most specific first, so a narrow rule
    (``example.org/news``) takes precedence over a broad one
    (``example.org``) regardless of the order they were given in.
    """

    def __init__(self, rules) -> None:
        rules = tuple(rules)
        if not rules:
            raise ScopeRuleError("a scope needs at least one rule")
        for rule in rules:
            if not isinstance(rule, ScopeRule):
                raise ScopeRuleError(f"not a ScopeRule: {rule!r}")
        self.rules: tuple[ScopeRule, ...] = tuple(
            sorted(rules, key=_specificity, reverse=True)
        )

    def __iter__(self) -> Iterator[ScopeRule]:
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)

    def __contains__(self, url: str) -> bool:
        return self.match(url) is not None

    def match(self, url: str) -> ScopeMatch | None:
        """Return the matching rule and the normalized URL, or None if the
        URL is out of scope (or not a valid http(s) URL at all)."""
        parts = self._parts(url)
        if parts is None:
            return None
        host, port, path, normalized = parts
        for rule in self.rules:
            if rule._matches(host, port, path):
                return ScopeMatch(rule, normalized)
        return None

    @staticmethod
    def _parts(url: str) -> tuple[str, int, str, str] | None:
        "Split a URL into canonical (host, port, path, normalized URL), or None."
        if not isinstance(url, str):
            return None
        try:
            parsed = urlsplit(url.strip())
            port = parsed.port  # raises on malformed or out-of-range ports
        except ValueError:
            return None
        scheme = parsed.scheme.lower()
        if scheme not in _SCHEMES or not parsed.hostname:
            return None
        host = _normalize_host(parsed.hostname)
        effective_port = port or DEFAULT_PORTS[scheme]
        path = _normalize_path(parsed.path)
        # normalized address: canonical host (bracketed IPv6), non-default
        # port only, decoded path, query kept, fragment and userinfo dropped
        display_host = f"[{host}]" if ":" in host else host
        if port and port != DEFAULT_PORTS[scheme]:
            display_host = f"{display_host}:{port}"
        normalized = urlunsplit((scheme, display_host, path, parsed.query, ""))
        return host, effective_port, path, normalized
