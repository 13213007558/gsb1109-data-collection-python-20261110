#!/usr/bin/env python
"""Tests for mechanize._async_export."""

from unittest import TestCase

import mechanize
from mechanize import (AsyncExportError, AsyncExportSpec, ExportJob,
                       ExportJobFailed, ExportWaitCancelled,
                       ExportWaitTimeout, submit_async_export)
from mechanize._urllib2 import URLError


class FakeHeaders(dict):
    def getheaders(self, name):
        name = name.lower()
        return [v for k, v in self.items() if name == k.lower()]


class FakeResponse:
    def __init__(self, url, data=b"", code=200, headers=None):
        self.url = url
        self.data = data
        self.code = code
        self.headers = FakeHeaders(headers or {})

    def info(self):
        return self.headers

    def geturl(self):
        return self.url

    def read(self):
        return self.data


class FakeBrowser:
    """Browser stand-in replaying a scripted sequence of responses.

    Each script entry is ("submit"|"open", url-substring-or-None,
    response-or-exception).  Calls are recorded so tests can assert
    that the export form was submitted exactly once and that no remote
    cancellation was ever attempted.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self, kind, url):
        assert self.script, "unexpected %s to %r" % (kind, url)
        want_kind, want_url, outcome = self.script.pop(0)
        assert kind == want_kind, "expected %s, got %s" % (want_kind, kind)
        if want_url is not None:
            assert want_url in url, "expected %r in %r" % (want_url, url)
        self.calls.append((kind, url))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def submit(self, **kwds):
        return self._next("submit", "form-action")

    def open(self, request, data=None, **kwds):
        url = request if isinstance(request, str) else request.get_full_url()
        return self._next("open", url)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


EXPORT_URL = "http://example.com/export"
STATUS_URL = "http://example.com/jobs/42"
RESULT_URL = "http://example.com/jobs/result.csv"


def make_spec():
    return AsyncExportSpec(
        status_url=lambda r: r.info().get("Location"),
        is_success=lambda r: r.info().get("X-Job-State") == "done",
        is_failure=lambda r: r.info().get("X-Job-State") == "failed",
        result_url=lambda r: r.info().get("X-Result-Url"),
        failure_reason=lambda r: r.info().get("X-Failure-Reason"),
    )


def accepted(headers=None):
    h = {"Location": "/jobs/42"}
    h.update(headers or {})
    return FakeResponse(EXPORT_URL, code=202, headers=h)


def pending(headers=None):
    h = {"X-Job-State": "running"}
    h.update(headers or {})
    return FakeResponse(STATUS_URL, headers=h)


def done():
    return FakeResponse(STATUS_URL, headers={
        "X-Job-State": "done", "X-Result-Url": "result.csv"})


def failed(reason):
    return FakeResponse(STATUS_URL, headers={
        "X-Job-State": "failed", "X-Failure-Reason": reason})


def make_job(browser, clock, **kwds):
    kwds.setdefault("clock", clock.time)
    kwds.setdefault("sleep", clock.sleep)
    return submit_async_export(browser, make_spec(), **kwds)


class SubmitTests(TestCase):

    def test_exports_are_public(self):
        for name in ("AsyncExportError", "AsyncExportSpec", "ExportJob",
                     "ExportJobFailed", "ExportWaitCancelled",
                     "ExportWaitTimeout", "submit_async_export"):
            self.assertIn(name, mechanize.__all__)
            self.assertTrue(hasattr(mechanize, name))

    def test_submit_creates_job_once(self):
        clock = FakeClock()
        browser = FakeBrowser([("submit", None, accepted())])
        job = make_job(browser, clock)
        self.assertEqual(job.state, ExportJob.PENDING)
        self.assertEqual(job.status_url, STATUS_URL)
        self.assertEqual(job.job_id, STATUS_URL)
        self.assertEqual(browser.calls, [("submit", "form-action")])

    def test_missing_status_address_raises(self):
        clock = FakeClock()
        browser = FakeBrowser([
            ("submit", None, FakeResponse(EXPORT_URL, code=202))])
        self.assertRaises(AsyncExportError, make_job, browser, clock)


class WaitTests(TestCase):

    def test_poll_error_does_not_resubmit_and_completes(self):
        clock = FakeClock()
        browser = FakeBrowser([
            ("submit", None, accepted()),
            ("open", "/jobs/42", URLError("connection reset")),
            ("open", "/jobs/42", pending()),
            ("open", "/jobs/42", done()),
            ("open", "result.csv", FakeResponse(
                RESULT_URL, data=b"a,b\n1,2\n")),
        ])
        job = make_job(browser, clock, poll_interval=2.0)
        result = job.wait()
        self.assertEqual(job.state, ExportJob.SUCCEEDED)
        self.assertEqual(result.read(), b"a,b\n1,2\n")
        self.assertIs(job.result, result)
        self.assertEqual(job.result_url, RESULT_URL)
        self.assertIsNone(job.last_poll_error)
        submits = [c for c in browser.calls if c[0] == "submit"]
        self.assertEqual(len(submits), 1)
        self.assertFalse(browser.script)
        # fallback interval used when no Retry-After was suggested
        self.assertEqual(clock.sleeps, [2.0, 2.0])

    def test_retry_after_is_honoured(self):
        clock = FakeClock()
        browser = FakeBrowser([
            ("submit", None, accepted()),
            ("open", "/jobs/42", pending({"Retry-After": "7"})),
            ("open", "/jobs/42", done()),
            ("open", "result.csv", FakeResponse(RESULT_URL, data=b"x")),
        ])
        job = make_job(browser, clock, poll_interval=1.0)
        job.wait()
        self.assertEqual(clock.sleeps, [7.0])

    def test_max_wait_cap_raises_and_job_stays_pending(self):
        clock = FakeClock()
        browser = FakeBrowser([
            ("submit", None, accepted()),
            ("open", "/jobs/42", pending({"Retry-After": "30"})),
        ])
        job = make_job(browser, clock, max_wait=10.0)
        self.assertRaises(ExportWaitTimeout, job.wait)
        self.assertEqual(job.state, ExportJob.PENDING)
        self.assertEqual(clock.sleeps, [])

    def test_timeout_then_wait_again_succeeds(self):
        clock = FakeClock()
        browser = FakeBrowser([
            ("submit", None, accepted()),
            ("open", "/jobs/42", pending({"Retry-After": "30"})),
            ("open", "/jobs/42", done()),
            ("open", "result.csv", FakeResponse(RESULT_URL, data=b"y")),
        ])
        job = make_job(browser, clock, max_wait=10.0)
        self.assertRaises(ExportWaitTimeout, job.wait)
        result = job.wait(timeout=60.0)
        self.assertEqual(result.read(), b"y")
        self.assertEqual(job.state, ExportJob.SUCCEEDED)

    def test_failure_keeps_job_identity_and_reason(self):
        clock = FakeClock()
        browser = FakeBrowser([
            ("submit", None, accepted()),
            ("open", "/jobs/42", pending()),
            ("open", "/jobs/42", failed("query too broad")),
        ])
        job = make_job(browser, clock, poll_interval=1.0)
        try:
            job.wait()
        except ExportJobFailed as error:
            self.assertEqual(error.job_id, STATUS_URL)
            self.assertEqual(error.reason, "query too broad")
        else:
            self.fail("ExportJobFailed not raised")
        self.assertEqual(job.state, ExportJob.FAILED)
        self.assertEqual(job.job_id, STATUS_URL)
        self.assertEqual(job.reason, "query too broad")
        self.assertIsNone(job.result_url)
        self.assertFalse(browser.script)

    def test_cancel_wait_does_not_touch_remote_job(self):
        clock = FakeClock()
        browser = FakeBrowser([
            ("submit", None, accepted()),
            ("open", "/jobs/42", pending()),
            # waiting again afterwards still works
            ("open", "/jobs/42", done()),
            ("open", "result.csv", FakeResponse(RESULT_URL, data=b"z")),
        ])
        job = make_job(browser, clock, poll_interval=1.0)

        def cancel_instead_of_sleep(seconds):
            job.cancel_wait()

        job._sleep = cancel_instead_of_sleep
        self.assertRaises(ExportWaitCancelled, job.wait)
        self.assertEqual(job.state, ExportJob.CANCELLED)
        # only the submission and one status poll happened: no remote
        # cancellation request was issued
        self.assertEqual(browser.calls, [
            ("submit", "form-action"), ("open", STATUS_URL)])
        # the remote job is still there and can be waited on again
        job._sleep = clock.sleep
        result = job.wait(timeout=60.0)
        self.assertEqual(result.read(), b"z")
        self.assertEqual(job.state, ExportJob.SUCCEEDED)

    def test_single_poll(self):
        clock = FakeClock()
        browser = FakeBrowser([
            ("submit", None, accepted()),
            ("open", "/jobs/42", pending()),
        ])
        job = make_job(browser, clock)
        self.assertFalse(job.poll())
        self.assertEqual(job.state, ExportJob.PENDING)
        self.assertEqual(clock.sleeps, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
