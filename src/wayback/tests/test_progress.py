import pytest
from datetime import datetime, timezone

from wayback import SearchProgress, ResumeKeyError, CdxRecord
from wayback._progress import cdx_line_to_record, cdx_record_to_line


QUERY = {
    'url': 'http://example.com',
    'limit': '25',
    'from': '20010101000000',
    'to': '20011231000000',
    'showResumeKey': 'true',
    'resolveRevisits': 'true',
}


def make_line(suffix, timestamp, n):
    return (
        f'com,example)/{suffix} {timestamp} http://example.com/{suffix} '
        f'text/html 200 DIGEST{n} {1000 + n}'
    )


def test_cdx_line_round_trip():
    record = cdx_line_to_record(make_line('a', '20010101000000', 1))
    assert isinstance(record, CdxRecord)
    assert record.timestamp == datetime(2001, 1, 1, tzinfo=timezone.utc)
    assert cdx_record_to_line(record) == make_line('a', '20010101000000', 1)


def test_cdx_line_round_trip_with_missing_status_and_length():
    text = 'com,example)/ 20010101000000 http://example.com/ warc/revisit - DIGEST -'
    record = cdx_line_to_record(text)
    assert record.statuscode is None
    assert record.length is None
    assert cdx_record_to_line(record) == text


def test_progress_requires_url():
    with pytest.raises(ValueError):
        SearchProgress({'limit': '25'})


def test_progress_query_is_a_defensive_copy():
    progress = SearchProgress.start(dict(QUERY))
    view = progress.query
    view['url'] = 'changed'
    assert progress.query['url'] == QUERY['url']
    assert 'resumeKey' not in progress.to_query_dict()


def test_persistence_round_trip_dict():
    progress = SearchProgress.start(dict(QUERY))
    progress.add_replay_line(make_line('a', '20010101000000', 1))
    progress.advance_cursor('KEY2', make_line('a', '20010101000000', 1), '20010101000000')
    progress.note_record(cdx_line_to_record(make_line('a', '20010101000000', 1)))

    restored = SearchProgress.from_dict(progress.to_dict())
    assert restored.to_dict() == progress.to_dict()


def test_persistence_round_trip_disk(tmp_path):
    progress = SearchProgress.start(dict(QUERY))
    progress.advance_cursor('KEY2', make_line('a', '20010101000000', 1), '20010101000000')
    path = tmp_path / 'nested' / 'progress.json'
    progress.save(path)

    loaded = SearchProgress.load(path)
    assert loaded.to_dict() == progress.to_dict()


def test_unsupported_version_raises():
    with pytest.raises(ValueError):
        SearchProgress.from_dict({'version': 999, 'query': dict(QUERY)})


def test_replay_guard_rotation_on_cursor_advance():
    progress = SearchProgress.start(dict(QUERY))
    progress.add_replay_line(make_line('a', '20010101000000', 1))
    progress.add_replay_line(make_line('b', '20010102000000', 2))
    assert progress.is_replay(make_line('b', '20010102000000', 2))

    # Advancing the cursor rotates the guard to just the new boundary.
    progress.advance_cursor('KEY2', make_line('b', '20010102000000', 2), '20010102000000')
    assert progress.resume_key == 'KEY2'
    assert progress.replay_guard == (make_line('b', '20010102000000', 2),)
    assert not progress.is_replay(make_line('a', '20010101000000', 1))
    assert progress.is_replay(make_line('b', '20010102000000', 2))


def test_pending_and_completion_require_confirmation():
    progress = SearchProgress.start(dict(QUERY))
    record = cdx_line_to_record(make_line('a', '20010101000000', 1))
    progress.note_record(record)

    assert progress.pending_count == 1
    assert progress.seen_count == 1
    assert not progress.is_complete

    progress.mark_server_complete()
    # Server finished, but the consumer has not confirmed the record.
    assert progress.server_complete
    assert not progress.is_complete

    assert progress.mark_processed(record) is True
    assert progress.is_complete
    assert progress.processed_count == 1
    # Confirming again is a no-op.
    assert progress.mark_processed(record) is False
    assert progress.processed_count == 1


def test_mark_processed_accepts_raw_line():
    progress = SearchProgress.start(dict(QUERY))
    line = make_line('a', '20010101000000', 1)
    progress.note_record(cdx_line_to_record(line))
    assert progress.mark_processed(line) is True
    assert progress.pending_count == 0


def test_matches_query_ignores_cursor():
    progress = SearchProgress.start(dict(QUERY))
    progress.advance_cursor('KEY2', make_line('a', '20010101000000', 1), '20010101000000')

    assert progress.matches_query(dict(QUERY))
    different = dict(QUERY, limit='50')
    assert not progress.matches_query(different)


def test_tuple_filter_normalized_and_survives_persistence():
    query = dict(QUERY, filter=('statuscode:200', 'urlkey:.*x.*'))
    progress = SearchProgress.start(query)

    # A list-typed equivalent still matches the tuple original.
    list_query = dict(QUERY, filter=['statuscode:200', 'urlkey:.*x.*'])
    assert progress.matches_query(list_query)

    # After a JSON round trip the query still matches the original tuple form.
    restored = SearchProgress.from_dict(progress.to_dict())
    assert restored.matches_query(query)
    assert restored.to_search_kwargs()['filter_field'] == ['statuscode:200', 'urlkey:.*x.*']


def test_to_query_dict_includes_current_cursor():
    progress = SearchProgress.start(dict(QUERY))
    assert 'resumeKey' not in progress.to_query_dict()
    progress.advance_cursor('KEY2', make_line('a', '20010101000000', 1), '20010101000000')
    assert progress.to_query_dict()['resumeKey'] == 'KEY2'


def test_to_search_kwargs_reconstructs_arguments():
    query = dict(
        QUERY,
        offset='10',
        matchType='prefix',
        filter='statuscode:200',
        fastLatest='true',
        collapse='urlkey',
    )
    progress = SearchProgress.start(query)
    kwargs = progress.to_search_kwargs()

    assert kwargs['url'] == 'http://example.com'
    assert kwargs['limit'] == 25
    assert kwargs['offset'] == 10
    assert kwargs['match_type'] == 'prefix'
    assert kwargs['filter_field'] == 'statuscode:200'
    assert kwargs['fast_latest'] is True
    assert kwargs['collapse'] == 'urlkey'
    assert kwargs['resolve_revisits'] is True
    assert kwargs['from_date'] == datetime(2001, 1, 1, tzinfo=timezone.utc)
    assert kwargs['to_date'] == datetime(2001, 12, 31, tzinfo=timezone.utc)


def test_boundary_kwargs_uses_earliest_unconfirmed_and_drops_offset():
    query = dict(QUERY, offset='10')
    progress = SearchProgress.start(query)
    progress.note_record(cdx_line_to_record(make_line('c', '20010103000000', 3)))
    progress.note_record(cdx_line_to_record(make_line('b', '20010102000000', 2)))

    kwargs = progress.to_boundary_kwargs()
    assert 'offset' not in kwargs
    assert kwargs['from_date'] == datetime(2001, 1, 2, tzinfo=timezone.utc)


def test_boundary_kwargs_falls_back_to_last_processed():
    progress = SearchProgress.start(dict(QUERY))
    record = cdx_line_to_record(make_line('a', '20010101000000', 1))
    progress.note_record(record)
    progress.mark_processed(record)

    kwargs = progress.to_boundary_kwargs()
    assert kwargs['from_date'] == datetime(2001, 1, 1, tzinfo=timezone.utc)


def test_resume_key_error_offers_executable_recovery():
    progress = SearchProgress.start(dict(QUERY))
    progress.advance_cursor('KEY2', make_line('a', '20010101000000', 1), '20010101000000')
    error = progress.raise_resume_key_error(reason='boom')

    assert isinstance(error, ResumeKeyError)
    assert error.progress is progress
    message = str(error)
    assert 'boom' in message
    assert 'track_progress=True' in message
    assert 'to_search_kwargs' not in message
