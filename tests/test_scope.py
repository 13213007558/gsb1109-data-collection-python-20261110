"""
Unit tests for crawl scope rules (courlan.scope).
"""

import pytest

from courlan import Scope, ScopeMatch, ScopeRule, ScopeRuleError


def test_exact_host():
    scope = Scope([ScopeRule("example.org")])
    assert "https://example.org/" in scope
    assert "https://example.org/news/article-1" in scope
    assert "https://other.org/" not in scope
    # a bare host is not a wildcard: subdomains are out unless stated
    assert "https://news.example.org/" not in scope


def test_lookalike_hosts_stay_out_of_scope():
    "Addresses that merely contain the target host string must not match."
    scope = Scope([ScopeRule("example.org", path="/news")])
    lookalikes = [
        "https://example.org.evil.com/news",  # target as a subdomain label
        "https://example.org@evil.com/news",  # target as userinfo
        "https://user:example.org@evil.com/news",  # target as password
        "https://evil.com/example.org/news",  # target inside the path
        "https://evil.com/news?u=https://example.org/news",  # target in query
        "https://evil.com/news#https://example.org",  # target in fragment
        "https://example-org.com/news",  # hyphenated lookalike
        "https://example.org:bad/news",  # malformed port, not a valid URL
        "ftp://example.org/news",  # not an http(s) URL
        "example.org/news",  # no scheme
    ]
    for url in lookalikes:
        assert url not in scope, url
        assert scope.match(url) is None


def test_explicit_subdomains():
    scope = Scope(
        [
            ScopeRule("example.org"),
            ScopeRule("news.example.org"),
        ]
    )
    assert "https://news.example.org/article" in scope
    assert "https://blog.example.org/" not in scope
    # opt-in subdomain coverage
    scope = Scope([ScopeRule("example.org", include_subdomains=True)])
    assert "https://example.org/" in scope
    assert "https://news.example.org/" in scope
    assert "https://deep.news.example.org/" in scope
    assert "https://example.org.evil.com/" not in scope


def test_port_rules():
    scope = Scope([ScopeRule("example.org", port=8080)])
    assert "http://example.org:8080/news" in scope
    assert "http://example.org/news" not in scope  # effective port 80
    assert "http://example.org:80/news" not in scope
    # default ports are normalized away before comparison
    scope = Scope([ScopeRule("example.org", port=443)])
    assert "https://example.org/news" in scope
    assert "https://example.org:443/news" in scope
    assert "https://example.org:8443/news" not in scope
    # no port in the rule: any port is in scope
    scope = Scope([ScopeRule("example.org")])
    assert "http://example.org:8080/news" in scope


def test_path_segment_prefix():
    scope = Scope([ScopeRule("example.org", path="/news")])
    assert "https://example.org/news" in scope
    assert "https://example.org/news/" in scope
    assert "https://example.org/news/politics/a-1" in scope
    # segment boundaries: a string prefix is not enough
    assert "https://example.org/newsletter" not in scope
    assert "https://example.org/new" not in scope
    assert "https://example.org/" not in scope
    # root prefix covers the whole host
    assert "https://example.org/anything" in Scope([ScopeRule("example.org")])


def test_query_and_fragment_do_not_change_attribution():
    scope = Scope([ScopeRule("example.org", path="/news")])
    assert "https://example.org/news?utm_source=x&page=2" in scope
    assert "https://example.org/news#comments" in scope
    assert "https://example.org/newsletter?next=/news" not in scope
    assert "https://example.org/?redirect=/news" not in scope


def test_excluded_subtrees():
    rule = ScopeRule("example.org", path="/news", exclude=("/news/archive",))
    scope = Scope([rule])
    assert "https://example.org/news/politics" in scope
    assert "https://example.org/news/archive" not in scope
    assert "https://example.org/news/archive/2020" not in scope
    # exclusion prefixes are segment prefixes too
    assert "https://example.org/news/archivement" in scope


def test_idn_normalization():
    "Unicode and punycode spellings of a rule host are interchangeable."
    rule = ScopeRule("kräuter.de")
    assert rule.host == "xn--kruter-cua.de"
    scope = Scope([rule])
    assert "https://kräuter.de/新闻" in scope
    assert "https://xn--kruter-cua.de/新闻" in scope
    # and the other way round: punycode rule, unicode URL
    assert "https://kräuter.de/" in Scope([ScopeRule("xn--kruter-cua.de")])
    # a lookalike punycode host is still another host
    assert "https://xn--kruter-cua.de.evil.com/" not in scope


def test_trailing_dot_and_case():
    scope = Scope([ScopeRule("Example.ORG.")])
    match = scope.match("https://EXAMPLE.org./news")
    assert match is not None
    assert match.url == "https://example.org/news"
    assert "https://example.org../news" not in scope  # not a valid FQDN dot


def test_ip_literals():
    # IPv6 spellings canonicalize to one form
    scope = Scope([ScopeRule("::1", port=8080)])
    match = scope.match("http://[0:0:0:0:0:0:0:1]:8080/news")
    assert match is not None
    assert match.url == "http://[::1]:8080/news"
    assert "http://[::1]/news" not in scope  # different effective port
    assert "http://[::2]:8080/news" not in scope
    # IPv4
    assert "http://127.0.0.1/x" in Scope([ScopeRule("127.0.0.1")])
    assert "http://127.0.0.2/x" not in Scope([ScopeRule("127.0.0.1")])


def test_encoded_slashes_are_consistent():
    "Percent-encoded slashes decode before segment matching, one result either way."
    scope = Scope([ScopeRule("example.org", path="/news")])
    plain = scope.match("https://example.org/news/politics")
    encoded = scope.match("https://example.org/news%2Fpolitics")
    upper = scope.match("https://example.org/news%2fpolitics")
    assert encoded is not None and upper is not None
    assert encoded.url == upper.url == plain.url
    # an encoded slash cannot smuggle a path past the segment boundary:
    # "/news/../secret" resolves to "/secret", in or out consistently
    assert scope.match("https://example.org/news%2F..%2Fsecret") is None
    assert scope.match("https://example.org/news/../secret") is None
    assert "https://example.org/newsletter%2Fx" not in scope


def test_match_returns_rule_and_normalized_url():
    broad = ScopeRule("example.org")
    narrow = ScopeRule("example.org", path="/news", exclude=("/news/archive",))
    scope = Scope([broad, narrow])
    match = scope.match("HTTPS://Example.ORG:443/news/politics?utm=x#frag")
    assert isinstance(match, ScopeMatch)
    assert match.rule is narrow  # most specific rule wins, regardless of order
    assert match.url == "https://example.org/news/politics?utm=x"
    assert scope.match("https://example.org/news/archive/x").rule is broad
    assert scope.match("https://example.org/sport").rule is broad


def test_from_url():
    rule = ScopeRule.from_url("https://example.org:8080/news")
    assert (rule.host, rule.port, rule.path) == ("example.org", 8080, "/news")
    rule = ScopeRule.from_url("example.org/news", exclude=("/news/archive",))
    assert (rule.host, rule.port, rule.path) == ("example.org", None, "/news")
    assert rule.exclude == ("/news/archive",)
    assert "http://example.org/news/x" in Scope([rule])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": ""},
        {"host": "  "},
        {"host": "https://example.org"},  # a URL, not a bare host
        {"host": "example.org/news"},  # path in the host field
        {"host": "user@example.org"},  # userinfo
        {"host": "example.org:8080"},  # port belongs in the port field
        {"host": "example..org"},
        {"host": "exa_mple.org"},
        {"host": "-example.org"},
        {"host": "a" * 64 + ".org"},  # label too long
        {"host": "example.org", "port": 0},
        {"host": "example.org", "port": 65536},
        {"host": "example.org", "port": "8080"},
        {"host": "example.org", "port": True},
        {"host": "example.org", "path": "news"},  # must start with "/"
        {"host": "example.org", "path": "/news?x=1"},  # no query in rules
        {"host": "example.org", "path": "/news#top"},  # no fragment in rules
        {"host": "example.org", "path": "/news", "exclude": ("/sport",)},
        {"host": "example.org", "path": "/news", "exclude": ("/news",)},
        {"host": "example.org", "path": "/news", "exclude": ("news/archive",)},
        {"host": "127.0.0.1", "include_subdomains": True},  # IPs have no subdomains
    ],
)
def test_invalid_rules_rejected_upfront(kwargs):
    "Broken rules raise at construction, before any crawling starts."
    with pytest.raises(ScopeRuleError):
        ScopeRule(**kwargs)


def test_invalid_from_url():
    for bad in (
        "",
        "https://example.org/news?x=1",
        "https://user@example.org/",
        "http://example.org:99999/",
    ):
        with pytest.raises(ScopeRuleError):
            ScopeRule.from_url(bad)
    with pytest.raises(ScopeRuleError):
        Scope([])
    with pytest.raises(ScopeRuleError):
        Scope(["example.org"])  # rules must be ScopeRule instances
    # ScopeRuleError is a ValueError, so callers can catch either
    with pytest.raises(ValueError):
        ScopeRule("https://example.org")


def test_scope_container():
    rules = [ScopeRule("example.org"), ScopeRule("news.example.org", path="/en")]
    scope = Scope(rules)
    assert len(scope) == 2
    assert set(scope) == set(rules)
    assert scope.match("not a url") is None
    assert scope.match(None) is None
    assert scope.match("https://news.example.org/en/story").rule == rules[1]
