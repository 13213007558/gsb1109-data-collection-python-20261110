"""Tests for the read-only proxy route diagnostics.

The key contract tested here is that the diagnosis never re-implements proxy
selection: every resolved mapping is compared against the exact ``proxies``
dictionary that a recording transport adapter receives on the send path, and
the chosen proxy is compared against ``select_proxy`` on that dictionary.
"""

from __future__ import annotations

import json

import pytest

from requests import Session, diagnose_proxy_route
from requests.adapters import BaseAdapter
from requests.diagnostics import (
    ProxyRouteDiagnosis,
    _entries_from_mapping,
    _redact_proxy_url,
)
from requests.utils import select_proxy, should_bypass_proxies

PROXY_ENV_VARS = (
    "http_proxy",
    "https_proxy",
    "ftp_proxy",
    "all_proxy",
    "no_proxy",
)


class _AdapterCaptured(Exception):
    """Raised by the recording adapter to expose the received kwargs."""

    def __init__(self, proxies):
        super().__init__("captured adapter call")
        self.proxies = proxies


class RecordingAdapter(BaseAdapter):
    """Captures the proxies mapping handed to the transport."""

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        raise _AdapterCaptured(proxies)

    def close(self):
        pass


@pytest.fixture
def isolated_proxy_env(monkeypatch):
    """Clear every proxy variable and pin getproxies() to the environment."""
    for var in PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.upper(), raising=False)

    def env_only():
        from urllib.request import getproxies_environment

        return getproxies_environment()

    monkeypatch.setattr("requests.utils.getproxies", env_only)
    monkeypatch.setattr("requests.diagnostics.getproxies", env_only)
    # Keep the platform bypass hook deterministic; individual tests can opt in.
    monkeypatch.setattr("requests.utils.proxy_bypass", lambda host: False)
    return monkeypatch


def _capture_adapter_proxies(session, url, proxies=None):
    """Run a request far enough to record the adapter's proxies argument."""
    session.mount("http://", RecordingAdapter())
    session.mount("https://", RecordingAdapter())
    try:
        session.get(url, proxies=proxies)
    except _AdapterCaptured as captured:
        return captured.proxies
    raise AssertionError("recording adapter did not intercept the request")


def _redact_mapping(mapping):
    # Use the production redaction so NO_PROXY-style keys are treated the
    # same on both sides of the comparison.
    return {
        entry.key: entry.redacted_proxy
        for entry in _entries_from_mapping(mapping, "adapter")
    }


def test_redaction_strips_userinfo_but_keeps_host_and_port():
    assert _redact_proxy_url("http://user:secret@proxy.example:3128") == (
        "http://***:***@proxy.example:3128"
    )
    assert _redact_proxy_url("http://user@proxy.example:3128") == (
        "http://***@proxy.example:3128"
    )
    assert _redact_proxy_url("proxy.example:3128") == "proxy.example:3128"
    assert _redact_proxy_url("socks5://[2001:db8::1]:1080") == (
        "socks5://[2001:db8::1]:1080"
    )
    assert _redact_proxy_url("http://u:p@[::1]:8080") == "http://***:***@[::1]:8080"
    assert _redact_proxy_url("not a url at all") == "<malformed proxy url>"
    assert _redact_proxy_url(None) is None


def test_report_and_dict_never_contain_credentials(isolated_proxy_env):
    secret = "super-secret-password"
    session = Session()
    session.proxies = {"https": f"http://suser:{secret}@session-proxy:3128"}
    diagnosis = session.diagnose_proxy_route(
        "https://example.com/path",
        proxies={"http": f"http://euser:{secret}@explicit-proxy:8080"},
    )

    rendered = diagnosis.format_report() + json.dumps(diagnosis.to_dict()) + str(diagnosis)
    assert secret not in rendered
    assert "session-proxy:3128" in rendered
    assert "explicit-proxy:8080" in rendered
    json.loads(json.dumps(diagnosis.to_dict()))


def test_host_classification_for_name_ipv4_ipv6_and_port(isolated_proxy_env):
    cases = {
        "http://example.com:8080/x": ("hostname", 8080),
        "http://10.20.30.40:5000/": ("ipv4", 5000),
        "http://[2001:db8::dead:beef]:8080/": ("ipv6", 8080),
    }
    for url, (host_type, port) in cases.items():
        diagnosis = diagnose_proxy_route(url)
        assert diagnosis.host_type == host_type
        assert diagnosis.port == port
        if port is not None:
            assert f":{port}" in diagnosis.format_report()


def test_diagnosis_matches_adapter_mapping_with_env_proxy(isolated_proxy_env):
    isolated_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy.example:3129")
    session = Session()
    session.proxies = {"http": "http://session-proxy.example:3128"}
    url = "https://example.com/resource"
    explicit = {"https": "http://explicit-proxy.example:8080"}

    diagnosis = session.diagnose_proxy_route(url, proxies=explicit)
    adapter_proxies = _capture_adapter_proxies(session, url, explicit)

    # The mapping reported as reaching the adapter must be the real mapping,
    # compared after redaction so secrets never need to appear in assertions.
    assert [entry.key for entry in diagnosis.effective_proxies] == list(
        adapter_proxies
    )
    assert {entry.key: entry.redacted_proxy for entry in diagnosis.effective_proxies} == (
        _redact_mapping(adapter_proxies)
    )
    # The selected proxy must match select_proxy() on the captured mapping.
    assert diagnosis.selected_proxy == select_proxy(url, adapter_proxies)
    assert diagnosis.selected_key == "https"
    assert diagnosis.selected_proxy_redacted == "http://explicit-proxy.example:8080"


def test_environment_proxy_used_when_no_explicit_or_session_value(isolated_proxy_env):
    isolated_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy.example:3129")
    session = Session()
    url = "https://example.com/resource"

    diagnosis = session.diagnose_proxy_route(url)
    adapter_proxies = _capture_adapter_proxies(session, url)

    assert diagnosis.selected_proxy == select_proxy(url, adapter_proxies)
    assert diagnosis.selected_proxy == "http://env-proxy.example:3129"
    env_entry = next(
        entry for entry in diagnosis.environment_proxies if entry.key == "https"
    )
    assert env_entry.location == "environment:HTTPS_PROXY"
    assert diagnosis.selected_key == "https"


def test_environment_proxy_overrides_session_proxy_for_same_key(isolated_proxy_env):
    # Non-obvious real behaviour: merge_environment_settings() fills the
    # request dict from the environment before merge_setting(), so an
    # environment proxy wins over a session proxy defined for the same key.
    isolated_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy:3129")
    session = Session()
    session.proxies = {"https": "http://session-proxy:3128"}
    url = "https://example.com/resource"

    diagnosis = session.diagnose_proxy_route(url)
    adapter_proxies = _capture_adapter_proxies(session, url)

    assert adapter_proxies["https"] == "http://env-proxy:3129"
    assert diagnosis.selected_proxy == select_proxy(url, adapter_proxies)
    assert diagnosis.selected_proxy == "http://env-proxy:3129"
    # The session value is still reported separately as an input.
    assert {entry.key for entry in diagnosis.session_proxies} == {"https"}


def test_lowercase_env_var_takes_precedence(isolated_proxy_env):
    isolated_proxy_env.setenv("https_proxy", "http://lower-proxy:1")
    isolated_proxy_env.setenv("HTTPS_PROXY", "http://upper-proxy:2")

    diagnosis = diagnose_proxy_route("https://example.com")
    assert diagnosis.selected_proxy == "http://lower-proxy:1"
    env_entry = next(
        entry for entry in diagnosis.environment_proxies if entry.key == "https"
    )
    assert env_entry.location == "environment:https_proxy"


def test_trust_env_false_ignores_environment(isolated_proxy_env):
    isolated_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy:3129")
    session = Session()
    session.trust_env = False

    diagnosis = session.diagnose_proxy_route("https://example.com")
    assert diagnosis.environment_proxies == ()
    assert diagnosis.selected_proxy is None
    assert diagnosis.bypass is False

    adapter_proxies = _capture_adapter_proxies(session, "https://example.com")
    assert adapter_proxies == {}


@pytest.mark.parametrize(
    "url,no_proxy_rule",
    (
        ("http://service.example.com/path", "example.com"),
        ("http://service.example.com/path", ".example.com"),
        ("http://example.com:8080/path", "example.com:8080"),
        ("http://10.20.30.40/x", "10.20.30.40"),
        ("http://10.20.30.40/x", "10.20.30.0/24"),
        ("http://192.168.1.55/x", "192.168.1.0/24"),
    ),
)
def test_no_proxy_hit_matches_real_bypass_logic(isolated_proxy_env, url, no_proxy_rule):
    diagnosis = diagnose_proxy_route(url, proxies={"no_proxy": no_proxy_rule})

    assert diagnosis.bypass is True
    assert diagnosis.no_proxy.bypass is True
    assert diagnosis.no_proxy.matched_rule == no_proxy_rule
    assert diagnosis.no_proxy.match is not None and diagnosis.no_proxy.match.reason
    # Same verdict as the helper actually used by get_environ_proxies().
    assert should_bypass_proxies(url, no_proxy_rule) is True
    # A bypass strips environment proxies from the adapter mapping.
    assert diagnosis.environment_proxies == ()
    assert diagnosis.selected_proxy is None


@pytest.mark.parametrize(
    "url,no_proxy_rule",
    (
        ("http://other.example.org/path", "example.com"),
        ("http://10.20.40.40/x", "10.20.30.0/24"),
        ("http://192.168.2.55/x", "192.168.1.0/24"),
        ("http://notexample.com/path", "example.com"),
    ),
)
def test_no_proxy_miss_matches_real_bypass_logic(isolated_proxy_env, url, no_proxy_rule):
    diagnosis = diagnose_proxy_route(url, proxies={"no_proxy": no_proxy_rule})

    assert diagnosis.bypass is False
    assert diagnosis.no_proxy.matched_rule is None
    assert should_bypass_proxies(url, no_proxy_rule) is False


def test_no_proxy_cidr_reason_uses_real_network_math(isolated_proxy_env):
    diagnosis = diagnose_proxy_route(
        "http://172.16.5.9/", proxies={"no_proxy": "172.16.0.0/12"}
    )
    assert diagnosis.no_proxy.match.rule == "172.16.0.0/12"
    assert "CIDR" in diagnosis.no_proxy.match.reason


def test_ipv6_hostname_is_matched_as_domain_suffix(isolated_proxy_env):
    url = "http://[2001:db8::beef]:8080/path"
    diagnosis = diagnose_proxy_route(url, proxies={"no_proxy": "2001:db8::beef:8080"})
    assert diagnosis.host_type == "ipv6"
    assert diagnosis.bypass == should_bypass_proxies(url, "2001:db8::beef:8080")
    assert diagnosis.bypass is True


def test_no_proxy_from_environment_is_recorded(isolated_proxy_env):
    isolated_proxy_env.setenv("no_proxy", "internal.example,10.0.0.0/8")
    diagnosis = diagnose_proxy_route("http://api.internal.example/v1")

    assert diagnosis.no_proxy.no_proxy_source == "no_proxy"
    assert diagnosis.no_proxy.rules == ("internal.example", "10.0.0.0/8")
    assert diagnosis.no_proxy.matched_rule == "internal.example"
    assert diagnosis.bypass is True


def test_environment_no_proxy_key_is_not_listed_as_proxy(isolated_proxy_env):
    # urllib normalises NO_PROXY to a "no" key inside getproxies(); it must
    # never appear as a redacted proxy URL in the environment section.
    isolated_proxy_env.setenv("NO_PROXY", "internal.example")
    isolated_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy:3129")

    diagnosis = diagnose_proxy_route("https://api.external.example/v1")
    assert [entry.key for entry in diagnosis.environment_proxies] == ["https"]
    assert "no" not in [entry.key for entry in diagnosis.environment_proxies]
    assert diagnosis.no_proxy.rules == ("internal.example",)


def test_no_proxy_mapping_entries_are_not_parsed_as_urls(isolated_proxy_env):
    diagnosis = diagnose_proxy_route(
        "http://10.0.0.5/", proxies={"no_proxy": "10.0.0.0/8"}
    )
    no_proxy_entry = next(
        entry for entry in diagnosis.effective_proxies if entry.key == "no_proxy"
    )
    assert no_proxy_entry.redacted_proxy == "10.0.0.0/8"
    assert no_proxy_entry.has_credentials is False


def test_platform_proxy_bypass_vote_is_reported(isolated_proxy_env):
    isolated_proxy_env.setattr("requests.utils.proxy_bypass", lambda host: True)
    diagnosis = diagnose_proxy_route("http://platform-local/x")

    assert diagnosis.no_proxy.system_bypass is True
    assert diagnosis.no_proxy.system_bypass_source == "proxy_bypass"
    assert diagnosis.bypass is True
    assert "platform proxy_bypass() also voted to bypass" in diagnosis.format_report()
    assert should_bypass_proxies("http://platform-local/x", None) is True


def test_no_proxy_bypass_strips_env_proxy_at_adapter(isolated_proxy_env):
    isolated_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy:3129")
    session = Session()
    url = "https://internal.example/x"
    explicit = {"no_proxy": "internal.example"}

    diagnosis = session.diagnose_proxy_route(url, proxies=explicit)
    adapter_proxies = _capture_adapter_proxies(session, url, explicit)

    assert diagnosis.bypass is True
    assert select_proxy(url, adapter_proxies) is None
    assert diagnosis.selected_proxy is None
    assert _redact_mapping(adapter_proxies) == {
        entry.key: entry.redacted_proxy for entry in diagnosis.effective_proxies
    }


def test_scheme_and_host_specific_proxy_key_selection(isolated_proxy_env):
    proxies = {
        "https://example.com": "http://host-specific:9000",
        "https": "http://scheme-only:9001",
        "all": "http://catch-all:9002",
    }
    diagnosis = diagnose_proxy_route("https://example.com", proxies=proxies)

    assert diagnosis.selected_key == "https://example.com"
    assert diagnosis.selected_proxy == "http://host-specific:9000"

    fallback = diagnose_proxy_route("https://other.example", proxies=proxies)
    assert fallback.selected_key == "https"
    assert fallback.selected_proxy == "http://scheme-only:9001"


def test_diagnosis_is_read_only_and_performs_no_io(isolated_proxy_env):
    isolated_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy:3129")
    session = Session()
    session.mount("https://", RecordingAdapter())
    session.proxies = {"https": "http://session-proxy:3128"}
    before = dict(session.proxies)

    diagnosis = session.diagnose_proxy_route(
        "https://example.com", proxies={"no_proxy": "example.com"}
    )

    assert isinstance(diagnosis, ProxyRouteDiagnosis)
    assert session.proxies == before
    assert dict(session.adapters)  # adapter never replaced or invoked


def test_module_level_function_equivalent_to_session_method(isolated_proxy_env):
    session = Session()
    session.proxies = {"https": "http://session-proxy:3128"}
    kwargs = {"proxies": {"http": "http://explicit:8080"}}

    via_session = session.diagnose_proxy_route("https://example.com", **kwargs)
    via_function = diagnose_proxy_route(
        "https://example.com", session=session, **kwargs
    )
    assert via_function.to_dict() == via_session.to_dict()


def test_url_without_hostname_reports_bypass(isolated_proxy_env):
    diagnosis = diagnose_proxy_route("file:///etc/hosts")
    assert diagnosis.host_type == "no-hostname"
    assert diagnosis.hostname is None
    assert diagnosis.bypass is True
    assert diagnosis.selected_proxy is None
    assert should_bypass_proxies("file:///etc/hosts", None) is True


def test_direct_connection_is_rendered_in_report(isolated_proxy_env):
    diagnosis = diagnose_proxy_route("http://plain.example/")
    report = diagnosis.format_report()
    assert "NO_PROXY: not set" in report
    assert "result: direct connection (no proxy selected)" in report
    assert str(diagnosis) == report


def test_malformed_proxy_and_bad_port_do_not_raise(isolated_proxy_env):
    diagnosis = diagnose_proxy_route(
        "http://example.com/",
        proxies={"http": "http://u:p@[2001:db8::beef]:notaport"},
    )
    entry = next(
        entry for entry in diagnosis.explicit_proxies if entry.key == "http"
    )
    assert entry.redacted_proxy == "<malformed proxy url>"

    target = diagnose_proxy_route("http://[2001:db8::beef]:notaport/")
    assert target.host_type == "ipv6"
    assert target.port is None


def test_system_proxy_location_is_labelled(isolated_proxy_env):
    def fake_getproxies():
        return {"https": "http://system-configured:3128"}

    # get_environ_proxies() in utils must see the same system mapping that
    # the diagnostics module labels.
    isolated_proxy_env.setattr("requests.utils.getproxies", fake_getproxies)
    isolated_proxy_env.setattr(
        "requests.diagnostics.getproxies", fake_getproxies
    )
    isolated_proxy_env.setattr(
        "requests.diagnostics.getproxies_environment", lambda: {}
    )
    diagnosis = diagnose_proxy_route("https://example.com/")
    env_entry = next(
        entry for entry in diagnosis.environment_proxies if entry.key == "https"
    )
    assert env_entry.location == "system proxy configuration"
    assert env_entry.redacted_proxy == "http://system-configured:3128"


def test_no_hostname_proxy_key_selection(isolated_proxy_env):
    diagnosis = diagnose_proxy_route(
        "file:///x", proxies={"all": "http://catch-all:9002"}
    )
    assert diagnosis.selected_key == "all"
    assert diagnosis.selected_proxy == "http://catch-all:9002"


def test_no_proxy_match_branch_is_rendered_in_report(isolated_proxy_env):
    diagnosis = diagnose_proxy_route(
        "http://10.20.30.40/", proxies={"no_proxy": "10.20.30.0/24"}
    )
    report = diagnosis.format_report()
    assert "NO_PROXY (argument)" in report
    assert "matched rule '10.20.30.0/24'" in report
