"""
Tests for the shared, thread-safe service cooldown and for the way
:class:`WaybackSession` coordinates requests around it.

These tests use an injected fake clock and controlled (in-memory) responses.
They verify logical send times and cooldown behavior deterministically and
never perform real network requests or real rate-limit pressure testing.
"""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from unittest import mock

from .._client import WaybackSession, DEFAULT_RATE_LIMIT_DELAY
from .._utils import RateLimit, ServiceCooldown, CooldownReason
from ..exceptions import RateLimitError, RequestCancelled, WaybackRetryError


class FakeClock:
    "A manually controlled clock safe to read from multiple threads."

    def __init__(self, start=0.0):
        self._now = float(start)
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self._now

    def advance(self, seconds):
        with self._lock:
            self._now += seconds
            return self._now


def self_advancing_sleep(clock):
    """
    A sleep that advances the fake ``clock`` by the requested amount instead
    of blocking in real time. Both local backoff and cooldown waits therefore
    complete instantly in wall-clock terms while remaining deterministic in
    logical time, which is what these tests assert on.
    """

    def sleep(seconds):
        if seconds > 0:
            clock.advance(seconds)

    return sleep


def bounded_real_sleep(real_cap=0.01):
    """
    A sleep that always returns after at most ``real_cap`` real seconds, even
    when the fake clock is not advancing. This keeps cancellation responsive
    while a thread waits on a cooldown that nobody is moving the clock past.
    """

    def sleep(seconds):
        time.sleep(min(seconds, real_cap))

    return sleep


def make_response(status_code, headers=None, content=b''):
    response = requests.Response()
    response.status_code = status_code
    if headers:
        response.headers.update(headers)
    response._content = content
    response.url = 'http://test.com'
    return response


def make_session(clock, sleep, cooldown=None, **kwargs):
    options = dict(
        retries=0,
        search_calls_per_second=RateLimit(0),
        memento_calls_per_second=RateLimit(0),
        clock=clock,
        sleep=sleep,
    )
    options.update(kwargs)
    if cooldown is not None:
        options['cooldown'] = cooldown
    return WaybackSession(**options)


class TestServiceCooldown:
    def test_starts_inactive(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: None)
        assert not cooldown.is_active()
        assert cooldown.remaining() == 0

    def test_engage_sets_remaining_and_expires_with_clock(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: None)
        cooldown.engage(10, CooldownReason.rate_limited)

        assert cooldown.is_active()
        assert cooldown.is_active(CooldownReason.rate_limited)
        assert cooldown.remaining() == pytest.approx(10)

        clock.advance(4)
        assert cooldown.remaining() == pytest.approx(6)

        clock.advance(6)
        assert not cooldown.is_active()
        assert cooldown.remaining() == 0

    def test_engage_never_shortens_an_existing_cooldown(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: None)
        cooldown.engage(10, CooldownReason.rate_limited)
        clock.advance(2)
        # A shorter, later request must not reduce the remaining cooldown.
        cooldown.engage(3, CooldownReason.rate_limited)
        assert cooldown.remaining() == pytest.approx(8)

    def test_reasons_are_tracked_separately(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: None)
        cooldown.engage(10, CooldownReason.rate_limited)
        clock.advance(2)
        cooldown.engage(5, CooldownReason.connection_failure)

        assert cooldown.remaining(CooldownReason.rate_limited) == pytest.approx(8)
        assert cooldown.remaining(CooldownReason.connection_failure) == pytest.approx(5)
        # The generic remaining time is the longest of the active cooldowns.
        assert cooldown.remaining() == pytest.approx(8)

        cooldown.clear(CooldownReason.connection_failure)
        assert not cooldown.is_active(CooldownReason.connection_failure)
        assert cooldown.is_active(CooldownReason.rate_limited)

    def test_clear_removes_everything(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: None)
        cooldown.engage(10, CooldownReason.rate_limited)
        cooldown.engage(10, CooldownReason.connection_failure)
        cooldown.clear()
        assert not cooldown.is_active()

    def test_wait_returns_once_cooldown_elapses_without_real_sleeping(self):
        clock = FakeClock()
        slept = []
        cooldown = ServiceCooldown(
            clock=clock, sleep=lambda seconds: slept.append(seconds) or clock.advance(seconds)
        )
        cooldown.engage(10, CooldownReason.rate_limited)

        cooldown.wait()

        assert not cooldown.is_active()
        # The wait was driven entirely by advancing the fake clock.
        assert clock() == pytest.approx(10)
        assert sum(slept) == pytest.approx(10)

    def test_success_does_not_clear_by_default(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: None)
        cooldown.engage(10, CooldownReason.rate_limited)

        cooldown.note_success()

        assert cooldown.is_active()
        assert not cooldown.clear_on_success

    def test_success_clears_when_policy_enables_it(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(
            clear_on_success=True,
            clock=clock,
            sleep=lambda seconds: None,
        )
        cooldown.engage(10, CooldownReason.rate_limited)

        cooldown.note_success()

        assert not cooldown.is_active()

    def test_cancellation_raises_and_preserves_cooldown(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: None)
        cooldown.engage(10, CooldownReason.rate_limited)
        cancellation = threading.Event()
        cancellation.set()

        with pytest.raises(RequestCancelled):
            cooldown.wait(cancellation)

        # Cancelling one waiter must not lift the cooldown for anyone else.
        assert cooldown.is_active()
        assert cooldown.remaining() == pytest.approx(10)

    def test_cancellation_mid_wait_preserves_cooldown_for_others(self):
        clock = FakeClock()
        cancellation = threading.Event()
        # Bounded real sleeps keep the waiter responsive even though the clock
        # only advances when the test does so explicitly.
        cooldown = ServiceCooldown(clock=clock, sleep=bounded_real_sleep())
        cooldown.engage(10, CooldownReason.rate_limited)

        def cancelled_waiter():
            with pytest.raises(RequestCancelled):
                cooldown.wait(cancellation)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(cancelled_waiter)
            # Let the waiter enter its blocking sleep, then cancel it.
            time.sleep(0.02)
            cancellation.set()
            future.result(timeout=5)

        # The cooldown survives and another, uncancelled waiter still honors it.
        assert cooldown.is_active()
        cancellation.clear()
        clock.advance(10)
        cooldown.wait()
        assert not cooldown.is_active()

    def test_success_keeps_waiters_blocked_by_default(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=bounded_real_sleep())
        cooldown.engage(10, CooldownReason.rate_limited)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(cooldown.wait)
            time.sleep(0.03)
            # Under the default policy a success does not end the cooldown.
            cooldown.note_success()
            time.sleep(0.03)
            assert not future.done()
            assert cooldown.is_active()
            # The waiter is only released once the cooldown naturally elapses.
            clock.advance(10)
            future.result(timeout=5)

        assert not cooldown.is_active()

    def test_success_unblocks_waiters_when_policy_enabled(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(
            clear_on_success=True,
            clock=clock,
            sleep=bounded_real_sleep(),
        )
        cooldown.engage(10, CooldownReason.rate_limited)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(cooldown.wait)
            time.sleep(0.03)
            assert not future.done()
            # An explicit opt-in lets a success clear the cooldown early,
            # releasing the waiting thread before the scheduled end.
            cooldown.note_success()
            future.result(timeout=5)

        assert not cooldown.is_active()


class TestSessionCooldown:
    def test_429_engages_shared_cooldown_with_retry_after(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: clock.advance(seconds))
        session = make_session(clock, self_advancing_sleep(clock), cooldown=cooldown)
        handler = mock.Mock(return_value=make_response(429, {'Retry-After': '10'}))

        with mock.patch.object(requests.Session, 'send', side_effect=handler):
            with pytest.raises(RateLimitError) as excinfo:
                session.request('GET', 'http://test.com')

        assert excinfo.value.retry_after == 10
        assert cooldown.is_active(CooldownReason.rate_limited)
        assert cooldown.remaining(CooldownReason.rate_limited) == pytest.approx(10)
        assert not cooldown.is_active(CooldownReason.connection_failure)
        handler.assert_called_once()
        session.close()

    def test_429_without_retry_after_uses_default_delay(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: clock.advance(seconds))
        session = make_session(clock, self_advancing_sleep(clock), cooldown=cooldown)
        handler = mock.Mock(return_value=make_response(429))

        with mock.patch.object(requests.Session, 'send', side_effect=handler):
            with pytest.raises(RateLimitError):
                session.request('GET', 'http://test.com')

        assert cooldown.remaining(CooldownReason.rate_limited) == pytest.approx(DEFAULT_RATE_LIMIT_DELAY)
        session.close()

    def test_429_honors_http_date_retry_after(self):
        clock = FakeClock(start=1000.0)
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: clock.advance(seconds))
        session = make_session(clock, self_advancing_sleep(clock), cooldown=cooldown)
        retry_after_date = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(clock() + 30))
        handler = mock.Mock(return_value=make_response(429, {'Retry-After': retry_after_date}))

        with mock.patch.object(requests.Session, 'send', side_effect=handler):
            with pytest.raises(RateLimitError):
                session.request('GET', 'http://test.com')

        assert cooldown.remaining(CooldownReason.rate_limited) == pytest.approx(30, abs=1)
        session.close()

    def test_connection_failure_uses_connection_backoff_reason(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: clock.advance(seconds))
        session = make_session(
            clock,
            self_advancing_sleep(clock),
            cooldown=cooldown,
            retries=2,
            backoff=0.5,
        )
        engage_calls = []
        original_engage = cooldown.engage
        cooldown.engage = lambda seconds, reason: (
            engage_calls.append((seconds, reason)) or original_engage(seconds, reason)
        )

        def fail(request, **kwargs):
            raise RequestsConnectionError('NewConnectionError: failed to connect')

        with mock.patch.object(requests.Session, 'send', side_effect=fail):
            with pytest.raises(WaybackRetryError):
                session.request('GET', 'http://test.com')

        # Backoff is attributed to connection failures, never to rate limiting.
        assert all(reason != CooldownReason.rate_limited for _, reason in engage_calls)
        # Exponential backoff (0.5 * 2**0) is engaged through the shared state.
        assert (0.5, CooldownReason.connection_failure) in engage_calls
        session.close()

    def test_success_reports_to_cooldown_but_keeps_default_policy(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=lambda seconds: clock.advance(seconds))
        cooldown.engage(10, CooldownReason.rate_limited)
        session = make_session(clock, self_advancing_sleep(clock), cooldown=cooldown)
        handler = mock.Mock(return_value=make_response(200))

        with mock.patch.object(requests.Session, 'send', side_effect=handler):
            with mock.patch.object(cooldown, 'note_success', wraps=cooldown.note_success) as note_success:
                response = session.request('GET', 'http://test.com')

        assert response.status_code == 200
        note_success.assert_called_once()
        assert not cooldown.clear_on_success
        session.close()

    def test_success_clears_cooldown_when_policy_enables_it(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(
            clear_on_success=True,
            clock=clock,
            sleep=lambda seconds: clock.advance(seconds),
        )
        cooldown.engage(10, CooldownReason.rate_limited)
        session = make_session(clock, self_advancing_sleep(clock), cooldown=cooldown)
        handler = mock.Mock(return_value=make_response(200))

        with mock.patch.object(requests.Session, 'send', side_effect=handler):
            with mock.patch.object(cooldown, 'clear', wraps=cooldown.clear) as clear_mock:
                session.request('GET', 'http://test.com')

        clear_mock.assert_called()
        assert not cooldown.is_active()
        session.close()

    def test_session_parameter_enables_clear_on_success_policy(self):
        clock = FakeClock()
        session = make_session(
            clock,
            self_advancing_sleep(clock),
            clear_cooldown_on_success=True,
        )
        assert session.cooldown.clear_on_success
        session.close()

    def test_cancel_context_cleans_up_after_exit(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=self_advancing_sleep(clock))
        session = make_session(clock, self_advancing_sleep(clock), cooldown=cooldown)
        handler = mock.Mock(return_value=make_response(200))
        cancellation = threading.Event()
        cancellation.set()

        # Inside the context a set event would cancel requests...
        with session.cancel_with(cancellation):
            assert session._cancellation() is cancellation
        # ...but once the context exits, the per-thread association is removed
        # so later requests on the same thread behave normally.
        assert session._cancellation() is None

        with mock.patch.object(requests.Session, 'send', side_effect=handler):
            response = session.request('GET', 'http://test.com')

        assert response.status_code == 200
        handler.assert_called_once()
        session.close()

    def test_preset_cancellation_stops_request_without_sending(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=bounded_real_sleep())
        cooldown.engage(10, CooldownReason.rate_limited)
        session = make_session(clock, bounded_real_sleep(), cooldown=cooldown)
        handler = mock.Mock(return_value=make_response(200))
        cancellation = threading.Event()
        cancellation.set()

        with mock.patch.object(requests.Session, 'send', side_effect=handler):
            with session.cancel_with(cancellation):
                with pytest.raises(RequestCancelled):
                    session.request('GET', 'http://test.com')

        handler.assert_not_called()
        # The shared cooldown is untouched by the cancellation.
        assert cooldown.remaining(CooldownReason.rate_limited) == pytest.approx(10)
        session.close()

    def test_cancellation_during_wait_keeps_cooldown_for_others(self):
        clock = FakeClock()
        cooldown = ServiceCooldown(clock=clock, sleep=bounded_real_sleep())
        cooldown.engage(10, CooldownReason.rate_limited)
        session = make_session(clock, bounded_real_sleep(), cooldown=cooldown)
        cancellation = threading.Event()

        def cancelled_request():
            with session.cancel_with(cancellation):
                with pytest.raises(RequestCancelled):
                    session.request('GET', 'http://test.com')

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(cancelled_request)
            time.sleep(0.02)
            cancellation.set()
            future.result(timeout=5)

        # Another request on a different thread still has to wait out the
        # full cooldown before it is allowed to send.
        assert cooldown.remaining(CooldownReason.rate_limited) == pytest.approx(10)
        session.close()


class TestConcurrentCooldown:
    def test_waiting_threads_do_not_send_until_cooldown_elapses(self):
        clock = FakeClock()
        sleep = self_advancing_sleep(clock)
        # Two sessions share one cooldown, modeling multiple collector threads
        # using the same Wayback account.
        shared_cooldown = ServiceCooldown(clock=clock, sleep=sleep)
        session_a = make_session(clock, sleep, cooldown=shared_cooldown)
        session_b = make_session(clock, sleep, cooldown=shared_cooldown)

        send_times = []
        send_lock = threading.Lock()

        def handler(request, **kwargs):
            with send_lock:
                send_times.append(clock())
                rate_limited = len(send_times) == 1
            if rate_limited:
                # Only the first request trips the rate limit, for 10 seconds.
                return make_response(429, {'Retry-After': '10'})
            return make_response(200)

        def request_from(session, outcomes, name):
            try:
                response = session.request('GET', 'http://test.com')
                outcomes[name] = response.status_code
            except RateLimitError as error:
                outcomes[name] = error

        outcomes = {}
        with mock.patch.object(requests.Session, 'send', side_effect=handler):
            with ThreadPoolExecutor(max_workers=3) as executor:
                # Phase 1: deterministically trip the shared cooldown with a
                # single request before any concurrent waiters start.
                trigger = executor.submit(request_from, session_a, outcomes, 'trigger')
                trigger.result(timeout=5)

                assert isinstance(outcomes['trigger'], RateLimitError)
                assert shared_cooldown.is_active(CooldownReason.rate_limited)
                assert shared_cooldown.remaining(CooldownReason.rate_limited) == pytest.approx(10)

                # Phase 2: concurrent requests on both sessions must block on
                # the shared cooldown and only be sent once it elapses at t=10.
                # Their self-advancing sleeps move the fake clock forward
                # deterministically without any real waiting.
                futures = [
                    executor.submit(request_from, session_a, outcomes, 'waiter_a'),
                    executor.submit(request_from, session_b, outcomes, 'waiter_b'),
                ]
                for future in futures:
                    future.result(timeout=10)

        session_a.close()
        session_b.close()

        assert outcomes['waiter_a'] == 200
        assert outcomes['waiter_b'] == 200

        # Exactly one request went out before the cooldown (at t == 0), and
        # every concurrent request was held until the cooldown elapsed.
        assert len(send_times) == 3
        assert send_times[0] == pytest.approx(0)
        assert all(sent_at >= 10 for sent_at in send_times[1:])
