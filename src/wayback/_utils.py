from collections import OrderedDict
from collections.abc import Mapping, MutableMapping
from datetime import date, datetime, timezone
import email.utils
from enum import Enum
import logging
import re
import requests
import requests.adapters
import threading
import time
from typing import Callable, Dict, Optional, Union
import urllib.parse
from .exceptions import RequestCancelled, SessionClosedError

logger = logging.getLogger(__name__)

URL_DATE_FORMAT = '%Y%m%d%H%M%S'
MEMENTO_URL_PATTERN = re.compile(r'^http(?:s)?://web.archive.org/web/(\d+)(\w\w_)?/(.+)$')
MEMENTO_URL_TEMPLATE = 'https://web.archive.org/web/{timestamp}{mode}/{url}'


def format_timestamp(value):
    """
    Format a value as a Wayback-style timestamp string.

    Parameters
    ----------
    value : str or datetime.datetime or datetime.date

    Returns
    -------
    str
    """
    if isinstance(value, str):
        return value
    elif isinstance(value, datetime):
        # Make sure we have either a naive datetime (assumed to
        # represent UTC) or convert the datetime to UTC.
        if value.tzinfo:
            value_utc = value.astimezone(timezone.utc)
        else:
            value_utc = value
        return value_utc.strftime(URL_DATE_FORMAT)
    elif isinstance(value, date):
        return value.strftime(URL_DATE_FORMAT)
    else:
        raise TypeError('Timestamp must be a datetime, date, or string')


def parse_timestamp(time_string):
    """
    Given a Wayback-style timestamp string, return an equivalent ``datetime``.
    """
    # Before parsing, try to fix invalid timestamps.
    # We've seen a handful of timestamps where "00" was inserted before the
    # month or day part of the timestamp, e.g:
    #
    #   20000008241731
    #       ^^ Month is "00"
    #
    # The Wayback team looked into some of these, and the "00" was always
    # an insertion, pushing the month or day and the following components of
    # the timestamp out by two characters. Then the seconds get truncated
    # (there's only room for 14 characters in the timestamp in the CDX index).
    # For example:
    #
    #   In raw data:   2000000824173151 (16 characters)
    #   In CDX index:  20000008241731   (Truncated to 14 characters)
    #   Correct value: 20000824173151   (Aug. 24, 2000 at 17:31:51 UTC)
    #
    # The best we can do for these cases is pull out the incorrect "00" and add
    # "00" for the seconds that got truncated. This isn't exact, but we can't
    # see the raw data so this is as close as we can get.
    #
    # The issue seems to be limited to some crawls in the year 2000.
    if time_string[5] == '0' and time_string[4] == '0':
        logger.warning('found invalid timestamp with month 00: %s', time_string)
        time_string = f'{time_string[0:4]}{time_string[6:]}00'
    elif time_string[7] == '0' and time_string[6] == '0':
        logger.warning('found invalid timestamp with day 00: %s', time_string)
        time_string = f'{time_string[0:6]}{time_string[8:]}00'

    # Parse the cleaned-up result.
    return datetime.strptime(time_string, URL_DATE_FORMAT).replace(tzinfo=timezone.utc)


def parse_retry_after(retry_after_header, now: Optional[Callable[[], float]] = None):
    """
    Given a response object, return the recommended retry-after time in seconds
    or ``None`` if there is no recommended timeframe. Returns ``0`` if the
    time was in the past or could not be parsed.

    Parameters
    ----------
    retry_after_header : str, optional
        The value of an HTTP ``Retry-After`` header. It may be a number of
        seconds or an HTTP date.
    now : callable, optional
        A callable that returns the current time in seconds since the epoch.
        This is primarily intended for testing with an injected clock.
    """
    if isinstance(retry_after_header, str):
        seconds = 0
        try:
            seconds = int(retry_after_header)
        except ValueError:
            retry_date_tuple = email.utils.parsedate_tz(retry_after_header)
            if retry_date_tuple:
                retry_date = email.utils.mktime_tz(retry_date_tuple)
                seconds = retry_date - int((now or time.time)())

        return max(0, seconds)

    return None


def ensure_utc_datetime(value):
    """
    Given a datetime, date, or Wayback-style timestamp string, return an
    equivalent datetime in UTC.

    Parameters
    ----------
    value : str or datetime.datetime or datetime.date

    Returns
    -------
    datetime.datetime
    """
    if isinstance(value, str):
        return parse_timestamp(value)
    elif isinstance(value, datetime):
        if value.tzinfo:
            return value.astimezone(timezone.utc)
        else:
            return value.replace(tzinfo=timezone.utc)
    elif isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    else:
        raise TypeError('`datetime` must be a string, date, or datetime')


def clean_memento_url_component(url):
    """
    Attempt to fix encoding or other issues with the target URL that was
    embedded in a memento URL.
    """
    # A URL *may* be percent encoded, decode ONLY if so (we don’t want to
    # accidentally decode the querystring if there is one)
    lower_url = url.lower()
    if lower_url.startswith('http%3a') or lower_url.startswith('https%3a'):
        url = urllib.parse.unquote(url)

    return url


def memento_url_data(memento_url):
    """
    Get the original URL, time, and mode that a memento URL represents a
    capture of.

    Returns
    -------
    url : str
        The URL that the memento is a capture of.
    time : datetime.datetime
        The time the memento was captured in the UTC timezone.
    mode : str
        The playback mode.

    Examples
    --------
    Extract original URL, time and mode.

    >>> url = ('https://web.archive.org/web/20170813195036id_/'
    ...        'https://arpa-e.energy.gov/?q=engage/events-workshops')
    >>> memento_url_data(url)
    ('https://arpa-e.energy.gov/?q=engage/events-workshops',
     datetime.datetime(2017, 8, 13, 19, 50, 36, tzinfo=timezone.utc),
     'id_')
    """
    match = MEMENTO_URL_PATTERN.match(memento_url)
    if match is None:
        raise ValueError(f'"{memento_url}" is not a memento URL')

    url = clean_memento_url_component(match.group(3))
    date = parse_timestamp(match.group(1))
    mode = match.group(2) or ''

    return url, date, mode


def format_memento_url(url, timestamp, mode=''):
    """
    Get the URL for a memento of a given URL, timestamp, and mode.

    Parameters
    ----------
    url : str
    timestamp : str or datetime.datetime or datetime.date
    mode : str

    Returns
    -------
    str
    """
    return MEMENTO_URL_TEMPLATE.format(url=url, timestamp=format_timestamp(timestamp), mode=mode)


def set_memento_url_mode(url, mode):
    """
    Return a memento URL with the "mode" component set to the given mode. If
    the URL is not a memento URL, raises ``ValueError``.

    Parameters
    ----------
    url : str
    mode : str

    Returns
    -------
    str
    """
    captured_url, timestamp, _ = memento_url_data(url)
    return format_memento_url(captured_url, timestamp, mode)


class RateLimit:
    """
    ``RateLimit`` is a simple locking mechanism that can be used to enforce
    rate limits and is safe to use across multiple threads. It can also be used
    as a context manager.

    Calling `rate_limit_instance.wait()` blocks until a minimum time has passed
    since the last call. Using `with rate_limit_instance:` blocks entries to
    the context until a minimum time since the last context entry.

    Parameters
    ----------
    per_second : int or float
        The maximum number of calls per second that are allowed. If 0, a call
        to `wait()` will never block.

    Examples
    --------
    Slow down a tight loop to only occur twice per second:

    >>> limit = RateLimit(per_second=2)
    >>> for x in range(10):
    >>>     with limit:
    >>>         print(x)
    """

    def __init__(self, per_second: Union[int, float], clock: Optional[Callable[[], float]] = None):
        if not isinstance(per_second, (int, float)):
            raise TypeError(
                f'The RateLimit per_second argument must be an int or float, not {type(per_second).__name__}'
            )

        self._lock = threading.RLock()
        self._last_call_time = 0.0
        self._clock = clock or time.time
        if per_second <= 0:
            self._minimum_wait = 0.0
        else:
            self._minimum_wait = 1.0 / per_second

    def wait(self) -> None:
        if self._minimum_wait == 0:
            return

        with self._lock:
            current_time = self._clock()
            idle_time = current_time - self._last_call_time
            if idle_time < self._minimum_wait:
                self._sleep(self._minimum_wait - idle_time)

            self._last_call_time = self._clock()

    def _sleep(self, seconds: float) -> None:
        # Separate method so tests can replace sleeping without replacing the
        # clock that drives the rate-limit calculations.
        time.sleep(seconds)

    def __enter__(self) -> None:
        self.wait()

    def __exit__(self, type, value, traceback) -> None:
        pass

    @classmethod
    def make_limit(cls, per_second: Union['RateLimit', int, float]) -> 'RateLimit':
        """
        If the given rate is a ``RateLimit`` object, return it unchanged.
        Otherwise, create a new ``RateLimit`` with the given rate.
        """
        if isinstance(per_second, RateLimit):
            return per_second
        else:
            return cls(per_second)


class CooldownReason(Enum):
    """
    Why a shared :class:`ServiceCooldown` was engaged.

    Distinguishing these reasons lets calling code apply different policies
    (e.g. honoring a server-provided ``Retry-After`` versus applying local
    exponential backoff after a connection failure).
    """

    #: The server explicitly asked clients to wait (e.g. an HTTP 429 with a
    #: ``Retry-After`` header).
    rate_limited = 'rate_limited'
    #: A request failed because of a network/connection problem, so clients
    #: should back off before trying again.
    connection_failure = 'connection_failure'


class ServiceCooldown:
    """
    A thread-safe, shareable cooldown state for coordinating requests to a
    service (such as the Wayback Machine) across multiple threads and even
    multiple :class:`~wayback.WaybackSession` instances.

    When one thread receives a rate-limited response (HTTP 429), it engages
    the cooldown with the duration from the response's ``Retry-After`` header.
    All other threads sharing this instance then block in :meth:`wait` until
    the cooldown expires instead of continuing to send requests into the
    service's rate limit.

    Waiting threads do not hold any HTTP connection or response: the cooldown
    only owns a lock, and the actual blocking sleep in :meth:`wait` happens
    outside that lock, so the cooldown state stays mutable for other threads
    while callers wait.

    A cooldown is keyed by its :class:`CooldownReason`. Cancelling one
    waiting request (e.g. via its cancellation event) never clears the
    cooldown for other requests. Whether a successful response clears an
    active cooldown is governed by the explicit ``clear_on_success`` policy,
    which defaults to ``False`` so a cooldown always runs to its scheduled
    end unless the caller opts in.

    Parameters
    ----------
    clear_on_success : bool, default: False
        If ``True``, a call to :meth:`note_success` clears any active
        cooldown. If ``False`` (the default), successful responses do not end
        a cooldown early; it always lasts until its scheduled end.
    clock : callable, optional
        A zero-argument callable returning the current monotonic-ish time in
        seconds. Defaults to :func:`time.time`. Injecting a clock makes the
        cooldown timing deterministic in tests without real sleeping or real
        rate-limit pressure.
    sleep : callable, optional
        A callable taking a number of seconds to sleep. Defaults to
        :func:`time.sleep`. Tests can inject a no-op or a clock-advancing
        sleep.
    """

    def __init__(
        self,
        clear_on_success: bool = False,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        poll_interval: float = 0.05,
    ):
        self._lock = threading.RLock()
        self._clear_on_success = clear_on_success
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self._poll_interval = poll_interval
        # End times keyed by why the cooldown was engaged. A missing entry or
        # an end time at/before the current time means no active cooldown.
        self._until: Dict[CooldownReason, float] = {}

    @property
    def clear_on_success(self) -> bool:
        "Whether a successful response clears any active cooldown."
        return self._clear_on_success

    def remaining(self, reason: Optional[CooldownReason] = None) -> float:
        """
        Return how many seconds remain in the active cooldown.

        If ``reason`` is given, only cooldowns of that reason are considered.
        Otherwise the longest remaining cooldown across all reasons is
        returned. Returns ``0`` if there is no active cooldown.
        """
        with self._lock:
            now = self._clock()
            if reason is not None:
                return max(0.0, self._until.get(reason, 0.0) - now)
            if not self._until:
                return 0.0
            return max(0.0, max(self._until.values()) - now)

    def is_active(self, reason: Optional[CooldownReason] = None) -> bool:
        "Return whether a cooldown is currently active."
        return self.remaining(reason) > 0

    def engage(self, seconds: float, reason: CooldownReason) -> float:
        """
        Engage or extend the cooldown for ``reason`` for at least ``seconds``
        seconds from now.

        If a cooldown for the same reason is already active and ends later
        than the requested new end time, the existing end time is kept (a
        cooldown is never shortened). Returns the resulting remaining time.
        """
        if seconds < 0:
            seconds = 0
        with self._lock:
            new_until = self._clock() + seconds
            current_until = self._until.get(reason, 0.0)
            self._until[reason] = max(current_until, new_until)
            return max(0.0, self._until[reason] - self._clock())

    def clear(self, reason: Optional[CooldownReason] = None) -> None:
        """
        Clear the cooldown. If ``reason`` is given, clear only that reason's
        cooldown; otherwise clear all cooldowns.
        """
        with self._lock:
            if reason is None:
                self._until.clear()
            else:
                self._until.pop(reason, None)

    def note_success(self) -> None:
        """
        Record a successful response.

        By default this does nothing, so an active cooldown always runs to its
        scheduled end. If ``clear_on_success`` was enabled, this clears any
        active cooldown so waiting threads stop blocking on their next check.
        """
        if self._clear_on_success:
            self.clear()

    def wait(self, cancellation: Optional[threading.Event] = None) -> None:
        """
        Block while any cooldown is active, then return.

        The blocking sleep happens *outside* the internal lock, so no HTTP
        connection or response resource is held while blocked and other
        threads can still engage, extend, or clear the cooldown.

        Parameters
        ----------
        cancellation : threading.Event, optional
            If this event is set while waiting, raise
            :class:`~wayback.exceptions.RequestCancelled` and leave the shared
            cooldown intact for other requests. Cancelling one request never
            clears the cooldown.
        """
        while True:
            with self._lock:
                remaining = self.remaining()
                if remaining <= 0:
                    return
                if cancellation is not None and cancellation.is_set():
                    raise RequestCancelled('Request was cancelled while waiting for a service cooldown')
                sleep_for = min(remaining, self._poll_interval)

            # Sleep without holding the lock so the cooldown state stays
            # mutable for other threads while this one waits.
            self._sleep(sleep_for)

            if cancellation is not None and cancellation.is_set():
                raise RequestCancelled('Request was cancelled while waiting for a service cooldown')

    def wait_for(self, seconds: float, cancellation: Optional[threading.Event] = None) -> None:
        """
        Engage a cooldown for ``seconds`` (using
        :attr:`CooldownReason.connection_failure`) and wait it out. This is
        the entry point for connection-failure backoff.
        """
        self.engage(seconds, CooldownReason.connection_failure)
        self.wait(cancellation)


class DepthCountedContext:
    """
    DepthCountedContext is a mixin or base class for context managers that need
    to be perform special operations only when all nested contexts they might
    be used in have exited.

    Override the `__exit_all__(self, type, value, traceback)` method to get a
    version of `__exit__` that is only called when exiting the top context.

    As a convenience, the built-in `__enter__` returns `self`, which is fairly
    common, so in many cases you don't need to author your own `__enter__` or
    `__exit__` methods.
    """

    _context_depth = 0

    def __enter__(self):
        self._context_depth += 1
        return self

    def __exit__(self, type, value, traceback):
        if self._context_depth > 0:
            self._context_depth -= 1
        if self._context_depth == 0:
            return self.__exit_all__(type, value, traceback)

    def __exit_all__(self, type, value, traceback):
        """
        A version of the normal `__exit__` context manager method that only
        gets called when the top level context is exited. This is meant to be
        overridden in your class.
        """
        pass


class DisableAfterCloseSession(requests.Session):
    """
    A custom session object raises a :class:`SessionClosedError` if you try to
    use it after closing it, to help identify and avoid potentially dangerous
    code patterns. (Standard session objects continue to be usable after
    closing, even if they may not work exactly as expected.)
    """

    _closed = False

    def close(self, disable=True):
        super().close()
        if disable:
            self._closed = True

    def send(self, *args, **kwargs):
        if self._closed:
            raise SessionClosedError('This session has already been closed and cannot send new HTTP requests.')

        return super().send(*args, **kwargs)


class CaseInsensitiveDict(MutableMapping):
    """
    A case-insensitive ``dict`` subclass.

    Implements all methods and operations of ``MutableMapping`` as well as
    dict's ``copy``.

    All keys are expected to be strings. The structure remembers the case of
    the last key to be set, and ``iter(instance)``, ``keys()``, ``items()``,
    ``iterkeys()``, and ``iteritems()`` will contain case-sensitive keys.
    However, querying and contains testing is case insensitive::

      cid = CaseInsensitiveDict()
      cid['Accept'] = 'application/json'
      cid['aCCEPT'] == 'application/json'  # True
      list(cid) == ['Accept']  # True

    For example, ``headers['content-encoding']`` will return the value of a
    ``'Content-Encoding'`` response header, regardless of how the header name
    was originally stored. If the constructor, ``.update``, or equality
    comparison operations are given keys that have equal lower-case keys, the
    behavior is undefined.

    This implementation is based on Requests v2.28.0, which is available under
    the Apache 2.0 license at https://github.com/psf/requests.
    """

    def __init__(self, data=None, **kwargs):
        self._store = OrderedDict()
        if data is None:
            data = {}
        self.update(data, **kwargs)

    def __setitem__(self, key, value):
        if not isinstance(key, str):
            raise TypeError(f'The key "{key}" is not a string')

        # Use the lowercased key for lookups, but store the actual
        # key alongside the value.
        self._store[key.lower()] = (key, value)

    def __getitem__(self, key):
        return self._store[key.lower()][1]

    def __delitem__(self, key):
        del self._store[key.lower()]

    def __iter__(self):
        return (casedkey for casedkey, mappedvalue in self._store.values())

    def __len__(self):
        return len(self._store)

    def _lower_items(self):
        """Like iteritems(), but with all lowercase keys."""
        return ((lowerkey, keyval[1]) for (lowerkey, keyval) in self._store.items())

    def __eq__(self, other):
        if isinstance(other, Mapping):
            other = CaseInsensitiveDict(other)
        else:
            return NotImplemented
        # Compare insensitively
        return dict(self._lower_items()) == dict(other._lower_items())

    def copy(self):
        return CaseInsensitiveDict(self._store.values())

    def __repr__(self):
        return str(dict(self.items()))
