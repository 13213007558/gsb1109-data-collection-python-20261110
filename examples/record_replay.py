#!/usr/bin/env python

"""Record a login + query flow, then replay it offline without credentials.

The website is simulated by an in-process handler, so the whole script runs
with no network access.  The same pattern works against a real site: simply
remove ``ScriptedSite`` and let mechanize use its normal HTTP handlers.

Run with:  python examples/record_replay.py
"""

from __future__ import print_function

import os
import tempfile
from io import BytesIO

import mechanize
from mechanize._response import closeable_response, make_headers
from mechanize._urllib2_fork import BaseHandler


BASE = "http://intranet.example"
REAL_PASSWORD = "do-not-export-me"
SESSION_COOKIE = "secret-session-token"

LOGIN_PAGE = (
    b'<html><body><form method="POST" action="/login">'
    b'<input name="username">'
    b'<input name="password" type="password">'
    b'</form></body></html>')
RESULTS_PAGE = b"<html><body>query results for order 123</body></html>"


def _page(url, code, msg, headers, body):
    return closeable_response(BytesIO(body), make_headers(headers), url,
                              code, msg)


class ScriptedSite(BaseHandler):
    """Tiny in-process stand-in for the real intranet site."""

    handler_order = 500

    def _handle(self, request):
        url = request.get_full_url()
        path = url.split("?", 1)[0]
        if request.get_method() == "GET" and path == BASE + "/login":
            return _page(url, 200, "OK",
                         [("Content-Type", "text/html")], LOGIN_PAGE)
        if request.get_method() == "POST":
            return _page(url, 302, "Found",
                         [("Location", BASE + "/query"),
                          ("Set-Cookie",
                           "sid=%s; Path=/; HttpOnly" % SESSION_COOKIE)],
                         b"")
        if SESSION_COOKIE in request.get_header("Cookie", ""):
            return _page(url, 200, "OK",
                         [("Content-Type", "text/html")], RESULTS_PAGE)
        return _page(url, 403, "Forbidden",
                     [("Content-Type", "text/html")], b"forbidden")

    http_open = _handle
    https_open = _handle


def main():
    cassette = tempfile.mktemp(suffix=".json")

    # 1) Record the real flow.
    browser = mechanize.Browser()
    browser.set_handle_robots(False)
    browser._replace_handler("http", ScriptedSite())
    browser._replace_handler("https", ScriptedSite())

    policy = mechanize.RedactionPolicy(redact_fields=["username", "password"])
    browser.set_recorder(policy=policy)

    browser.open(BASE + "/login")
    browser.select_form(nr=0)
    browser["username"] = "alice"
    browser["password"] = REAL_PASSWORD
    browser.submit()
    browser.open(BASE + "/query")
    browser.save_recording(cassette)
    browser.stop_recording()

    exported = open(cassette, encoding="utf-8").read()
    assert REAL_PASSWORD not in exported
    assert SESSION_COOKIE not in exported
    print("cassette written with no credentials: %s" % cassette)

    # 2) Hand the cassette to a colleague, who replays it fully offline.
    colleague = mechanize.Browser()
    colleague.set_handle_robots(False)
    colleague.set_replay(cassette)  # real HTTP/HTTPS handlers are removed

    colleague.open(BASE + "/login")
    colleague.select_form(nr=0)
    colleague["username"] = "replace-me"   # redacted fields match by name
    colleague["password"] = "replace-me"
    colleague.submit()
    response = colleague.open(BASE + "/query")
    colleague.assert_replay_complete()

    print("replayed final page:", response.geturl())
    print("body:", response.read().decode("utf-8"))
    os.unlink(cassette)


if __name__ == "__main__":
    main()
