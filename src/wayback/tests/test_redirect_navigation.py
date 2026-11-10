"""
Tests for interpreting the navigation history of played-back mementos.

Wayback playback can involve two interleaved kinds of redirect:

* service-level adjustments, where the Wayback Machine itself switches to a
  different snapshot (no ``Memento-Datetime`` header), and
* historical redirects, where the archived web server returned a 3xx response
  at crawl time (a real memento with ``Memento-Datetime``).

These tests use hand-built mock response chains so the two kinds can be
interleaved deterministically without any network access.
"""

from datetime import datetime, timedelta, timezone
import pytest
from .._models import RedirectType
from ..exceptions import MementoRedirectBoundaryError


T0 = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2020, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2020, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
T3 = datetime(2020, 1, 1, 3, 0, 0, tzinfo=timezone.utc)

START = 'http://example.com/start'
PAGE_A = 'http://example.com/a'
PAGE_B = 'http://example.com/b'
OTHER_PAGE = 'http://other.example.net/c'


def memento_url(original, timestamp):
    return f'https://web.archive.org/web/{timestamp:%Y%m%d%H%M%S}id_/{original}'


def register_redirect(mocker, source_url, status, location, *, memento, timestamp=None):
    headers = {'Location': location}
    if memento:
        headers['Memento-Datetime'] = f'{timestamp:%a, %d %b %Y %H:%M:%S} GMT'
    mocker.get(source_url, status_code=status, headers=headers, text='')


def register_final_page(mocker, source_url, timestamp):
    mocker.get(
        source_url,
        status_code=200,
        headers={'Memento-Datetime': f'{timestamp:%a, %d %b %Y %H:%M:%S} GMT'},
        text='<html>final page</html>',
    )


def register_interleaved_chain(mocker, *, third_target=PAGE_B):
    """
    Build a chain that alternates service and historical redirects:

        S0 (service 302)     -> S1
        S1 (historical 301)  -> memento URL of /a at T1
        S2 (service 302)     -> S3
        S3 (historical 301)  -> memento URL of third_target at T2
        S4 (service 302)     -> S5
        S5 (memento 200, final page)
    """
    s0 = memento_url(START, T0)
    s1 = memento_url(START, T1)
    s2 = memento_url(PAGE_A, T1)
    s3 = memento_url(PAGE_A, T2)
    s4 = memento_url(third_target, T2)
    s5 = memento_url(third_target, T3)

    register_redirect(mocker, s0, 302, s1, memento=False)
    register_redirect(mocker, s1, 301, s2, memento=True, timestamp=T1)
    register_redirect(mocker, s2, 302, s3, memento=False)
    register_redirect(mocker, s3, 301, s4, memento=True, timestamp=T2)
    register_redirect(mocker, s4, 302, s5, memento=False)
    register_final_page(mocker, s5, T3)

    return [s0, s1, s2, s3, s4, s5]


def requested_urls(mocker):
    return [request.url for request in mocker.request_history]


def test_navigation_separates_service_and_historical_redirects(test_client, requests_mock):
    urls = register_interleaved_chain(requests_mock)

    memento = test_client.get_memento(urls[0], exact=False)

    # The final page is the end of the chain.
    assert memento.url == PAGE_B
    assert memento.memento_url == urls[5]

    # `debug_history` stays a flat list of every Wayback URL, while `history`
    # only retains the redirects that were themselves mementos.
    assert memento.debug_history == tuple(urls[:5])
    assert [step.memento_url for step in memento.history] == [urls[1], urls[3]]

    # `navigation` explains every hop and keeps the two kinds separate.
    assert [step.type for step in memento.navigation] == [
        RedirectType.service,
        RedirectType.historical,
        RedirectType.service,
        RedirectType.historical,
        RedirectType.service,
    ]
    assert tuple(step.memento_url for step in memento.navigation) == memento.debug_history


def test_navigation_steps_preserve_capture_times_and_original_targets(test_client, requests_mock):
    urls = register_interleaved_chain(requests_mock)

    memento = test_client.get_memento(urls[0], exact=False)
    steps = memento.navigation

    # First step: a service-level switch between snapshots of the same URL.
    assert steps[0].type == RedirectType.service
    assert steps[0].memento_url == urls[0]
    assert steps[0].url == START
    assert steps[0].timestamp == T0
    assert steps[0].status_code == 302
    assert steps[0].target_memento_url == urls[1]
    assert steps[0].target_url == START
    assert steps[0].target_timestamp == T1

    # Second step: the original site's own 301, pointing at a different URL.
    assert steps[1].type == RedirectType.historical
    assert steps[1].memento_url == urls[1]
    assert steps[1].url == START
    assert steps[1].timestamp == T1
    assert steps[1].status_code == 301
    assert steps[1].target_memento_url == urls[2]
    assert steps[1].target_url == PAGE_A
    assert steps[1].target_timestamp == T1

    # The later historical step likewise keeps its real capture time.
    assert steps[3].type == RedirectType.historical
    assert steps[3].url == PAGE_A
    assert steps[3].timestamp == T2
    assert steps[3].target_url == PAGE_B
    assert steps[3].target_timestamp == T2


def test_historical_redirect_window_stops_before_drifting_too_far(test_client, requests_mock):
    urls = register_interleaved_chain(requests_mock)

    # The first historical redirect drifts one hour (allowed), the second two
    # hours (outside the 90-minute window), so playback must stop at the second
    # historical redirect instead of following it.
    with pytest.raises(MementoRedirectBoundaryError) as excinfo:
        test_client.get_memento(urls[0], exact=False, historical_redirect_window=timedelta(minutes=90))

    error = excinfo.value
    assert error.boundary == 'time'

    # The evidence gathered so far is retained, including the boundary step.
    assert [step.type for step in error.navigation] == [
        RedirectType.service,
        RedirectType.historical,
        RedirectType.service,
        RedirectType.historical,
    ]
    assert error.navigation[-1].target_url == PAGE_B
    assert error.navigation[-1].target_timestamp == T2
    assert [memento.memento_url for memento in error.history] == [urls[1], urls[3]]

    # The last loaded memento is the boundary-crossing redirect itself.
    assert error.memento.status_code == 301
    assert error.memento.memento_url == urls[3]

    # Nothing beyond the boundary was ever requested.
    assert requested_urls(requests_mock)[-1] == urls[3]
    assert urls[4] not in requested_urls(requests_mock)
    assert urls[5] not in requested_urls(requests_mock)


def test_historical_redirect_window_accepts_integer_seconds(test_client, requests_mock):
    urls = register_interleaved_chain(requests_mock)

    # A generous integer window behaves the same as the default (no boundary).
    memento = test_client.get_memento(urls[0], exact=False, historical_redirect_window=24 * 60 * 60)
    assert memento.memento_url == urls[5]
    assert len(memento.navigation) == 5


def test_same_site_stops_cross_site_historical_redirect(test_client, requests_mock):
    urls = register_interleaved_chain(requests_mock, third_target=OTHER_PAGE)

    with pytest.raises(MementoRedirectBoundaryError) as excinfo:
        test_client.get_memento(urls[0], exact=False, same_site=True)

    error = excinfo.value
    assert error.boundary == 'site'
    assert error.navigation[-1].url == PAGE_A
    assert error.navigation[-1].target_url == OTHER_PAGE
    assert error.memento.memento_url == urls[3]

    # The off-site target and everything after it are never requested, so the
    # client does not silently wander onto another (possibly present-day) site.
    requested = requested_urls(requests_mock)
    assert requested[-1] == urls[3]
    assert not any('other.example.net' in requested_url for requested_url in requested)


def test_same_site_ignores_protocol_and_www_prefix(test_client, requests_mock):
    www_url = 'https://www.example.com/a'
    s0 = memento_url(START, T0)
    s1 = memento_url(START, T1)
    s2 = memento_url(www_url, T1)
    s3 = memento_url(www_url, T2)

    register_redirect(requests_mock, s0, 302, s1, memento=False)
    register_redirect(requests_mock, s1, 301, s2, memento=True, timestamp=T1)
    register_redirect(requests_mock, s2, 302, s3, memento=False)
    register_final_page(requests_mock, s3, T2)

    memento = test_client.get_memento(s0, exact=False, same_site=True)
    assert memento.memento_url == s3
    historical_targets = [step.target_url for step in memento.navigation if step.type == RedirectType.historical]
    assert all(target == www_url for target in historical_targets)


def test_service_redirects_are_not_subject_to_historical_boundaries(test_client, requests_mock):
    urls = register_interleaved_chain(requests_mock)

    # A zero-width historical window would reject the historical hops, but the
    # service-level snapshot adjustments must still be governed only by
    # `target_window`, not this setting. The first historical hop trips it.
    with pytest.raises(MementoRedirectBoundaryError) as excinfo:
        test_client.get_memento(urls[0], exact=False, historical_redirect_window=0)

    error = excinfo.value
    assert error.boundary == 'time'
    assert [step.type for step in error.navigation] == [
        RedirectType.service,
        RedirectType.historical,
    ]


def test_navigation_when_not_following_historical_redirects(test_client, requests_mock):
    urls = register_interleaved_chain(requests_mock)

    memento = test_client.get_memento(urls[0], exact=False, follow_redirects=False)

    # The first response is only a service adjustment, so it is followed until
    # the first actual memento (the historical 301), which is then returned.
    assert memento.memento_url == urls[1]
    assert memento.status_code == 301
    assert len(memento.history) == 0
    assert [step.type for step in memento.navigation] == [RedirectType.service]
    assert memento.debug_history == (urls[0],)
    assert requested_urls(requests_mock) == urls[:2]
