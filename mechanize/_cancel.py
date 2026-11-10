"""Caller-triggered cancellation support for multi-step browser operations.

A :class:`CancellationToken` is created and owned by the caller, who may
trigger cancellation at any time (typically from another thread or a
callback) by calling :meth:`CancellationToken.cancel`.  The token is passed
to :meth:`mechanize.Browser.open` (and methods built on top of it, such as
:meth:`mechanize.Browser.submit` and :meth:`mechanize.Browser.follow_link`)
via the ``cancel_token`` keyword argument.

Cancellation is checked at three points:

* before the next navigation starts (no request is sent, browser state is
  left untouched),
* after waiting for the response (the request was already sent to the
  server; the response is closed so the connection is released),
* while reading the response body (the underlying file object is closed so
  the connection is released).

When cancellation is detected, :class:`BrowserCancelled` is raised.  Its
``request_sent`` attribute tells the caller whether the request had already
been transmitted to the server -- a remote submission that already happened
cannot be undone, and is never reported as rolled back.

This code is free software; you can redistribute it and/or modify it
under the terms of the BSD or ZPL 2.1 licenses (see the file LICENSE
included with the distribution).

"""

from __future__ import absolute_import

import threading


class BrowserCancelled(Exception):
    """Raised when a cancellable browser operation is aborted.

    Public attributes:

    stage: one of ``"before_request"`` (cancelled before the request was
        sent), ``"waiting_response"`` (cancelled while waiting for the
        response) or ``"reading_body"`` (cancelled while reading the
        response body).
    request: the :class:`mechanize.Request` that was cancelled, or None.
    request_sent: True if the request had already been transmitted to the
        server.  In that case any remote side effects (e.g. a form
        submission) may already have happened and cannot be undone.

    """

    def __init__(self, stage, request=None, request_sent=False):
        self.stage = stage
        self.request = request
        self.request_sent = request_sent
        if request_sent:
            detail = ("the request was already sent to the server and any "
                      "remote side effects cannot be undone")
        else:
            detail = "the request was not sent"
        Exception.__init__(
            self, "operation cancelled at stage %r: %s" % (stage, detail))


class CancellationToken:
    """Caller-owned token used to cancel browser operations.

    Pass an instance to :meth:`mechanize.Browser.open` (or methods built on
    it) via the ``cancel_token`` keyword argument, then call :meth:`cancel`
    to abort the operation.  Cancellation is sticky: once cancelled, the
    token remains cancelled, and passing it to further operations aborts
    them before any request is sent.

    """

    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        """Trigger cancellation.  Safe to call from any thread."""
        self._event.set()

    @property
    def cancelled(self):
        """True if :meth:`cancel` has been called."""
        return self._event.is_set()

    def raise_if_cancelled(self, stage, request=None, request_sent=False):
        """Raise :class:`BrowserCancelled` if cancellation was triggered."""
        if self.cancelled:
            raise BrowserCancelled(stage, request, request_sent)


class CancellableReader:
    """File-like wrapper that aborts reads when cancellation is triggered.

    Wraps the file object underlying a response.  When the token is
    cancelled, the wrapped file object is closed (releasing the connection)
    and :class:`BrowserCancelled` is raised with stage ``"reading_body"``.

    """

    def __init__(self, fp, token, request=None):
        self._fp = fp
        self._token = token
        self._request = request

    def _check(self):
        if self._token.cancelled:
            # do not keep holding the connection open
            self.close()
            raise BrowserCancelled(
                "reading_body", self._request, request_sent=True)

    def read(self, size=-1):
        self._check()
        data = self._fp.read(size)
        self._check()
        return data

    def readline(self, size=-1):
        self._check()
        line = self._fp.readline(size)
        self._check()
        return line

    def readlines(self, sizehint=-1):
        self._check()
        lines = self._fp.readlines(sizehint)
        self._check()
        return lines

    def close(self):
        return self._fp.close()

    def fileno(self):
        return self._fp.fileno()

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line
    next = __next__

    def __getattr__(self, name):
        return getattr(self._fp, name)


def wrap_response_fp(response, token, request=None):
    """Wrap a response's file object in a :class:`CancellableReader`.

    Works with mechanize's closeable_response (and anything else providing
    ``_set_fp``) as well as objects exposing a plain ``fp`` attribute.

    """
    if response is None:
        return None
    fp = getattr(response, "fp", None)
    if fp is None or isinstance(fp, CancellableReader):
        return response
    reader = CancellableReader(fp, token, request)
    set_fp = getattr(response, "_set_fp", None)
    if set_fp is not None:
        set_fp(reader)
    else:
        response.fp = reader
    return response
