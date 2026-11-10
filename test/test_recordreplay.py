#!/usr/bin/env python
"""Tests for optional interaction recording and offline replay.

The transport is replaced by an in-process scripted handler, so these tests
exercise the full Browser / cookie / redirect / form machinery without opening
any socket.  Replay tests additionally remove the HTTP and HTTPS handlers,
proving that responses come solely from the cassette.
"""

from __future__ import absolute_import

import json
import os
import tempfile
import unittest
from io import BytesIO

import mechanize
from mechanize._recordreplay import (
    RedactionPolicy, RecordingSession, RequestMismatchError, ReplayHandler,
    _Vault, build_request_fingerprint, decode_body, diff_fingerprints,
    encode_body)
from mechanize._response import closeable_response, make_headers
from mechanize._urllib2_fork import BaseHandler


BASE = "http://acme.example"
SECRET_SID = "TOPSECRET-SESSION-999"
REAL_USER = "alice"
REAL_PASSWORD = "hunter2"
SECRETS = (SECRET_SID, REAL_USER, REAL_PASSWORD)

LOGIN_FORM = (
    b'<html><body><form method="POST" action="/login">'
    b'<input name="username">'
    b'<input name="password" type="password">'
    b'<input name="q" value="books">'
    b'<input name="tag" value="a"><input name="tag" value="b">'
    b'</form></body></html>')
QUERY_PAGE = b"<html><body>QUERY RESULTS secret-data</body></html>"
BINARY_BODY = b"\x00\x01\x02\xff\xfe binary \x00 payload"


def make_response(url, code, msg, headers, body):
    return closeable_response(BytesIO(body), make_headers(headers), url,
                              code, msg)


class ScriptedTransport(BaseHandler):
    """Stand-in for the real HTTP(S) transport, recording nothing itself."""

    handler_order = 500

    def __init__(self):
        self.calls = []

    def _handle(self, req):
        self.calls.append((req.get_method(), req.get_full_url()))
        url = req.get_full_url()
        path = url.split("?", 1)[0]
        method = req.get_method()

        if method == "GET" and path == BASE + "/login":
            return make_response(
                url, 200, "OK", [("Content-Type", "text/html")], LOGIN_FORM)

        if method == "POST" and path == BASE + "/login":
            from urllib.parse import parse_qs
            raw = req.data
            if isinstance(raw, str):
                raw = raw.encode("iso-8859-1")
            form = parse_qs(raw.decode("iso-8859-1"))
            if (form.get("username") == [REAL_USER] and
                    form.get("password") == [REAL_PASSWORD]):
                return make_response(
                    url, 302, "Found",
                    [("Location", BASE + "/query"),
                     ("Set-Cookie",
                      "sid=%s; Path=/; HttpOnly" % SECRET_SID)], b"")
            return make_response(
                url, 401, "Unauthorized",
                [("Content-Type", "text/html")], b"bad credentials")

        if method == "GET" and path == BASE + "/query":
            if SECRET_SID in req.get_header("Cookie", ""):
                return make_response(
                    url, 200, "OK",
                    [("Content-Type", "text/html")], QUERY_PAGE)
            return make_response(
                url, 403, "Forbidden",
                [("Content-Type", "text/html")], b"forbidden")

        if method == "GET" and path == BASE + "/binary":
            return make_response(
                url, 200, "OK",
                [("Content-Type", "application/octet-stream")], BINARY_BODY)

        return make_response(
            url, 404, "Not Found",
            [("Content-Type", "text/html")], b"no such page")

    http_open = _handle
    https_open = _handle


def make_browser(transport=None):
    browser = mechanize.Browser()
    browser.set_handle_robots(False)
    if transport is not None:
        browser._replace_handler("http", transport)
        browser._replace_handler("https", transport)
    return browser


def login_policy():
    return RedactionPolicy(redact_fields=["username", "password"])


def fill_and_submit_login(browser, q="books"):
    browser.select_form(nr=0)
    browser["username"] = REAL_USER
    browser["password"] = REAL_PASSWORD
    browser["q"] = q
    return browser.submit()


class RedactionUnitTests(unittest.TestCase):

    def test_policy_covers_auth_and_cookies_by_default(self):
        policy = RedactionPolicy()
        names = policy.redacted_header_names()
        self.assertIn("Authorization", names)
        self.assertIn("Proxy-Authorization", names)
        self.assertIn("Cookie", names)

    def test_placeholder_is_stable_within_a_session(self):
        policy = login_policy()
        vault = _Vault(policy)
        first = vault.token("FIELD", "password")
        second = vault.token("FIELD", "password")
        self.assertEqual(first, second)
        self.assertNotEqual(first, vault.token("FIELD", "username"))
        self.assertEqual(first, "{{MECH_FIELD_PASSWORD}}")

    def test_repeated_fields_are_kept_in_order_and_redacted(self):
        policy = login_policy()
        vault = _Vault(policy)
        request = mechanize.Request(
            BASE + "/login",
            data=b"username=alice&password=hunter2&tag=a&tag=b",
            headers={"Content-Type":
                     "application/x-www-form-urlencoded"})
        fingerprint = build_request_fingerprint(request, policy, vault)
        fields = fingerprint["body"]["fields"]
        self.assertEqual(
            [name for name, _ in fields],
            ["username", "password", "tag", "tag"])
        self.assertEqual(fields[0][1], "{{MECH_FIELD_USERNAME}}")
        self.assertEqual(fields[1][1], "{{MECH_FIELD_PASSWORD}}")
        # Non-sensitive, repeated fields survive untouched.
        self.assertEqual([value for _, value in fields[2:]], ["a", "b"])

    def test_binary_body_roundtrip(self):
        encoded = encode_body(BINARY_BODY)
        self.assertEqual(encoded["kind"], "bytes")
        self.assertEqual(encoded["length"], len(BINARY_BODY))
        self.assertEqual(decode_body(encoded), BINARY_BODY)

    def test_text_body_stays_readable(self):
        encoded = encode_body(LOGIN_FORM)
        self.assertEqual(encoded["kind"], "text")
        self.assertEqual(decode_body(encoded), LOGIN_FORM)

    def test_diff_reports_url_and_field_differences(self):
        policy = login_policy()
        expected = build_request_fingerprint(
            mechanize.Request(BASE + "/login",
                              data=b"q=books",
                              headers={"Content-Type":
                                       "application/x-www-form-urlencoded"}),
            policy, _Vault(policy))
        actual = build_request_fingerprint(
            mechanize.Request(BASE + "/login?x=1",
                              data=b"q=movies",
                              headers={"Content-Type":
                                       "application/x-www-form-urlencoded"}),
            policy, _Vault(policy))
        lines = diff_fingerprints(expected, actual)
        joined = "\n".join(lines)
        self.assertIn("url:", joined)
        self.assertIn("q", joined)
        self.assertTrue(any("field" in line for line in lines))

    def test_diff_reports_missing_header(self):
        expected = {"method": "GET", "url": BASE,
                    "headers": [["X-Trace", "1"]],
                    "body": {"kind": "empty"}}
        actual = {"method": "GET", "url": BASE, "headers": [],
                  "body": {"kind": "empty"}}
        lines = diff_fingerprints(expected, actual)
        self.assertTrue(any("X-Trace" in line for line in lines))


class RecordReplayFlowTests(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.addCleanup(self._remove_cassette)

    def _remove_cassette(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _record(self):
        transport = ScriptedTransport()
        browser = make_browser(transport)
        session = browser.set_recorder(policy=login_policy())
        browser.open(BASE + "/login")
        response = fill_and_submit_login(browser)
        self.assertEqual(browser.geturl(), BASE + "/query")
        self.assertEqual(response.code, 200)
        self.assertIn(b"QUERY RESULTS", browser.response().read())
        browser.save_recording(self.path)
        browser.stop_recording()
        return session

    def test_cassette_keeps_order_status_pages_but_no_credentials(self):
        session = self._record()
        self.assertEqual([entry["index"] for entry in session.entries],
                         [0, 1, 2])
        with open(self.path, encoding="utf-8") as handle:
            raw = handle.read()
        for secret in SECRETS:
            self.assertNotIn(secret, raw)
        data = json.loads(raw)
        methods = [entry["request"]["method"] for entry in data["entries"]]
        self.assertEqual(methods, ["GET", "POST", "GET"])
        statuses = [entry["response"]["code"] for entry in data["entries"]]
        self.assertEqual(statuses, [200, 302, 200])
        # The form page and the query results page are both retained.
        self.assertIn("<form", raw)
        self.assertIn("QUERY RESULTS", raw)

    def test_cookie_value_uses_one_associated_placeholder(self):
        self._record()
        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        token = "{{MECH_COOKIE_SID}}"
        set_cookie = json.dumps(data["entries"][1]["response"]["headers"])
        request_cookie = json.dumps(data["entries"][2]["request"]["headers"])
        self.assertIn(token, set_cookie)
        self.assertIn(token, request_cookie)
        self.assertNotIn(SECRET_SID, set_cookie + request_cookie)

    def test_replay_walks_same_pages_without_any_transport(self):
        self._record()
        browser = mechanize.Browser()
        browser.set_handle_robots(False)
        handler = browser.set_replay(self.path)

        # Real scheme transports must be gone: only the cassette can answer.
        self.assertNotIn("http", browser._ua_handlers)
        self.assertNotIn("https", browser._ua_handlers)
        self.assertIsInstance(handler, ReplayHandler)

        login = browser.open(BASE + "/login")
        self.assertIn(b"<form", login.read())
        fill_and_submit_login(browser)
        self.assertEqual(browser.geturl(), BASE + "/query")
        self.assertEqual(browser.response().code, 200)
        self.assertEqual(browser.response().read(), QUERY_PAGE)
        browser.assert_replay_complete()

    def test_replay_accepts_substitute_credentials(self):
        # The colleague never learns the real password: redacted fields match
        # by name, so dummy values still replay the same transitions.
        self._record()
        browser = mechanize.Browser()
        browser.set_handle_robots(False)
        browser.set_replay(self.path)
        browser.open(BASE + "/login")
        browser.select_form(nr=0)
        browser["username"] = "replace-me"
        browser["password"] = "replace-me"
        browser["q"] = "books"
        browser.submit()
        self.assertEqual(browser.geturl(), BASE + "/query")
        browser.assert_replay_complete()

    def test_mismatch_raises_diff_instead_of_another_response(self):
        self._record()
        browser = mechanize.Browser()
        browser.set_handle_robots(False)
        browser.set_replay(self.path)
        browser.open(BASE + "/login")
        browser.select_form(nr=0)
        browser["username"] = "replace-me"
        browser["password"] = "replace-me"
        browser["q"] = "movies"  # non-redacted field diverges
        with self.assertRaises(RequestMismatchError) as caught:
            browser.submit()
        self.assertEqual(caught.exception.index, 1)
        self.assertTrue(
            any("q" in line for line in caught.exception.diff))

    def test_extra_request_does_not_open_network(self):
        self._record()
        browser = mechanize.Browser()
        browser.set_handle_robots(False)
        browser.set_replay(self.path)
        browser.open(BASE + "/login")
        browser.select_form(nr=0)
        fill_and_submit_login(browser)
        with self.assertRaises(RequestMismatchError) as caught:
            browser.open(BASE + "/unexpected")
        self.assertEqual(caught.exception.reason, "cassette exhausted")

    def test_error_status_is_recorded_and_replayed(self):
        transport = ScriptedTransport()
        browser = make_browser(transport)
        browser.set_recorder(policy=login_policy())
        browser.open(BASE + "/login")
        browser.select_form(nr=0)
        browser["username"] = "wrong"
        browser["password"] = "wrong"
        with self.assertRaises(mechanize.HTTPError) as recorded_err:
            browser.submit()
        self.assertEqual(recorded_err.exception.code, 401)
        browser.save_recording(self.path)
        browser.stop_recording()

        replay = mechanize.Browser()
        replay.set_handle_robots(False)
        replay.set_replay(self.path)
        replay.open(BASE + "/login")
        replay.select_form(nr=0)
        replay["username"] = "wrong"
        replay["password"] = "wrong"
        with self.assertRaises(mechanize.HTTPError) as replayed_err:
            replay.submit()
        self.assertEqual(replayed_err.exception.code, 401)

    def test_binary_response_replays_byte_for_byte(self):
        transport = ScriptedTransport()
        browser = make_browser(transport)
        browser.set_recorder(policy=login_policy())
        browser.open(BASE + "/binary")
        browser.save_recording(self.path)
        browser.stop_recording()

        replay = mechanize.Browser()
        replay.set_handle_robots(False)
        replay.set_replay(self.path)
        response = replay.open(BASE + "/binary")
        self.assertEqual(response.read(), BINARY_BODY)
        replay.assert_replay_complete()

    def test_stop_replay_reinstalls_transport_handlers(self):
        self._record()
        browser = mechanize.Browser()
        browser.set_handle_robots(False)
        browser.set_replay(self.path)
        self.assertNotIn("http", browser._ua_handlers)
        browser.stop_replay()
        self.assertIn("http", browser._ua_handlers)
        self.assertIsNone(browser.replay)

    def test_session_save_load_roundtrip(self):
        session = self._record()
        loaded = RecordingSession.load(self.path)
        self.assertEqual(loaded.to_dict()["entries"],
                         session.to_dict()["entries"])
        self.assertEqual(loaded.policy.redact_fields,
                         ("username", "password"))

    def test_factory_helpers(self):
        transport = ScriptedTransport()
        browser, session = mechanize.new_recording_browser(
            policy=login_policy())
        browser.set_handle_robots(False)
        browser._replace_handler("http", transport)
        browser._replace_handler("https", transport)
        browser.open(BASE + "/login")
        browser.save_recording(self.path)

        replay = mechanize.new_replay_browser(self.path)
        replay.set_handle_robots(False)
        self.assertIsInstance(replay.replay, ReplayHandler)
        response = replay.open(BASE + "/login")
        self.assertIn(b"<form", response.read())

    def test_default_headers_match_across_different_transports(self):
        # A custom User-Agent added via opener-level addheaders must appear in
        # both fingerprints even though replay has no real transport handler.
        transport = ScriptedTransport()
        browser = make_browser(transport)
        browser.addheaders = [("User-agent", "turtle/4.2")]
        browser.set_recorder(policy=login_policy())
        browser.open(BASE + "/login")
        browser.save_recording(self.path)
        browser.stop_recording()

        with open(self.path, encoding="utf-8") as handle:
            recorded = json.load(handle)
        recorded_header = dict(
            (name.lower(), value)
            for name, value in recorded["entries"][0]["request"]["headers"])
        self.assertEqual(recorded_header["user-agent"], "turtle/4.2")

        replay = mechanize.Browser()
        replay.set_handle_robots(False)
        replay.addheaders = [("User-agent", "turtle/4.2")]
        replay.set_replay(self.path)
        response = replay.open(BASE + "/login")
        self.assertEqual(response.code, 200)
        replay.assert_replay_complete()

    def test_authorization_header_is_redacted(self):
        transport = ScriptedTransport()
        browser = make_browser(transport)
        browser.set_header("Authorization", "Basic YWxpY2U6aHVudGVyMg==")
        browser.set_recorder(policy=login_policy())
        browser.open(BASE + "/login")
        browser.save_recording(self.path)
        browser.stop_recording()

        exported = open(self.path, encoding="utf-8").read()
        self.assertNotIn("Basic YWxpY2U6aHVudGVyMg==", exported)
        self.assertIn("{{MECH_HEADER_AUTHORIZATION}}", exported)

        replay = mechanize.Browser()
        replay.set_handle_robots(False)
        replay.set_header("Authorization", "Basic cmVwbGFjZS1tZQ==")
        replay.set_replay(self.path)
        response = replay.open(BASE + "/login")
        self.assertEqual(response.code, 200)
        replay.assert_replay_complete()

    def test_browser_clone_keeps_recording_and_replay_linked(self):
        import copy

        transport = ScriptedTransport()
        browser = make_browser(transport)
        session = browser.set_recorder(policy=login_policy())
        clone = copy.copy(browser)
        # The clone keeps recording into the same shared session.
        self.assertIs(clone.recording_session, session)
        clone.open(BASE + "/login")
        self.assertEqual(len(session.entries), 1)

        browser.save_recording(self.path)
        replay = mechanize.Browser()
        replay.set_handle_robots(False)
        replay.set_replay(self.path)
        replay_clone = copy.copy(replay)
        self.assertIsNotNone(replay_clone.replay)
        self.assertIsNot(replay_clone.replay, replay.replay)
        response = replay_clone.open(BASE + "/login")
        self.assertEqual(response.code, 200)


if __name__ == "__main__":
    unittest.main()
