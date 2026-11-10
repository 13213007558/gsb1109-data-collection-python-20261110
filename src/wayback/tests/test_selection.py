from datetime import date, datetime, timedelta, timezone

import pytest

from wayback import (
    Direction,
    MementoCriteria,
    NoCompatibleMementoError,
    RejectionReason,
    TimeRelation,
    build_criteria,
    rank_mementos,
    select_memento,
)
from wayback._models import CdxRecord
from wayback._selection import (
    evaluate_candidate,
    media_type_matches,
    normalize_media_type,
    time_relation,
)

UTC = timezone.utc


def make_record(
    timestamp, *, statuscode=200, mimetype='text/html', original='http://example.com/', length=100, digest='D'
):
    return CdxRecord(
        urlkey='com,example)/',
        timestamp=timestamp,
        original=original,
        mimetype=mimetype,
        statuscode=statuscode,
        digest=digest,
        length=length,
    )


def ts(year, month, day, hour=12):
    return datetime(year, month, day, hour, tzinfo=UTC)


def test_normalize_media_type_strips_parameters_and_case():
    assert normalize_media_type('Text/HTML; charset=utf-8') == 'text/html'
    assert normalize_media_type(None) == ''


@pytest.mark.parametrize(
    'mimetype,allowed,expected',
    [
        ('text/html', ['text/html'], True),
        ('text/html', ['TEXT/HTML'], True),
        ('text/html; charset=utf-8', ['text/html'], True),
        ('application/pdf', ['text/*'], False),
        ('text/html', ['text/*'], True),
        ('application/pdf', ['*'], True),
        ('application/pdf', ['*/*'], True),
        ('image/png', ['text/html', 'image/*'], True),
        ('text/html', None, True),
        ('unk', ['text/html'], False),
    ],
)
def test_media_type_matches(mimetype, allowed, expected):
    assert media_type_matches(mimetype, allowed) is expected


def test_build_criteria_normalizes_inputs():
    criteria = build_criteria(
        'before',
        allowed_status=[200, '200', 301],
        media_types=['text/html', 'application/pdf'],
    )
    assert isinstance(criteria, MementoCriteria)
    assert criteria.direction == Direction.before
    assert criteria.allowed_status == frozenset({200, 301})
    assert criteria.media_types == ('text/html', 'application/pdf')


def test_build_criteria_rejects_bad_direction():
    with pytest.raises(ValueError):
        build_criteria('sideways')


def test_build_criteria_rejects_non_callable_payload():
    with pytest.raises(TypeError):
        build_criteria(payload='not-callable')


def test_time_relation():
    target = ts(2020, 1, 10)
    assert time_relation(ts(2020, 1, 9), target) == TimeRelation.before
    assert time_relation(ts(2020, 1, 11), target) == TimeRelation.after
    assert time_relation(target, target) == TimeRelation.at


def test_evaluate_candidate_accepts_matching_record():
    target = ts(2020, 1, 10)
    criteria = build_criteria('nearest', allowed_status={200}, media_types=['text/html'])
    assert evaluate_candidate(make_record(target), target, criteria) is None


def test_evaluate_candidate_time_direction():
    target = ts(2020, 1, 10)
    criteria = build_criteria('before')
    future = make_record(ts(2020, 1, 11))
    assert evaluate_candidate(future, target, criteria) == RejectionReason.time_direction

    criteria = build_criteria('after')
    past = make_record(ts(2020, 1, 9))
    assert evaluate_candidate(past, target, criteria) == RejectionReason.time_direction


def test_evaluate_candidate_distance():
    target = ts(2020, 1, 10)
    criteria = build_criteria('nearest')
    record = make_record(ts(2020, 1, 20))
    assert evaluate_candidate(record, target, criteria, timedelta(days=5)) == RejectionReason.distance


def test_evaluate_candidate_status_and_media_type():
    target = ts(2020, 1, 10)
    redirect = make_record(target, statuscode=302)
    criteria = build_criteria(allowed_status={200})
    assert evaluate_candidate(redirect, target, criteria) == RejectionReason.status_code

    pdf = make_record(target, mimetype='application/pdf')
    criteria = build_criteria(media_types=['text/html'])
    assert evaluate_candidate(pdf, target, criteria) == RejectionReason.media_type


def test_evaluate_candidate_payload_predicate():
    target = ts(2020, 1, 10)
    tiny = make_record(target, length=10)
    criteria = build_criteria(payload=lambda record: record.length and record.length >= 100)
    assert evaluate_candidate(tiny, target, criteria) == RejectionReason.payload


def test_rank_orders_by_distance_and_uses_stable_tie_rule():
    target = ts(2020, 1, 10)
    records = [
        make_record(ts(2020, 1, 12), digest='far'),  # +2 days
        make_record(ts(2020, 1, 8), digest='near-before'),  # -2 days
        make_record(ts(2020, 1, 11), digest='after'),  # +1 day
        make_record(ts(2020, 1, 9), digest='before'),  # -1 day
    ]

    eligible, rejected = rank_mementos(records, target)
    assert rejected == []
    assert [selection.record.digest for selection in eligible] == ['before', 'after', 'near-before', 'far']

    # Equal-distance tie: the earlier (before) capture wins over the future one.
    tied = [
        make_record(ts(2020, 1, 11), digest='after'),
        make_record(ts(2020, 1, 9), digest='before'),
    ]
    eligible, _ = rank_mementos(tied, target)
    assert eligible[0].record.digest == 'before'
    assert eligible[0].distance == timedelta(days=1)
    assert eligible[0].relation == TimeRelation.before


def test_rank_is_order_independent_and_total():
    target = ts(2020, 1, 10)
    records = [
        make_record(ts(2020, 1, 9), digest='b'),
        make_record(ts(2020, 1, 9), digest='a'),
        make_record(ts(2020, 1, 11), digest='c'),
    ]
    first, _ = rank_mementos(records, target)
    second, _ = rank_mementos(list(reversed(records)), target)
    assert [s.record.digest for s in first] == [s.record.digest for s in second]
    # Same timestamp + distance is disambiguated by digest.
    assert first[0].record.digest == 'a'


def test_rank_exact_match_wins():
    target = ts(2020, 1, 10)
    records = [
        make_record(ts(2020, 1, 10), digest='exact'),
        make_record(ts(2020, 1, 9), digest='before'),
    ]
    eligible, _ = rank_mementos(records, target)
    assert eligible[0].record.digest == 'exact'
    assert eligible[0].relation == TimeRelation.at
    assert eligible[0].distance == timedelta(0)


def test_rank_collects_rejection_reasons():
    target = ts(2020, 1, 10)
    records = [
        make_record(ts(2020, 1, 20), statuscode=302, digest='redirect'),
        make_record(ts(2020, 1, 11), mimetype='application/pdf', digest='pdf'),
    ]
    criteria = build_criteria('before', allowed_status={200}, media_types=['text/html'])
    eligible, rejected = rank_mementos(records, target, criteria)
    assert eligible == []
    reasons = {record.digest: reason for record, reason in rejected}
    # The future redirect is excluded on direction before status is even checked.
    assert reasons['redirect'] == RejectionReason.time_direction
    assert reasons['pdf'] == RejectionReason.time_direction


def test_select_returns_best_candidate():
    target = ts(2020, 1, 10)
    records = [
        make_record(ts(2020, 1, 12), statuscode=302, digest='redirect'),
        make_record(ts(2020, 1, 9), statuscode=200, digest='good'),
    ]
    selection = select_memento(records, target, build_criteria(allowed_status={200}), url='http://example.com/')
    assert selection.record.digest == 'good'
    assert selection.target == target


def test_select_does_not_relax_into_the_future():
    target = ts(2020, 1, 10)
    # Only future captures exist, but the request is limited to the past.
    records = [
        make_record(ts(2020, 1, 11), statuscode=200, digest='future-1'),
        make_record(ts(2020, 1, 12), statuscode=200, digest='future-2'),
    ]
    with pytest.raises(NoCompatibleMementoError) as exc_info:
        select_memento(records, target, build_criteria('before'), url='http://example.com/')

    error = exc_info.value
    assert error.url == 'http://example.com/'
    assert error.target == target
    assert all(reason == RejectionReason.time_direction.value for _label, reason in error.rejections)
    assert len(error.rejections) == 2
    assert 'time_direction' in str(error)


def test_select_error_breaks_down_each_restriction_category():
    target = ts(2020, 1, 10)
    records = [
        make_record(target, statuscode=302, digest='redirect'),
        make_record(target, mimetype='application/pdf', digest='pdf'),
        make_record(target, length=5, digest='tiny'),
        make_record(ts(2020, 1, 20), digest='far'),
    ]
    criteria = build_criteria(
        'nearest',
        allowed_status={200},
        media_types=['text/html'],
        payload=lambda record: record.length and record.length >= 100,
    )
    with pytest.raises(NoCompatibleMementoError) as exc_info:
        select_memento(records, target, criteria, max_distance=timedelta(days=5))

    reasons = [reason for _label, reason in exc_info.value.rejections]
    assert sorted(reasons) == sorted(
        [
            RejectionReason.status_code.value,
            RejectionReason.media_type.value,
            RejectionReason.payload.value,
            RejectionReason.distance.value,
        ]
    )
    message = str(exc_info.value)
    for category in ('status_code', 'media_type', 'payload', 'distance'):
        assert category in message


def test_select_with_no_candidates_explains_empty_index():
    with pytest.raises(NoCompatibleMementoError) as exc_info:
        select_memento([], ts(2020, 1, 10), build_criteria(), url='http://example.com/')
    assert 'no candidates' in str(exc_info.value).lower()


def test_select_accepts_date_and_string_targets():
    target_date = date(2020, 1, 10)
    record = make_record(datetime(2020, 1, 10, 0, tzinfo=UTC))
    selection = select_memento([record], target_date)
    assert selection.record == record

    selection = select_memento([record], '20200110000000')
    assert selection.record == record
