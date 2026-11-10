"""
Offline integration tests for the criteria-driven memento selection in
:meth:`wayback.WaybackClient.get_memento_near`.

These never touch the network: the CDX index and every memento playback
response are synthesized with requests_mock, so the selection process is
verified deterministically rather than against whatever the live archive
currently holds.
"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from wayback import Direction, NoCompatibleMementoError, RejectionReason
from wayback._client import CDX_SEARCH_URL
from wayback._models import CdxRecord
from wayback._utils import format_memento_url, format_timestamp

UTC = timezone.utc


def make_record(
    timestamp, *, statuscode=200, mimetype='text/html', original='http://example.com/', length=100, digest=None
):
    return CdxRecord(
        urlkey='com,example)/',
        timestamp=timestamp,
        original=original,
        mimetype=mimetype,
        statuscode=statuscode,
        digest=digest or f'digest-{format_timestamp(timestamp)}',
        length=length,
    )


def cdx_line(record):
    status = '-' if record.statuscode is None else str(record.statuscode)
    length = '-' if record.length is None else str(record.length)
    return ' '.join(
        [
            record.urlkey,
            format_timestamp(record.timestamp),
            record.original,
            record.mimetype,
            status,
            record.digest,
            length,
        ]
    )


def register_cdx(requests_mock, records):
    body = '\n'.join(cdx_line(record) for record in records)
    if records:
        body += '\n'
    requests_mock.get(CDX_SEARCH_URL, text=body)


def raw_url(record):
    return format_memento_url(record.original, record.timestamp, mode='id_')


def register_playable_memento(requests_mock, record, *, text='<html>ok</html>', content_type='text/html'):
    requests_mock.get(
        raw_url(record),
        status_code=200,
        headers={
            'Memento-Datetime': format_datetime(record.timestamp),
            'Content-Type': content_type,
        },
        text=text,
    )


def register_unplayable_memento(requests_mock, record):
    # A non-memento response carrying a Wayback runtime error, which the
    # client turns into a MementoPlaybackError.
    requests_mock.get(
        raw_url(record),
        status_code=200,
        headers={'X-Archive-Wayback-Runtime-Error': 'java.lang.RuntimeException: capture unavailable'},
        text='capture unavailable',
    )


def register_redirect_to(requests_mock, from_record, to_record):
    # A non-memento archival redirect (no Memento-Datetime header) pointing at
    # a different capture's memento URL.
    requests_mock.get(
        raw_url(from_record),
        status_code=302,
        headers={'Location': raw_url(to_record)},
        text='',
    )


def playback_requests(requests_mock):
    return [request for request in requests_mock.request_history if '/web/' in request.url]


def test_selects_acceptable_status_and_media_type(test_client, requests_mock):
    target = datetime(2020, 1, 10, tzinfo=UTC)
    login_redirect = make_record(datetime(2020, 1, 9, tzinfo=UTC), statuscode=302, digest='redirect')
    pdf = make_record(datetime(2020, 1, 11, tzinfo=UTC), mimetype='application/pdf', digest='pdf')
    good = make_record(datetime(2020, 1, 12, tzinfo=UTC), statuscode=200, mimetype='text/html', digest='good')
    register_cdx(requests_mock, [login_redirect, pdf, good])
    register_playable_memento(requests_mock, good)

    memento = test_client.get_memento_near(
        'http://example.com/',
        target,
        allowed_status=200,
        media_types='text/html',
    )

    assert memento.timestamp == good.timestamp
    assert memento.status_code == 200
    # The rejected redirect/pdf captures were never requested for playback.
    requested = [request.url for request in playback_requests(requests_mock)]
    assert requested == [raw_url(good)]


def test_direction_before_does_not_relax_into_the_future(test_client, requests_mock):
    target = datetime(2020, 1, 10, tzinfo=UTC)
    future = make_record(datetime(2020, 1, 11, tzinfo=UTC), digest='future')
    register_cdx(requests_mock, [future])

    with pytest.raises(NoCompatibleMementoError) as exc_info:
        test_client.get_memento_near('http://example.com/', target, direction=Direction.before)

    assert all(reason == RejectionReason.time_direction.value for _label, reason in exc_info.value.rejections)
    # It must fail at the CDX stage and never attempt playback.
    assert playback_requests(requests_mock) == []


def test_max_distance_bounds_the_cdx_query_and_filters(test_client, requests_mock):
    target = datetime(2020, 1, 10, tzinfo=UTC)
    far = make_record(datetime(2020, 1, 20, tzinfo=UTC), digest='far')
    register_cdx(requests_mock, [far])

    with pytest.raises(NoCompatibleMementoError) as exc_info:
        test_client.get_memento_near(
            'http://example.com/',
            target,
            max_distance=timedelta(days=2),
        )

    assert exc_info.value.rejections[0][1] == RejectionReason.distance.value
    cdx_request = requests_mock.last_request
    assert 'from' in cdx_request.qs
    assert 'to' in cdx_request.qs
    assert playback_requests(requests_mock) == []


def test_payload_predicate_is_enforced(test_client, requests_mock):
    target = datetime(2020, 1, 10, tzinfo=UTC)
    small = make_record(datetime(2020, 1, 9, tzinfo=UTC), length=10, digest='small')
    large = make_record(datetime(2020, 1, 11, tzinfo=UTC), length=5000, digest='large')
    register_cdx(requests_mock, [small, large])
    register_playable_memento(requests_mock, large)

    memento = test_client.get_memento_near(
        'http://example.com/',
        target,
        payload=lambda record: record.length is not None and record.length >= 1000,
    )

    assert memento.timestamp == large.timestamp


def test_equal_distance_prefers_the_earlier_capture(test_client, requests_mock):
    target = datetime(2020, 1, 10, tzinfo=UTC)
    before = make_record(datetime(2020, 1, 9, tzinfo=UTC), digest='before')
    after = make_record(datetime(2020, 1, 11, tzinfo=UTC), digest='after')
    register_cdx(requests_mock, [after, before])
    register_playable_memento(requests_mock, before)
    register_playable_memento(requests_mock, after)

    memento = test_client.get_memento_near('http://example.com/', target)

    assert memento.timestamp == before.timestamp
    assert playback_requests(requests_mock)[0].url == raw_url(before)


def test_skips_unplayable_candidate_and_uses_next(test_client, requests_mock):
    target = datetime(2020, 1, 10, tzinfo=UTC)
    first = make_record(datetime(2020, 1, 9, tzinfo=UTC), digest='first')
    second = make_record(datetime(2020, 1, 11, tzinfo=UTC), digest='second')
    register_cdx(requests_mock, [first, second])
    register_unplayable_memento(requests_mock, first)
    register_playable_memento(requests_mock, second)

    memento = test_client.get_memento_near('http://example.com/', target)

    assert memento.timestamp == second.timestamp
    requested = [request.url for request in playback_requests(requests_mock)]
    assert requested == [raw_url(first), raw_url(second)]


def test_raises_when_every_candidate_fails_playback(test_client, requests_mock):
    target = datetime(2020, 1, 10, tzinfo=UTC)
    first = make_record(datetime(2020, 1, 9, tzinfo=UTC), digest='first')
    second = make_record(datetime(2020, 1, 11, tzinfo=UTC), digest='second')
    register_cdx(requests_mock, [first, second])
    register_unplayable_memento(requests_mock, first)
    register_unplayable_memento(requests_mock, second)

    with pytest.raises(NoCompatibleMementoError) as exc_info:
        test_client.get_memento_near('http://example.com/', target)

    reasons = [reason for _label, reason in exc_info.value.rejections]
    assert reasons == [RejectionReason.playback.value, RejectionReason.playback.value]


def test_verifies_timestamp_actually_played_back(test_client, requests_mock):
    target = datetime(2020, 1, 10, tzinfo=UTC)
    # First-ranked candidate is one day before the target and within window.
    first = make_record(datetime(2020, 1, 9, tzinfo=UTC), digest='first')
    # Its playback redirects to a capture 6 days before target: outside the
    # 5-day window, even though it is only 5 days away from the requested
    # capture (so get_memento itself is willing to follow the redirect).
    redirected = make_record(datetime(2020, 1, 4, tzinfo=UTC), digest='redirected')
    second = make_record(datetime(2020, 1, 11, tzinfo=UTC), digest='second')
    register_cdx(requests_mock, [first, second])
    register_redirect_to(requests_mock, first, redirected)
    register_playable_memento(requests_mock, redirected)
    register_playable_memento(requests_mock, second)

    memento = test_client.get_memento_near(
        'http://example.com/',
        target,
        max_distance=timedelta(days=5),
    )

    # The redirected capture failed the played-back timestamp check, so the
    # next eligible candidate was used instead.
    assert memento.timestamp == second.timestamp
    requested = [request.url for request in playback_requests(requests_mock)]
    assert raw_url(first) in requested
    assert raw_url(redirected) in requested
    assert raw_url(second) in requested


def test_played_back_timestamp_in_wrong_direction_is_rejected(test_client, requests_mock):
    target = datetime(2020, 1, 10, tzinfo=UTC)
    # Only a before-direction request is allowed; the sole CDX candidate is at
    # the target, but playback redirects to a future capture.
    at_target = make_record(target, digest='at-target')
    future = make_record(datetime(2020, 1, 12, tzinfo=UTC), digest='future')
    register_cdx(requests_mock, [at_target])
    register_redirect_to(requests_mock, at_target, future)
    register_playable_memento(requests_mock, future)

    with pytest.raises(NoCompatibleMementoError) as exc_info:
        test_client.get_memento_near('http://example.com/', target, direction=Direction.before)

    reasons = [reason for _label, reason in exc_info.value.rejections]
    assert RejectionReason.timestamp_mismatch.value in reasons


def test_empty_cdx_index_reports_no_candidates(test_client, requests_mock):
    register_cdx(requests_mock, [])

    with pytest.raises(NoCompatibleMementoError) as exc_info:
        test_client.get_memento_near('http://example.com/', datetime(2020, 1, 10, tzinfo=UTC))

    assert exc_info.value.rejections == []
    assert 'no candidates' in str(exc_info.value).lower()
    assert playback_requests(requests_mock) == []


def test_string_direction_is_accepted_end_to_end(test_client, requests_mock):
    target = datetime(2020, 1, 10, tzinfo=UTC)
    past = make_record(datetime(2020, 1, 9, tzinfo=UTC), digest='past')
    future = make_record(datetime(2020, 1, 11, tzinfo=UTC), digest='future')
    register_cdx(requests_mock, [past, future])
    register_playable_memento(requests_mock, past)

    memento = test_client.get_memento_near('http://example.com/', target, direction='before')

    assert memento.timestamp == past.timestamp
    requested = [request.url for request in playback_requests(requests_mock)]
    assert requested == [raw_url(past)]
