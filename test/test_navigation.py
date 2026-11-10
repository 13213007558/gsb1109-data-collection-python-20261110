#!/usr/bin/env python
"""Tests for the unified navigation policy.

The same disallowed target is reached through every navigation mechanism
mechanize supports -- an ordinary link, an HTTP 30x redirect, an HTTP
``Refresh`` header and an HTML ``http-equiv`` meta refresh -- and must be
refused in exactly the same way, before the request is sent, while leaving the
current page and a troubleshooting record behind.

"""

from unittest import TestCase

import mechanize


START_URL = "http://example.com/start"
REDIRECT_URL = "http://example.com/redir"
REFRESH_URL = "http://example.com/refresh"
META_URL = "http://example.com/meta"
EVIL_URL = "http://evil.example/land"
EVIL_ORIGIN = "http://evil.example"


def page(url, body=b"<html><body>page</body></html>"):
    return mechanize.make_response(
        body, [("Content-Type", "text/html")], url)


def link_page(url, target, name="out"):
    body = (
        '<html><body><a href="%s" name="%s">go</a></body></html>'
        % (target, name)).encode("ascii")
    return page(url, body)


class ScriptedHTTPHandler(mechanize.BaseHandler):
    """Transport stub that serves URL-keyed responses without a network.

    A route value is either a ready response or one of the descriptors
    ``("redirect", target)``, ``("refresh", target)``, ``("meta", target)``
    or ``("link", target)``.  Every requested URL is recorded so tests can
    prove a disallowed target was never fetched.

    """

    handler_order = -10

    def __init__(self, routes):
        self.routes = dict(routes)
        self.requests = []

    def _serve(self, request):
        current_url = request.get_full_url()
        route = self.routes[current_url]
        if not isinstance(route, tuple):
            return route
        kind, value = route
        if kind == "link":
            return link_page(current_url, value)
        if kind == "redirect":
            resp = mechanize.make_response(
                b"", [("Location", value)], current_url, 302, "Found")
            return self.parent.error(
                "http", request, resp, 302, "Found", resp.info())
        if kind == "refresh":
            return mechanize.make_response(
                b"<html></html>",
                [("Content-Type", "text/html"),
                 ("Refresh", "0; url=%s" % value)],
                current_url)
        if kind == "meta":
            body = (
                '<html><head><meta http-equiv="refresh" '
                'content="0; url=%s"></head><body></body></html>'
                % value).encode("ascii")
            return page(current_url, body)
        raise AssertionError("unknown route kind %r" % kind)

    def http_open(self, request):
        self.requests.append(request.get_full_url())
        return self._serve(request)

    def __copy__(self):
        # real handlers support no-arg construction; mirror that here so the
        # transport survives Browser cloning
        return self.__class__(self.routes)

    https_open = http_open


# Routes shared by the three single-step navigation scenarios.  EVIL_URL has a
# route so an *allowed* navigation can complete, while tests can assert it is
# never requested when the policy denies it.
EVIL_ROUTES = {
    START_URL: page(START_URL),
    REDIRECT_URL: ("redirect", EVIL_URL),
    REFRESH_URL: ("refresh", EVIL_URL),
    META_URL: ("meta", EVIL_URL),
    EVIL_URL: page(EVIL_URL),
}


def make_browser(routes, policy=None):
    browser = mechanize.Browser()
    # robots.txt would generate extra, irrelevant requests
    browser.set_handle_robots(False)
    browser.add_handler(ScriptedHTTPHandler(routes))
    if policy is not None:
        browser.set_navigation_policy(policy)
    return browser


class OriginTests(TestCase):
    def test_origin_of(self):
        self.assertEqual(
            mechanize.origin_of("https://Example.COM:8443/a/b?x=1#f"),
            "https://example.com:8443")
        self.assertEqual(
            mechanize.origin_of("http://example.com"), "http://example.com")
        self.assertIsNone(mechanize.origin_of("/relative/path"))
        self.assertIsNone(mechanize.origin_of(None))

    def test_exact_origin_matching_no_subdomain_trust(self):
        policy = mechanize.OriginNavigationPolicy(["http://example.com/"])
        for url in ("http://example.com/", "http://example.com/a?b=c"):
            origin = mechanize.origin_of(url)
            allowed, _ = policy.allow(origin, url, mechanize.DIRECT)
            self.assertTrue(allowed)
        for url in (
                "http://www.example.com/",       # subdomain is not trusted
                "https://example.com/",          # different scheme
                "http://example.com:8080/",      # different port
                "http://evil.example/"):
            origin = mechanize.origin_of(url)
            allowed, reason = policy.allow(origin, url, mechanize.DIRECT)
            self.assertFalse(allowed)
            self.assertTrue(reason)

    def test_originless_target_is_denied(self):
        policy = mechanize.OriginNavigationPolicy(["http://example.com/"])
        allowed, _ = policy.allow(None, "not-a-url", mechanize.DIRECT)
        self.assertFalse(allowed)

    def test_invalid_allowed_origin_rejected(self):
        self.assertRaises(
            ValueError, mechanize.OriginNavigationPolicy, ["relative/ref"])

    def test_base_policy_denies_by_default(self):
        policy = mechanize.BaseNavigationPolicy()
        allowed, reason = policy.allow(
            "http://example.com", "http://example.com/", mechanize.DIRECT)
        self.assertFalse(allowed)
        self.assertTrue(reason)


class NavigationPolicyTests(TestCase):
    def setUp(self):
        self.policy = mechanize.OriginNavigationPolicy([
            "http://example.com/"])

    def _start_browser(self, routes):
        browser = make_browser(routes, self.policy)
        browser.open(START_URL)
        self.assertEqual(browser.geturl(), START_URL)
        return browser

    def _transport(self, browser):
        matches = [h for h in browser.handlers
                   if isinstance(h, ScriptedHTTPHandler)]
        return matches[0]

    def test_default_disabled_keeps_legacy_behaviour(self):
        # No policy installed: every mechanism is free to leave the origin.
        routes = {START_URL: ("link", EVIL_URL), EVIL_URL: page(EVIL_URL)}
        browser = make_browser(routes)
        self.assertIsNone(browser.navigation_policy)
        browser.open(START_URL)
        browser.follow_link(name="out")
        self.assertEqual(browser.geturl(), EVIL_URL)
        self.assertEqual(browser.blocked_navigations, [])

    def test_link_to_disallowed_origin_is_blocked_before_request(self):
        routes = {START_URL: ("link", EVIL_URL), EVIL_URL: page(EVIL_URL)}
        browser = self._start_browser(routes)
        handler = self._transport(browser)
        history_len = len(browser._history._history)

        self.assertRaises(
            mechanize.NavigationDenied, browser.follow_link, name="out")

        # current page is untouched and still readable
        self.assertEqual(browser.geturl(), START_URL)
        self.assertIn(b"go", browser.response().read())
        self.assertEqual(len(browser._history._history), history_len)
        # the disallowed URL was never requested
        self.assertEqual(handler.requests, [START_URL])

        record = browser.blocked_navigations[-1]
        self.assertEqual(record.target_url, EVIL_URL)
        self.assertEqual(record.target_origin, EVIL_ORIGIN)
        self.assertEqual(record.source_type, mechanize.LINK)
        self.assertEqual(record.source_url, START_URL)
        self.assertEqual(record.source_origin, "http://example.com")
        self.assertTrue(record.reason)

    def test_redirect_to_disallowed_origin_is_blocked_before_request(self):
        browser = self._start_browser(EVIL_ROUTES)
        handler = self._transport(browser)

        self.assertRaises(
            mechanize.NavigationDenied, browser.open, REDIRECT_URL)

        # only the approved redirecting URL was requested, never the target
        self.assertEqual(handler.requests, [START_URL, REDIRECT_URL])
        # the page displayed before the navigation is preserved
        self.assertEqual(browser.geturl(), START_URL)
        self.assertIn(b"page", browser.response().read())

        record = browser.blocked_navigations[-1]
        self.assertEqual(record.target_origin, EVIL_ORIGIN)
        self.assertEqual(record.source_type, mechanize.REDIRECT)
        self.assertEqual(record.source_url, REDIRECT_URL)

    def test_refresh_header_to_disallowed_origin_is_blocked(self):
        browser = self._start_browser(EVIL_ROUTES)
        handler = self._transport(browser)

        self.assertRaises(
            mechanize.NavigationDenied, browser.open, REFRESH_URL)

        self.assertEqual(handler.requests, [START_URL, REFRESH_URL])
        self.assertEqual(browser.geturl(), START_URL)
        record = browser.blocked_navigations[-1]
        self.assertEqual(record.source_type, mechanize.REFRESH)
        self.assertEqual(record.source_url, REFRESH_URL)
        self.assertEqual(record.target_origin, EVIL_ORIGIN)

    def test_meta_refresh_to_disallowed_origin_is_blocked(self):
        browser = self._start_browser(EVIL_ROUTES)
        handler = self._transport(browser)

        self.assertRaises(
            mechanize.NavigationDenied, browser.open, META_URL)

        self.assertEqual(handler.requests, [START_URL, META_URL])
        self.assertEqual(browser.geturl(), START_URL)
        record = browser.blocked_navigations[-1]
        # meta refresh is surfaced through the same refresh mechanism
        self.assertEqual(record.source_type, mechanize.REFRESH)
        self.assertEqual(record.source_url, META_URL)

    def test_restriction_is_consistent_across_all_mechanisms(self):
        # The same target must produce the same decision no matter how it is
        # reached.
        scenarios = [
            (mechanize.LINK, "follow",
             {START_URL: ("link", EVIL_URL), EVIL_URL: page(EVIL_URL)}),
            (mechanize.REDIRECT, "redirect", EVIL_ROUTES),
            (mechanize.REFRESH, "refresh", EVIL_ROUTES),
            (mechanize.REFRESH, "meta", EVIL_ROUTES),
        ]
        for expected_source, label, routes in scenarios:
            browser = self._start_browser(routes)
            if label == "follow":
                action = lambda: browser.follow_link(name="out")
            else:
                target = {
                    "redirect": REDIRECT_URL,
                    "refresh": REFRESH_URL,
                    "meta": META_URL}[label]
                action = lambda target=target: browser.open(target)
            self.assertRaises(mechanize.NavigationDenied, action)
            record = browser.blocked_navigations[-1]
            self.assertEqual(record.target_origin, EVIL_ORIGIN, label)
            self.assertEqual(record.source_type, expected_source, label)
            self.assertEqual(browser.geturl(), START_URL, label)

    def test_allowed_target_works_identically_across_mechanisms(self):
        policy = mechanize.OriginNavigationPolicy([
            "http://example.com", EVIL_ORIGIN])
        link_routes = {START_URL: ("link", EVIL_URL),
                       EVIL_URL: page(EVIL_URL)}
        scenarios = [
            ("follow", link_routes, lambda b: b.follow_link(name="out")),
            ("redirect", EVIL_ROUTES, lambda b: b.open(REDIRECT_URL)),
            ("refresh", EVIL_ROUTES, lambda b: b.open(REFRESH_URL)),
            ("meta", EVIL_ROUTES, lambda b: b.open(META_URL)),
        ]
        for label, routes, action in scenarios:
            browser = make_browser(routes, policy)
            browser.open(START_URL)
            action(browser)
            self.assertEqual(browser.geturl(), EVIL_URL, label)
            self.assertEqual(browser.blocked_navigations, [], label)

    def test_novisit_requests_are_checked_too(self):
        browser = self._start_browser(EVIL_ROUTES)
        self.assertRaises(
            mechanize.NavigationDenied,
            browser.open_novisit, EVIL_URL)
        self.assertEqual(browser.geturl(), START_URL)

    def test_custom_policy_can_distinguish_source(self):
        seen = []

        class SourceAwarePolicy(mechanize.BaseNavigationPolicy):
            def allow(self, target_origin, target_url, source_type,
                      source_origin=None, source_url=None):
                seen.append((target_origin, source_type))
                # links and direct calls may leave, automatic
                # redirects/refreshes may not
                return (source_type in (mechanize.LINK, mechanize.DIRECT),
                        "custom decision")

        browser = make_browser(EVIL_ROUTES, SourceAwarePolicy())
        browser.open(START_URL)
        self.assertRaises(
            mechanize.NavigationDenied, browser.open, REDIRECT_URL)
        self.assertIn((EVIL_ORIGIN, mechanize.REDIRECT), seen)

        link_routes = {START_URL: ("link", EVIL_URL),
                       EVIL_URL: page(EVIL_URL)}
        browser2 = make_browser(link_routes, SourceAwarePolicy())
        browser2.open(START_URL)
        browser2.follow_link(name="out")
        self.assertEqual(browser2.geturl(), EVIL_URL)

    def test_disabling_policy_restores_navigation(self):
        routes = {START_URL: ("link", EVIL_URL), EVIL_URL: page(EVIL_URL)}
        browser = self._start_browser(routes)
        self.assertRaises(
            mechanize.NavigationDenied, browser.follow_link, name="out")
        browser.set_navigation_policy(None)
        self.assertIsNone(browser.navigation_policy)
        browser.follow_link(name="out")
        self.assertEqual(browser.geturl(), EVIL_URL)

    def test_denied_exception_is_urlerror_with_record(self):
        routes = {START_URL: ("link", EVIL_URL), EVIL_URL: page(EVIL_URL)}
        browser = self._start_browser(routes)
        try:
            browser.follow_link(name="out")
        except mechanize.URLError as error:
            self.assertIsInstance(error, mechanize.NavigationDenied)
            self.assertEqual(error.record.target_origin, EVIL_ORIGIN)
        else:
            self.fail("NavigationDenied (a URLError) was not raised")

    def test_multi_hop_redirect_denial_preserves_displayed_page(self):
        hop1 = "http://example.com/hop1"
        hop2 = "http://example.com/hop2"
        routes = {
            START_URL: page(START_URL),
            hop1: ("redirect", hop2),
            hop2: ("redirect", EVIL_URL),
            EVIL_URL: page(EVIL_URL),
        }
        browser = self._start_browser(routes)
        handler = self._transport(browser)

        self.assertRaises(mechanize.NavigationDenied, browser.open, hop1)

        # both approved hops were requested, the evil target was not
        self.assertEqual(handler.requests, [START_URL, hop1, hop2])
        # the page that was displayed before the navigation survives
        self.assertEqual(browser.geturl(), START_URL)
        self.assertIn(b"page", browser.response().read())
        record = browser.blocked_navigations[-1]
        self.assertEqual(record.source_type, mechanize.REDIRECT)
        self.assertEqual(record.source_url, hop2)
        self.assertEqual(record.target_origin, EVIL_ORIGIN)

    def test_repeated_denials_accumulate_records(self):
        routes = {START_URL: ("link", EVIL_URL), EVIL_URL: page(EVIL_URL)}
        browser = self._start_browser(routes)
        for _ in range(3):
            self.assertRaises(
                mechanize.NavigationDenied, browser.follow_link, name="out")
        self.assertEqual(len(browser.blocked_navigations), 3)
        self.assertEqual(browser.geturl(), START_URL)

    def test_clone_inherits_policy_and_log(self):
        import copy
        routes = {START_URL: ("link", EVIL_URL), EVIL_URL: page(EVIL_URL)}
        browser = self._start_browser(routes)
        self.assertRaises(
            mechanize.NavigationDenied, browser.follow_link, name="out")

        clone = copy.copy(browser)
        self.assertIs(clone.navigation_policy, self.policy)
        self.assertEqual(len(clone.blocked_navigations),
                         len(browser.blocked_navigations))
        # the copied policy keeps enforcing even though the clone has no
        # displayed page of its own
        self.assertRaises(
            mechanize.NavigationDenied, clone.open_novisit, EVIL_URL)
        self.assertEqual(len(clone.blocked_navigations),
                         len(browser.blocked_navigations) + 1)


class OpenerLevelTests(TestCase):
    def test_policy_enforced_at_opener_without_browser(self):
        opener = mechanize.UserAgentBase()
        opener.set_handle_robots(False)
        opener.set_navigation_policy(
            mechanize.OriginNavigationPolicy(["http://example.com"]))

        class DenyHandler(mechanize.BaseHandler):
            handler_order = -10

            def __init__(self):
                self.called = []

            def http_open(self, request):
                self.called.append(request.get_full_url())
                return page(request.get_full_url())

        handler = DenyHandler()
        opener.add_handler(handler)
        self.assertRaises(
            mechanize.NavigationDenied,
            opener.open, mechanize.Request(EVIL_URL))
        self.assertEqual(handler.called, [])
        self.assertEqual(
            opener.blocked_navigations[-1].source_type, mechanize.DIRECT)

        # an approved origin still goes through
        opener.open(mechanize.Request(START_URL))
        self.assertEqual(handler.called, [START_URL])

    def test_default_opener_has_no_policy(self):
        opener = mechanize.UserAgentBase()
        self.assertIsNone(opener.navigation_policy)
        self.assertEqual(opener.blocked_navigations, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
