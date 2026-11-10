"""Explicit asynchronous export flow.

Some business systems answer an export form submission with ``202
Accepted`` and only later make the real attachment available.  This
module models that flow explicitly:

* the export form is submitted exactly once;
* the caller declares, via :class:`AsyncExportSpec`, how to locate the
  status address, how to recognise success and failure, and where the
  result link is;
* the status address may be polled repeatedly, but a failed poll never
  causes the original submission to be resent;
* a server-suggested wait (``Retry-After``) and the overall wait cap
  are honoured independently;
* the wait may be cancelled locally, but cancellation never pretends
  to revoke the remote job.

Copyright 2026 mechanize contributors

This code is free software; you can redistribute it and/or modify it
under the terms of the BSD or ZPL 2.1 licenses (see the file LICENSE
included with the distribution).

"""

from __future__ import absolute_import

import time

from ._rfc3986 import urljoin
from ._urllib2 import HTTPError, URLError
from ._util import http2time


class AsyncExportError(Exception):
    """Base class for asynchronous export errors."""


class ExportJobFailed(AsyncExportError):
    """The remote export job reported failure.

    The job identity and the server-supplied reason are preserved on
    the exception and on the job itself.
    """

    def __init__(self, job_id, reason):
        self.job_id = job_id
        self.reason = reason
        AsyncExportError.__init__(
            self, "export job %s failed: %s" % (job_id, reason))


class ExportWaitCancelled(AsyncExportError):
    """The local wait was cancelled.

    The remote job is unaffected: it keeps running on the server and
    may still be polled again later via the same job object.
    """

    def __init__(self, job_id):
        self.job_id = job_id
        AsyncExportError.__init__(
            self, "stopped waiting for export job %s "
            "(the remote job was NOT cancelled)" % (job_id,))


class ExportWaitTimeout(AsyncExportError):
    """The total wait cap was exceeded before the job finished."""

    def __init__(self, job_id, waited):
        self.job_id = job_id
        self.waited = waited
        AsyncExportError.__init__(
            self, "export job %s not finished after %.3f seconds"
            % (job_id, waited))


class AsyncExportSpec:
    """Caller-declared rules for one asynchronous export endpoint.

    All callables receive a response object (as returned by
    ``Browser.open``) and return:

    ``status_url(response)``
        The address to poll, extracted from the 202 response to the
        export form submission.
    ``is_success(status_response)``
        True if the job finished successfully.
    ``is_failure(status_response)``
        True if the job failed.
    ``result_url(status_response)``
        The download link for the finished export.
    ``failure_reason(status_response)`` (optional)
        A human-readable reason for a failed job.
    """

    def __init__(self, status_url, is_success, is_failure, result_url,
                 failure_reason=None):
        self.status_url = status_url
        self.is_success = is_success
        self.is_failure = is_failure
        self.result_url = result_url
        self.failure_reason = (
            failure_reason if failure_reason is not None
            else lambda response: None)


def _retry_after_delay(response):
    """Return the server-suggested wait in seconds, or None."""
    info = response.info()
    if info is None:
        return None
    value = info.get("Retry-After")
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    # HTTP-date form.  This is compared against wall-clock time, so it
    # is only meaningful with the default (real) clock.
    when = http2time(value)
    if when is None:
        return None
    return max(0.0, when - time.time())


class ExportJob:
    """Handle for a single submitted asynchronous export job.

    ``state`` is one of ``PENDING``, ``SUCCEEDED``, ``FAILED`` or
    ``CANCELLED``.  ``CANCELLED`` only means the local wait was
    abandoned; the remote job still exists and the job may be waited
    on again.
    """

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def __init__(self, browser, spec, status_url, clock, sleep,
                 poll_interval, max_wait):
        self.browser = browser
        self.spec = spec
        self.status_url = status_url
        #: Stable identity of the remote job.
        self.job_id = status_url
        self.state = self.PENDING
        #: Server-supplied failure reason, once failed.
        self.reason = None
        #: Absolute download link, once succeeded.
        self.result_url = None
        #: Downloaded attachment response, once fetched.
        self.result = None
        #: Last transient poll error, if any.
        self.last_poll_error = None
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._max_wait = max_wait
        self._cancel_requested = False

    def cancel_wait(self):
        """Ask :meth:`wait` to stop waiting.

        This is purely local: no request is sent to the server and the
        remote job is not cancelled.  The job may be polled or waited
        on again afterwards.
        """
        self._cancel_requested = True

    def poll(self):
        """Perform a single status check.

        Returns True if the job reached a terminal state during this
        check.  Raises :class:`ExportJobFailed` if the job failed.
        Transient transport errors propagate to the caller; they never
        trigger a resubmission of the export form.
        """
        response = self.browser.open(self.status_url)
        return self._interpret(response)

    def wait(self, timeout=None, poll_interval=None):
        """Poll until the job finishes, then fetch the attachment.

        Returns the response for the downloaded result.  Honours the
        server-suggested ``Retry-After`` delay between polls, falling
        back to ``poll_interval``.  The total wait is capped by
        ``timeout`` (or the ``max_wait`` given at submission);
        exceeding it raises :class:`ExportWaitTimeout` and leaves the
        job pending so waiting may resume later.
        """
        if poll_interval is None:
            poll_interval = self._poll_interval
        limit = timeout if timeout is not None else self._max_wait
        start = self._clock()
        while True:
            if self._cancel_requested:
                self._cancel_requested = False
                self.state = self.CANCELLED
                raise ExportWaitCancelled(self.job_id)
            delay = poll_interval
            try:
                response = self.browser.open(self.status_url)
            except (URLError, HTTPError) as error:
                # A failed poll is transient: record it and keep
                # waiting.  The export form is never resubmitted.
                self.last_poll_error = error
            else:
                self.last_poll_error = None
                if self._interpret(response):
                    return self._fetch_result()
                suggested = _retry_after_delay(response)
                if suggested is not None:
                    delay = suggested
            if limit is not None:
                waited = self._clock() - start
                if waited + delay > limit:
                    raise ExportWaitTimeout(self.job_id, waited)
            self._sleep(delay)

    def _interpret(self, response):
        spec = self.spec
        if spec.is_failure(response):
            self.state = self.FAILED
            self.reason = spec.failure_reason(response)
            raise ExportJobFailed(self.job_id, self.reason)
        if spec.is_success(response):
            self.state = self.SUCCEEDED
            self.result_url = urljoin(
                self.status_url, spec.result_url(response))
            return True
        return False

    def _fetch_result(self):
        if self.result is None:
            self.result = self.browser.open(self.result_url)
        return self.result


def submit_async_export(browser, spec, request=None, data=None,
                        clock=time.monotonic, sleep=time.sleep,
                        poll_interval=1.0, max_wait=None, **kwds):
    """Submit an export form once and return an :class:`ExportJob`.

    ``browser`` is a :class:`mechanize.Browser` (or any object with a
    compatible ``open``/``submit`` interface).  If ``request`` is None
    the currently selected form is submitted with ``browser.submit``,
    otherwise ``browser.open(request, data)`` is used.  The server is
    expected to answer ``202 Accepted``; ``spec.status_url`` locates
    the polling address in that response.

    ``clock`` and ``sleep`` are injectable for testing: ``clock`` is a
    zero-argument callable returning seconds, ``sleep`` waits the
    given number of seconds.  ``poll_interval`` is the fallback delay
    between polls when the server does not suggest one, and
    ``max_wait`` caps the total time spent in a single
    :meth:`ExportJob.wait` call.

    The submission is performed exactly once here; polling afterwards
    never resubmits, even if individual status requests fail.
    """
    if request is None:
        response = browser.submit(**kwds)
    else:
        response = browser.open(request, data, **kwds)
    status_url = spec.status_url(response)
    if not status_url:
        raise AsyncExportError(
            "server did not provide a status address for the export job")
    status_url = urljoin(response.geturl(), status_url)
    return ExportJob(browser, spec, status_url, clock, sleep,
                     poll_interval, max_wait)
