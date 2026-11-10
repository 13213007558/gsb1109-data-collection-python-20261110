#!/usr/bin/env python
"""Tests for caller-triggered cancellation of browser operations."""

from unittest import TestCase

import mechanize
from mechanize._response import closeable_response, make_headers


class ControlledStream:
    """File-like object whose reads can be scripted from the test.

    Records close() so tests can verify the connection is released, and
    supports a callback so a read can trigger cancellation mid-flight.
    """

    def __init__(self, chunks=(b"", ), on_read=None):
        self._chunks = list(chunks)
        self._on_read = on_read
        self.read_count = 0
        self.closed = False

    def read(self, size=-1):
        self.read_count += 1
        if self._on_read is not None:
            self._on_read(self)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def readline(self, size=-1):
        return self.read(size)

    def close(self):
        self.closed = True

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line
    next = __next__


class CountingHandler(mechanize.BaseHandler):
    """http_open handler that counts requests and serves canned responses."""

    def __init__(self, response_factory, on_request=None):
        self.response_factory = response_factory
        self.on_request = on_request
        self.request_count = 0
        self.requests = []

    def http_open(self, request):
        self.request_count += 1
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)
        return self.response_factory(request)


class TestBrowser(mechanize.Browser):
    default_features = []
    default_others = []
    default_schemes = []


def html_response(request, html=b"<html><body>blah</body></html>",
                  streams=None):
    stream = ControlledStream([html])
    if streams is not None:
        streams.append(stream)
    return closeable_response(
        stream, make_headers([("Content-type", "text/html")]),
        request.get_full_url(), 200, "OK")


FORM_PAGE = (b'<html><body>'
             b'<form name="f" method="post" action="http://example.com/submit">'
             b'<input type="text" name="q" value="x"/>'
             b'<input type="submit" name="go" value="Go"/>'
             b'</form></body></html>')


class CancellationTests(TestCase):

    def make_browser(self, response_factory, on_request=None):
        browser = TestBrowser()
        handler = CountingHandler(response_factory, on_request)
        browser.add_handler(handler)
        return browser, handler

    def test_cancel_before_navigation(self):
        streams = []
        browser, handler = self.make_browser(
            lambda req: html_response(req, streams=streams))
        browser.open("http://example.com/")
        self.assertEqual(handler.request_count, 1)

        token = mechanize.CancellationToken()
        token.cancel()
        with self.assertRaises(mechanize.BrowserCancelled) as cm:
            browser.open("http://example.com/two", cancel_token=token)
        exc = cm.exception
        self.assertEqual(exc.stage, "before_request")
        self.assertFalse(exc.request_sent)
        self.assertEqual(exc.request.get_full_url(), "http://example.com/two")

        # the cancelled step never started: no request was sent, and the
        # current page was not torn down
        self.assertEqual(handler.request_count, 1)
        self.assertEqual(browser.geturl(), "http://example.com/")
        self.assertFalse(streams[0].closed)

        # the browser is genuinely reusable with a fresh token
        browser.open("http://example.com/two",
                     cancel_token=mechanize.CancellationToken())
        self.assertEqual(handler.request_count, 2)
        self.assertEqual(browser.geturl(), "http://example.com/two")

    def test_cancel_waiting_response(self):
        streams = []
        token = mechanize.CancellationToken()

        def cancel_during_request(request):
            if request.get_full_url().endswith("/two"):
                token.cancel()

        browser, handler = self.make_browser(
            lambda req: html_response(req, streams=streams),
            on_request=cancel_during_request)
        browser.open("http://example.com/")
        self.assertEqual(handler.request_count, 1)

        with self.assertRaises(mechanize.BrowserCancelled) as cm:
            browser.open("http://example.com/two", cancel_token=token)
        exc = cm.exception
        self.assertEqual(exc.stage, "waiting_response")
        # the request reached the server: it cannot be undone, only the
        # local wait was aborted
        self.assertTrue(exc.request_sent)
        self.assertEqual(handler.request_count, 2)
        # the in-flight response was closed, releasing the connection
        self.assertTrue(streams[-1].closed)
        # honest state: no current response, but the browser still works
        self.assertIsNone(browser.response())

        # the sticky token stops subsequent requests before they start
        with self.assertRaises(mechanize.BrowserCancelled) as cm:
            browser.open("http://example.com/three", cancel_token=token)
        self.assertEqual(cm.exception.stage, "before_request")
        self.assertEqual(handler.request_count, 2)

        # with a fresh token the browser is reusable
        browser.open("http://example.com/three",
                     cancel_token=mechanize.CancellationToken())
        self.assertEqual(handler.request_count, 3)
        self.assertEqual(browser.geturl(), "http://example.com/three")

    def test_cancel_while_reading_body(self):
        token = mechanize.CancellationToken()
        streams = []

        def cancelling_stream(chunks):
            def on_read(stream):
                token.cancel()
            stream = ControlledStream(chunks, on_read=on_read)
            streams.append(stream)
            return stream

        def response_factory(request):
            return closeable_response(
                cancelling_stream([b"<html><body>big</body></html>"]),
                make_headers([("Content-type", "text/html")]),
                request.get_full_url(), 200, "OK")

        browser, handler = self.make_browser(response_factory)
        response = browser.open("http://example.com/", cancel_token=token)
        self.assertEqual(handler.request_count, 1)

        with self.assertRaises(mechanize.BrowserCancelled) as cm:
            response.read()
        exc = cm.exception
        self.assertEqual(exc.stage, "reading_body")
        self.assertTrue(exc.request_sent)
        # reading the body stopped and the file object was closed instead of
        # keeping the connection open
        self.assertTrue(streams[0].closed)
        # the error is unambiguously a cancellation, not a network error
        self.assertNotIsInstance(exc, mechanize.HTTPError)
        self.assertNotIsInstance(exc, mechanize.URLError)

    def test_submit_not_started_when_cancelled(self):
        browser, handler = self.make_browser(
            lambda req: html_response(req, FORM_PAGE))
        browser.open("http://example.com/")
        browser.select_form(name="f")
        self.assertEqual(handler.request_count, 1)

        token = mechanize.CancellationToken()
        token.cancel()
        with self.assertRaises(mechanize.BrowserCancelled) as cm:
            browser.submit(cancel_token=token)
        exc = cm.exception
        self.assertEqual(exc.stage, "before_request")
        self.assertFalse(exc.request_sent)
        # the submission step never started: nothing was sent
        self.assertEqual(handler.request_count, 1)

    def test_submit_sent_is_not_reported_as_undone(self):
        token = mechanize.CancellationToken()

        def cancel_during_request(request):
            if request.get_full_url().endswith("/submit"):
                token.cancel()

        browser, handler = self.make_browser(
            lambda req: html_response(req, FORM_PAGE),
            on_request=cancel_during_request)
        browser.open("http://example.com/")
        browser.select_form(name="f")

        with self.assertRaises(mechanize.BrowserCancelled) as cm:
            browser.submit(cancel_token=token)
        exc = cm.exception
        self.assertEqual(exc.stage, "waiting_response")
        # the POST was transmitted: the result must not claim it was undone
        self.assertTrue(exc.request_sent)
        self.assertEqual(handler.request_count, 2)
        submitted = handler.requests[-1]
        self.assertEqual(submitted.get_full_url(), "http://example.com/submit")
        self.assertIsNotNone(submitted.data)
        self.assertIn("cannot be undone", str(exc))

    def test_follow_link_cancel_token(self):
        page = (b'<html><body><a href="http://example.com/two">two</a>'
                b'</body></html>')
        browser, handler = self.make_browser(
            lambda req: html_response(req, page))
        browser.open("http://example.com/")
        self.assertEqual(handler.request_count, 1)

        token = mechanize.CancellationToken()
        token.cancel()
        with self.assertRaises(mechanize.BrowserCancelled) as cm:
            browser.follow_link(text="two", cancel_token=token)
        self.assertEqual(cm.exception.stage, "before_request")
        self.assertEqual(handler.request_count, 1)

        browser.follow_link(text="two",
                            cancel_token=mechanize.CancellationToken())
        self.assertEqual(handler.request_count, 2)
        self.assertEqual(browser.geturl(), "http://example.com/two")

    def test_uncancelled_token_does_not_change_behaviour(self):
        streams = []
        browser, handler = self.make_browser(
            lambda req: html_response(req, streams=streams))
        token = mechanize.CancellationToken()
        response = browser.open("http://example.com/", cancel_token=token)
        self.assertEqual(response.read(), b"<html><body>blah</body></html>")
        self.assertEqual(handler.request_count, 1)
        self.assertTrue(streams[0].read_count > 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
