from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from wayback import (
    CdxRecord,
    CoverageGap,
    DuplicatePolicy,
    SampleSelection,
    SelectionReason,
    WaybackClient,
    sample_snapshots,
)


UTC = timezone.utc
BASE = datetime(2020, 1, 1, tzinfo=UTC)


def make_record(day, digest, hour=0):
    timestamp = BASE + timedelta(days=day, hours=hour)
    return CdxRecord(
        urlkey='com,example)/',
        timestamp=timestamp,
        original='http://example.com',
        mimetype='text/html',
        statuscode=200,
        digest=digest,
        length=100,
    )


# A deliberately non-uniform series: three same-day captures, then single
# captures separated by gaps of 10, 20, 60, 1 and 30 days, with two more
# repeated-digest runs in the middle and at the end.
#
# index  digest  day   gap from previous capture
# 0      A       0     -
# 1      A       0     0
# 2      A       0     0
# 3      B       10    10 days
# 4      C       30    20 days
# 5      C       40    10 days
# 6      D       100   60 days
# 7      E       101   1 day
# 8      F       131   30 days
# 9      F       132   1 day
@pytest.fixture
def series():
    return [
        make_record(0, 'A', 0),
        make_record(0, 'A', 6),
        make_record(0, 'A', 12),
        make_record(10, 'B'),
        make_record(30, 'C'),
        make_record(40, 'C'),
        make_record(100, 'D'),
        make_record(101, 'E'),
        make_record(131, 'F'),
        make_record(132, 'F'),
    ]


def test_budget_is_never_exceeded(series):
    for budget in range(1, len(series) + 3):
        selection = sample_snapshots(series, budget)
        assert isinstance(selection, SampleSelection)
        assert len(selection.records) <= budget
        assert selection.budget == len(selection.records)
        assert selection.requested_budget == budget
        # Gap counts never include a selected record or exceed the series size.
        assert selection.budget + sum(gap.skipped_captures for gap in selection.gaps) <= len(series)


def test_full_budget_covers_every_distinct_candidate(series):
    selection = sample_snapshots(series, len(series) + 5)
    assert selection.gaps == ()
    # Boundaries (2) + change points (5) + run representatives (2) = 9. The
    # remaining capture is an interior repeat whose digest is already
    # represented, so it never needs its own slot.
    assert selection.budget == 9
    for record in series:
        assert any(selected.digest == record.digest for selected in selection.records)


def test_timeline_boundaries_are_always_covered(series):
    selection = sample_snapshots(series, 5)
    assert selection.records[0] == series[0]
    assert selection.records[-1] == series[-1]
    boundary_roles = [reason.role for reason in selection.reasons if reason.role == 'boundary']
    assert boundary_roles == ['boundary', 'boundary']


def test_change_points_after_long_empty_windows_win(series):
    selection = sample_snapshots(series, 5)
    selected = set(selection.records)

    # The 60-day empty window (index 6, digest D) is the longest gap, so its
    # change point must be selected before the 10-day one (index 3, digest B)
    # or the 1-day one (index 7, digest E).
    assert series[6] in selected
    assert series[3] not in selected
    assert series[7] not in selected

    change_reasons = [reason for reason in selection.reasons if reason.role == 'change']
    assert all(isinstance(reason.gap_before, timedelta) for reason in change_reasons)
    long_gap = next(reason for reason in change_reasons if reason.record is series[6])
    assert long_gap.gap_before == timedelta(days=60)


def test_budget_shortfall_lists_uncovered_spans(series):
    selection = sample_snapshots(series, 5)
    assert len(selection.records) == 5
    assert selection.gaps
    assert all(isinstance(gap, CoverageGap) for gap in selection.gaps)
    assert all(gap.start <= gap.end for gap in selection.gaps)
    assert all(gap.reason == 'budget_exhausted' for gap in selection.gaps)

    # Every capture is either selected or accounted for inside a gap.
    accounted = selection.budget + sum(gap.skipped_captures for gap in selection.gaps)
    assert accounted == len(series)

    # Gaps are ordered and non-overlapping after merging.
    for earlier, later in zip(selection.gaps, selection.gaps[1:]):
        assert earlier.end <= later.start


def test_representative_policy_keeps_time_representatives(series):
    selection = sample_snapshots(series, 20)
    assert selection.gaps == ()
    roles = [reason.role for reason in selection.reasons]
    assert 'representative' in roles
    representative = next(reason for reason in selection.reasons if reason.role == 'representative')
    assert representative.record.digest == 'A'
    assert representative.record is series[2]
    assert selection.records == tuple(sorted(selection.records, key=lambda record: record.timestamp))


def test_drop_policy_deduplicates_repeated_digests(series):
    selection = sample_snapshots(series, 20, duplicate_policy=DuplicatePolicy.drop)
    assert all(reason.role != 'representative' for reason in selection.reasons)
    # Interior repeated captures disappear (A repeats at indices 1-2, C repeat
    # at index 5), but both timeline boundaries are always retained -- the last
    # capture happens to repeat digest F, yet still marks the range end.
    selected_digests = [record.digest for record in selection.records]
    assert selected_digests == ['A', 'B', 'C', 'D', 'E', 'F', 'F']
    assert series[1] not in selection.records
    assert series[2] not in selection.records
    assert series[5] not in selection.records
    assert selection.records[-1] is series[9]
    assert selection.distinct_digests == 6
    assert selection.duplicate_policy is DuplicatePolicy.drop


def test_drop_policy_accepts_string_name(series):
    selection = sample_snapshots(series, 20, duplicate_policy='drop')
    assert selection.duplicate_policy is DuplicatePolicy.drop
    assert len(selection.records) == 7


def test_minimum_budget_picks_earliest_boundary(series):
    selection = sample_snapshots(series, 1)
    assert len(selection.records) == 1
    assert selection.records[0] is series[0]
    assert selection.reasons[0].role == 'boundary'
    # All other captures are accounted for exactly once across the gaps.
    assert sum(gap.skipped_captures for gap in selection.gaps) == len(series) - 1
    assert selection.budget + sum(gap.skipped_captures for gap in selection.gaps) == len(series)


def test_unsorted_input_returns_chronological_output(series):
    shuffled = list(reversed(series))
    selection = sample_snapshots(shuffled, 5)
    timestamps = [record.timestamp for record in selection.records]
    assert timestamps == sorted(timestamps)


def test_time_range_filters_and_reports_outer_emptiness(series):
    selection = sample_snapshots(
        series,
        3,
        from_date=BASE + timedelta(days=20),
        to_date=BASE + timedelta(days=140),
    )
    assert selection.total_captures == 6
    assert all(record.timestamp >= BASE + timedelta(days=20) for record in selection.records)
    assert all(record.timestamp <= BASE + timedelta(days=140) for record in selection.records)

    leading_gap = next(gap for gap in selection.gaps if gap.reason == 'no_captures_in_range')
    assert leading_gap.start == BASE + timedelta(days=20)
    assert leading_gap.end == series[4].timestamp
    assert leading_gap.skipped_captures == 0


def test_empty_series_reports_requested_range():
    selection = sample_snapshots(
        [],
        5,
        from_date=datetime(2020, 1, 1, tzinfo=UTC),
        to_date=datetime(2020, 2, 1, tzinfo=UTC),
    )
    assert selection.records == ()
    assert selection.budget == 0
    assert len(selection.gaps) == 1
    assert selection.gaps[0].reason == 'no_captures_in_range'


def test_invalid_arguments(series):
    with pytest.raises(ValueError):
        sample_snapshots(series, 0)
    with pytest.raises(TypeError):
        sample_snapshots(series, 1.5)
    with pytest.raises(ValueError):
        sample_snapshots(series, 3, duplicate_policy='nope')
    with pytest.raises(ValueError):
        sample_snapshots(
            series,
            3,
            from_date=datetime(2020, 2, 1, tzinfo=UTC),
            to_date=datetime(2020, 1, 1, tzinfo=UTC),
        )


def test_report_lists_selection_and_gaps(series):
    selection = sample_snapshots(series, 5)
    text = selection.report()
    assert 'Selected 5 of 10 captures' in text
    assert '[boundary]' in text
    assert '[change]' in text
    assert 'Uncovered time spans' in text
    for reason in selection.reasons:
        assert reason.record.timestamp.isoformat() in text
        assert isinstance(reason, SelectionReason)

    full_coverage = sample_snapshots(series, 20)
    assert 'no time spans were omitted' in full_coverage.report()


def test_client_entry_point_uses_index_only(series):
    client = WaybackClient(session=mock.Mock())
    with mock.patch.object(client, 'search', return_value=iter(series)) as search:
        selection = client.sample_mementos(
            'http://example.com',
            5,
            from_date=BASE,
            to_date=BASE + timedelta(days=140),
        )

    search.assert_called_once_with(
        'http://example.com',
        from_date=BASE,
        to_date=BASE + timedelta(days=140),
    )
    assert len(selection.records) == 5
    # The session only exists on the client; no memento requests were issued.
    client.session.request.assert_not_called()
    client.close()


def test_client_entry_point_end_to_end_with_cdx_index(test_client, requests_mock):
    cdx_lines = []
    for day, digest in [(0, 'A'), (0, 'A'), (10, 'B'), (30, 'C'), (100, 'D'), (101, 'E')]:
        timestamp = (BASE + timedelta(days=day)).strftime('%Y%m%d%H%M%S')
        cdx_lines.append(
            ' '.join(['com,example)/', timestamp, 'http://example.com', 'text/html', '200', digest, '100'])
        )
    requests_mock.get(
        'https://web.archive.org/cdx/search/cdx'
        '?url=http%3A%2F%2Fexample.com&showResumeKey=true&resolveRevisits=true',
        [{'status_code': 200, 'text': '\n'.join(cdx_lines)}],
    )

    selection = test_client.sample_mementos('http://example.com', 3)

    assert len(selection.records) == 3
    assert selection.records[0].timestamp == BASE
    assert selection.records[-1].timestamp == BASE + timedelta(days=101)
    # Exactly one CDX index request and zero memento/body downloads.
    assert requests_mock.call_count == 1
    assert all('/web/' not in request.url for request in requests_mock.request_history)
