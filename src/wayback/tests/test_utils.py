from datetime import datetime, timezone
import email.utils
import pytest
import time
from .._utils import memento_url_data, RateLimit, parse_retry_after


class TestMementoUrlData:
    def test_extracts_url(self):
        url, timestamp, mode = memento_url_data(
            'https://web.archive.org/web/20170813195036/https://arpa-e.energy.gov/?q=engage/events-workshops'
        )
        assert url == 'https://arpa-e.energy.gov/?q=engage/events-workshops'
        assert timestamp == datetime(2017, 8, 13, 19, 50, 36, tzinfo=timezone.utc)
        assert mode == ''

        url, timestamp, mode = memento_url_data(
            'https://web.archive.org/web/20170813195036id_/https://arpa-e.energy.gov/?q=engage/events-workshops'
        )
        assert url == 'https://arpa-e.energy.gov/?q=engage/events-workshops'
        assert timestamp == datetime(2017, 8, 13, 19, 50, 36, tzinfo=timezone.utc)
        assert mode == 'id_'

    def test_decodes_url(self):
        url, _, _ = memento_url_data(
            'https://web.archive.org/web/20150930233055id_/'
            'http%3A%2F%2Fwww.epa.gov%2Fenvironmentaljustice%2Fgrants%2Fej-smgrants.html%3Futm'
        )
        assert url == 'http://www.epa.gov/environmentaljustice/grants/ej-smgrants.html?utm'

    def test_does_not_decode_query(self):
        url, _, _ = memento_url_data(
            'https://web.archive.org/web/20170813195036/https://arpa-e.energy.gov/?q=engage%2Fevents-workshops'
        )
        assert url == 'https://arpa-e.energy.gov/?q=engage%2Fevents-workshops'

    def test_raises_for_non_memento_urls(self):
        with pytest.raises(ValueError):
            memento_url_data('http://whatever.com')

    def test_raises_for_non_string_input(self):
        with pytest.raises(TypeError):
            memento_url_data(None)


class TestRateLimit:
    def test_call_per_seconds(self):
        """
        Test that the rate limit is accurately applied. It also checks that two
        rate limits applied sequentially do not interfere with another.
        """
        limit3 = RateLimit(per_second=3)
        limit1 = RateLimit(per_second=1)
        limit0 = RateLimit(per_second=0)

        start_time = time.time()
        for i in range(4):
            with limit3:
                pass
        assert 1.0 <= time.time() - start_time <= 1.1

        start_time = time.time()
        for i in range(3):
            with limit1:
                pass
        assert 2.0 <= time.time() - start_time <= 2.1

        start_time = time.time()
        for i in range(3):
            with limit0:
                pass
        assert 0 <= time.time() - start_time <= 0.1

    def test_simultaneous_ratelimits(self):
        """Check that multiple rate limits do not interfere with another."""
        limit1 = RateLimit(per_second=1)
        limit3 = RateLimit(per_second=3)
        start_time = time.time()
        # The first loop should take 1 second, as it waits on the sim1 lock,
        # the second loop 0.66 seconds, since it waits twice on sim2.
        for i in range(2):
            with limit1:
                for j in range(3):
                    with limit3:
                        pass
        assert 1.66 <= time.time() - start_time <= 1.7

    def test_injected_clock_is_deterministic(self):
        """A rate limit with an injected clock sleeps based on logical time."""
        current = [100.0]
        slept = []

        def clock():
            return current[0]

        def sleep(seconds):
            slept.append(seconds)
            current[0] += seconds

        limit = RateLimit(per_second=2, clock=clock)
        limit._sleep = sleep

        limit.wait()  # First call never blocks.
        assert slept == []

        limit.wait()  # Minimum wait for 2 calls/second is 0.5 seconds.
        assert slept == [pytest.approx(0.5)]
        assert current[0] == pytest.approx(100.5)


class TestParseRetryAfter:
    def test_seconds(self):
        assert 30 == parse_retry_after('30')

    def test_date(self):
        result = parse_retry_after(email.utils.formatdate(time.time() + 30))
        assert 29 <= result <= 31

    def test_no_input(self):
        assert parse_retry_after(None) is None

    def test_invalid_input(self):
        assert 0 == parse_retry_after('hello!')

    def test_negative_seconds(self):
        assert 0 == parse_retry_after('-10')

    def test_negative_date(self):
        result = parse_retry_after(email.utils.formatdate(time.time() - 30))
        assert 0 == result

    def test_date_with_injected_clock(self):
        # A fixed clock makes the date-based calculation deterministic.
        fixed_now = 1_000_000.0
        result = parse_retry_after(
            email.utils.formatdate(fixed_now + 30),
            now=lambda: fixed_now,
        )
        assert 30 == result

    def test_past_date_with_injected_clock_is_zero(self):
        fixed_now = 1_000_000.0
        result = parse_retry_after(
            email.utils.formatdate(fixed_now - 30),
            now=lambda: fixed_now,
        )
        assert 0 == result
