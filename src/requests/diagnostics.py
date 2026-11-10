"""Read-only diagnostics explaining how a proxy is chosen for a request.

The proxy selected for a request depends on four inputs, evaluated in a
specific order:

1. the ``proxies`` mapping passed explicitly to the request,
2. the ``Session.proxies`` mapping,
3. proxy environment variables (``HTTPS_PROXY`` and friends),
4. ``NO_PROXY`` / the platform ``proxy_bypass`` hook.

Re-implementing that resolution for debugging purposes risks drifting away
from what the transport actually does. Every value in this module is
therefore produced by the same helpers used on the send path
(:func:`requests.utils.get_environ_proxies`,
:func:`requests.sessions.merge_setting`,
:func:`requests.utils.resolve_proxies` and
:func:`requests.utils.select_proxy`). The diagnostics only record where each
value came from, why ``NO_PROXY`` matched and redact credentials, so a report
can be shared without leaking passwords.

Nothing here performs network I/O, mutates the environment or alters the
:class:`~requests.Session` passed in.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .compat import getproxies, getproxies_environment
from .sessions import merge_setting
from .utils import (
    NoProxyEvaluation,
    _evaluate_no_proxy,
    get_environ_proxies,
    resolve_proxies,
    select_proxy,
)

if TYPE_CHECKING:
    from .sessions import Session


def _redact_proxy_url(proxy_url: str | None) -> str | None:
    """Return a copy of ``proxy_url`` with userinfo and URL path stripped.

    Only the scheme, host and port remain, which is enough to identify which
    proxy was selected without revealing credentials. IPv6 literals keep their
    brackets.
    """
    if not proxy_url:
        return proxy_url

    # A bare "host:port" entry would make urlsplit() treat the host as a
    # scheme; supply a temporary scheme for parsing, mirroring what the
    # adapter does later via prepend_scheme_if_needed().
    candidate = proxy_url
    had_scheme = "://" in candidate
    if not had_scheme:
        candidate = "http://" + candidate

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return "<malformed proxy url>"

    if not parsed.hostname or " " in parsed.netloc:
        return "<malformed proxy url>"

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "<malformed proxy url>"

    userinfo = ""
    if parsed.username is not None:
        userinfo = "***:***@" if parsed.password is not None else "***@"

    scheme = f"{parsed.scheme}://" if had_scheme and parsed.scheme else ""
    return f"{scheme}{userinfo}{host}{port}"


def _has_credentials(proxy_url: str | None) -> bool:
    if not proxy_url:
        return False
    candidate = proxy_url if "://" in proxy_url else "http://" + proxy_url
    try:
        parsed = urlsplit(candidate)
        return parsed.username is not None
    except ValueError:
        return False


def _classify_host(hostname: str | None) -> str:
    if hostname is None:
        return "no-hostname"
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return "hostname"
    return "ipv6" if isinstance(ip, ipaddress.IPv6Address) else "ipv4"


def _env_var_name(prefix: str) -> str | None:
    """Name of the environment variable actually supplying ``prefix`` proxy.

    Mirrors the lowercase-first lookup performed by
    :func:`urllib.request.getproxies_environment`.
    """
    lowercase = f"{prefix}_proxy"
    uppercase = f"{prefix.upper()}_PROXY"
    if os.environ.get(lowercase):
        return lowercase
    if os.environ.get(uppercase):
        return uppercase
    return None


@dataclass(frozen=True)
class ProxyEntry:
    """A single proxy mapping together with its provenance."""

    key: str
    location: str
    redacted_proxy: str | None
    has_credentials: bool


@dataclass(frozen=True)
class ProxyRouteDiagnosis:
    """The full, redacted explanation of how a proxy would be selected."""

    url: str
    scheme: str
    hostname: str | None
    host_type: str
    port: int | None
    trust_env: bool
    explicit_proxies: tuple[ProxyEntry, ...]
    session_proxies: tuple[ProxyEntry, ...]
    environment_proxies: tuple[ProxyEntry, ...]
    no_proxy: NoProxyEvaluation
    effective_proxies: tuple[ProxyEntry, ...]
    selected_key: str | None
    selected_proxy: str | None
    bypass: bool

    @property
    def selected_proxy_redacted(self) -> str | None:
        return _redact_proxy_url(self.selected_proxy)

    def to_dict(self) -> dict[str, object]:
        """JSON-serialisable, redacted view of the diagnosis."""

        def entry(entry: ProxyEntry) -> dict[str, object]:
            return {
                "key": entry.key,
                "location": entry.location,
                "proxy": entry.redacted_proxy,
                "has_credentials": entry.has_credentials,
            }

        return {
            "url": self.url,
            "scheme": self.scheme,
            "hostname": self.hostname,
            "host_type": self.host_type,
            "port": self.port,
            "trust_env": self.trust_env,
            "explicit_proxies": [entry(item) for item in self.explicit_proxies],
            "session_proxies": [entry(item) for item in self.session_proxies],
            "environment_proxies": [
                entry(item) for item in self.environment_proxies
            ],
            "no_proxy": {
                "value_present": self.no_proxy.no_proxy is not None,
                "source": self.no_proxy.no_proxy_source,
                "rules": list(self.no_proxy.rules),
                "matched_rule": self.no_proxy.matched_rule,
                "match_reason": (
                    self.no_proxy.match.reason if self.no_proxy.match else None
                ),
                "system_bypass": self.no_proxy.system_bypass,
                "bypass": self.no_proxy.bypass,
            },
            "effective_proxies": [entry(item) for item in self.effective_proxies],
            "selected_key": self.selected_key,
            "selected_proxy": self.selected_proxy_redacted,
            "bypass": self.bypass,
        }

    def format_report(self) -> str:
        """Render a compact human-readable report with no credentials."""
        lines = [f"Proxy route diagnosis for {self.url}"]
        host_description = self.hostname or "<no hostname>"
        if self.port is not None:
            host_description += f":{self.port}"
        lines.append(f"  target: {self.scheme}://{host_description} ({self.host_type})")
        lines.append(f"  trust_env: {self.trust_env}")

        lines.append("  explicit request proxies:")
        lines.extend(_format_entries(self.explicit_proxies))
        lines.append("  session proxies:")
        lines.extend(_format_entries(self.session_proxies))
        lines.append("  environment proxies:")
        lines.extend(_format_entries(self.environment_proxies))

        no_proxy = self.no_proxy
        if no_proxy.no_proxy is None:
            lines.append("  NO_PROXY: not set")
        else:
            source = no_proxy.no_proxy_source or "environment"
            lines.append(
                f"  NO_PROXY ({source}): {', '.join(no_proxy.rules) or '<empty>'}"
            )
        if no_proxy.match is not None:
            lines.append(
                f"    matched rule {no_proxy.match.rule!r}: {no_proxy.match.reason}"
            )
        if no_proxy.system_bypass:
            lines.append("    platform proxy_bypass() also voted to bypass")

        lines.append("  proxies handed to the adapter:")
        lines.extend(_format_entries(self.effective_proxies))

        if self.selected_proxy is None:
            lines.append("  result: direct connection (no proxy selected)")
        else:
            lines.append(
                f"  result: proxy key {self.selected_key!r} -> "
                f"{self.selected_proxy_redacted}"
            )
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.format_report()


def _format_entries(entries: tuple[ProxyEntry, ...]) -> list[str]:
    if not entries:
        return ["    (none)"]
    return [
        f"    {item.key} <- {item.location}: {item.redacted_proxy or '(empty)'}"
        for item in entries
    ]


def _entries_from_mapping(
    proxies: dict[str, str], location: str
) -> tuple[ProxyEntry, ...]:
    entries: list[ProxyEntry] = []
    for key, value in proxies.items():
        if key in ("no_proxy", "no"):
            # NO_PROXY (urllib also normalises it to a "no" key) is a rule
            # list rather than a proxy URL, so it never carries credentials
            # and must not be parsed as one.
            entries.append(
                ProxyEntry(
                    key=key,
                    location=location,
                    redacted_proxy=value,
                    has_credentials=False,
                )
            )
            continue
        entries.append(
            ProxyEntry(
                key=key,
                location=location,
                redacted_proxy=_redact_proxy_url(value),
                has_credentials=_has_credentials(value),
            )
        )
    return tuple(entries)


def _environment_entries(
    url: str, no_proxy: str | None, trust_env: bool
) -> tuple[ProxyEntry, ...]:
    """Collect environment proxies using the real send-path helpers.

    Returns redacted entries for the mapping that the real code would use
    (empty when ``trust_env`` is off or NO_PROXY bypasses).
    """
    if not trust_env:
        return ()

    # Exact call made by Session.merge_environment_settings(); returns {}
    # when should_bypass_proxies() is True.
    raw_environ = get_environ_proxies(url, no_proxy=no_proxy)
    if not raw_environ:
        return ()

    # Values come from getproxies(); the environment scan only labels where
    # they originated, so the reported value can never differ from reality.
    labelled = getproxies_environment()
    entries: list[ProxyEntry] = []
    for key, value in getproxies().items():
        if key not in raw_environ:
            continue
        if key == "no":
            # urllib normalises NO_PROXY to a "no" key; it is a rule list,
            # not a proxy URL, and the dedicated NO_PROXY section covers it.
            continue
        if key in labelled and (var_name := _env_var_name(key)):
            location = f"environment:{var_name}"
        else:
            location = "system proxy configuration"
        entries.append(
            ProxyEntry(
                key=key,
                location=location,
                redacted_proxy=_redact_proxy_url(value),
                has_credentials=_has_credentials(value),
            )
        )
    return tuple(entries)


def _build_effective_proxies(
    url: str,
    explicit: dict[str, str],
    session_proxies: dict[str, str],
    trust_env: bool,
) -> dict[str, str]:
    """Reproduce the exact proxy mapping that reaches the adapter.

    Follows :meth:`Session.merge_environment_settings` and the
    :func:`resolve_proxies` default applied in :meth:`Session.send`, reusing
    those functions directly instead of copying their rules.
    """
    request_proxies = dict(explicit)
    if trust_env:
        env_proxies = get_environ_proxies(
            url, no_proxy=request_proxies.get("no_proxy")
        )
        for key, value in env_proxies.items():
            request_proxies.setdefault(key, value)

    merged = merge_setting(request_proxies, session_proxies)
    # resolve_proxies() only reads .url; a SimpleNamespace keeps the
    # diagnostic free of Request preparation side effects.
    return resolve_proxies(
        SimpleNamespace(url=url), merged, trust_env=trust_env
    )


def diagnose_proxy_route(
    url: str,
    proxies: dict[str, str] | None = None,
    session: Session | None = None,
    trust_env: bool | None = None,
) -> ProxyRouteDiagnosis:
    """Explain, without sending anything, which proxy would serve ``url``.

    :param url: URL the request would be sent to.
    :param proxies: the ``proxies`` mapping that would be passed explicitly
        to the request (e.g. ``session.get(..., proxies=...)``).
    :param session: optional :class:`~requests.Session` whose ``proxies`` and
        ``trust_env`` settings should be taken into account.
    :param trust_env: override the ``trust_env`` flag. When ``session`` is
        given its own flag is used unless this argument is provided.
    :rtype: ProxyRouteDiagnosis
    """
    explicit = dict(proxies or {})
    session_proxies = dict(getattr(session, "proxies", None) or {})

    if trust_env is None:
        trust_env = bool(getattr(session, "trust_env", True))

    parsed = urlsplit(url)
    scheme = parsed.scheme
    hostname = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    host_type = _classify_host(hostname)

    merged_inputs = dict(session_proxies)
    merged_inputs.update(explicit)
    no_proxy_override = merged_inputs.get("no_proxy")

    # Shared with should_bypass_proxies(): the bypass verdict and the hit
    # reason are produced by the matching code used on the send path.
    no_proxy = _evaluate_no_proxy(url, no_proxy_override)

    environment_entries = _environment_entries(
        url, no_proxy_override, trust_env
    )

    effective = _build_effective_proxies(
        url, explicit, session_proxies, trust_env
    )

    # The exact call the HTTPAdapter performs to pick the connection pool.
    selected_proxy = select_proxy(url, effective)
    selected_key = _selected_key(url, effective, selected_proxy)

    return ProxyRouteDiagnosis(
        url=url,
        scheme=scheme,
        hostname=hostname,
        host_type=host_type,
        port=port,
        trust_env=trust_env,
        explicit_proxies=_entries_from_mapping(
            explicit, "explicit argument"
        ),
        session_proxies=_entries_from_mapping(session_proxies, "session"),
        environment_proxies=environment_entries,
        no_proxy=no_proxy,
        effective_proxies=_entries_from_mapping(
            effective, "resolved for adapter"
        ),
        selected_key=selected_key,
        selected_proxy=selected_proxy,
        bypass=no_proxy.bypass,
    )


def _selected_key(
    url: str, proxies: dict[str, str], selected_proxy: str | None
) -> str | None:
    """Identify which key select_proxy() matched, in its documented order."""
    if selected_proxy is None:
        return None
    urlparts = urlsplit(url)
    if urlparts.hostname is None:
        return urlparts.scheme if urlparts.scheme in proxies else "all"
    proxy_keys = [
        urlparts.scheme + "://" + urlparts.hostname,
        urlparts.scheme,
        "all://" + urlparts.hostname,
        "all",
    ]
    for key in proxy_keys:
        if key in proxies and proxies[key] == selected_proxy:
            return key
    return None
