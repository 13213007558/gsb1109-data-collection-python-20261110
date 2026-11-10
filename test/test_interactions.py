from warcio.interactions import InteractionQuery, RecordSummary, query_interactions
from warcio.cli import main

from io import BytesIO

import itertools
import json
import pytest


# ============================================================================
def make_warc_record(rec_type, url, date, record_id, http_block, warc_extra=''):
    msgtype = 'request' if rec_type == 'request' else 'response'
    headers = ('WARC/1.0\r\n'
               'WARC-Type: {0}\r\n'
               'WARC-Record-ID: {1}\r\n'
               'WARC-Target-URI: {2}\r\n'
               'WARC-Date: {3}\r\n'
               '{4}'
               'Content-Type: application/http; msgtype={5}\r\n'
               'Content-Length: {6}\r\n'
               '\r\n').format(rec_type, record_id, url, date,
                              warc_extra, msgtype, len(http_block))
    return headers.encode('utf-8') + http_block + b'\r\n\r\n'


def make_request(url, date, record_id, warc_extra=''):
    http = b'GET / HTTP/1.1\r\nHost: example.com\r\n\r\n'
    return make_warc_record('request', url, date, record_id, http, warc_extra)


def make_response(url, date, record_id, status='200 OK',
                  http_extra='', warc_extra=''):
    http = ('HTTP/1.1 {0}\r\n'
            'Content-Type: text/plain\r\n'
            '{1}'
            '\r\n').format(status, http_extra).encode('utf-8') + b'hello'
    return make_warc_record('response', url, date, record_id, http, warc_extra)


def run_query(records, **kwargs):
    return query_interactions(BytesIO(b''.join(records)), **kwargs)


def strip_offsets(entry):
    if isinstance(entry, dict):
        return {key: strip_offsets(value) for key, value in entry.items()
                if key != 'offset'}
    if isinstance(entry, list):
        return [strip_offsets(value) for value in entry]
    return entry


def assert_shuffle_invariant(records, **kwargs):
    expected = strip_offsets(run_query(records, **kwargs))
    for perm in itertools.permutations(records):
        report = strip_offsets(run_query(list(perm), **kwargs))
        assert report == expected


# ============================================================================
class TestInteractions(object):
    URL_A = 'http://example.com/a'
    URL_B = 'http://example.com/b'

    def test_explicit_concurrent_to_pair(self):
        resp = make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>')
        req = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>',
                           warc_extra='WARC-Concurrent-To: <urn:uuid:resp-1>\r\n')

        report = run_query([resp, req])

        assert len(report['interactions']) == 1
        interaction = report['interactions'][0]
        assert interaction['request']['record_id'] == '<urn:uuid:req-1>'
        assert interaction['response']['record_id'] == '<urn:uuid:resp-1>'
        assert interaction['basis'] == 'explicit:WARC-Concurrent-To'
        assert interaction['confidence'] == 'explicit'
        assert report['ambiguous'] == []
        assert report['unpaired'] == {'requests': [], 'responses': []}
        assert report['conflicts'] == []

    def test_explicit_refers_to_pair(self):
        resp = make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>')
        req = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>',
                           warc_extra='WARC-Refers-To: <urn:uuid:resp-1>\r\n')

        report = run_query([req, resp])

        assert len(report['interactions']) == 1
        assert report['interactions'][0]['basis'] == 'explicit:WARC-Refers-To'

    def test_explicit_pair_preferred_over_heuristic(self):
        # two requests to the same url at the same time; explicit link
        # decides which one belongs to the response
        resp = make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>')
        req_1 = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>',
                             warc_extra='WARC-Concurrent-To: <urn:uuid:resp-1>\r\n')
        req_2 = make_request(self.URL_A, '2020-01-01T00:00:01Z', '<urn:uuid:req-2>')

        report = run_query([resp, req_1, req_2], max_time_delta=10)

        assert len(report['interactions']) == 1
        assert report['interactions'][0]['request']['record_id'] == '<urn:uuid:req-1>'
        assert report['interactions'][0]['confidence'] == 'explicit'
        # the other request stays unpaired rather than stealing the response
        assert [entry['record']['record_id']
                for entry in report['unpaired']['requests']] == ['<urn:uuid:req-2>']

    def test_heuristic_disabled_without_time_delta(self):
        resp = make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>')
        req = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>')

        report = run_query([resp, req])

        assert report['interactions'] == []
        assert [entry['reason']
                for entry in report['unpaired']['requests']] == ['heuristic-disabled']
        assert [entry['reason']
                for entry in report['unpaired']['responses']] == ['heuristic-disabled']

    def test_heuristic_unique_candidate(self):
        resp = make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>')
        req = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>')

        report = run_query([resp, req], max_time_delta=5)

        assert len(report['interactions']) == 1
        interaction = report['interactions'][0]
        assert interaction['basis'] == 'heuristic:url+time'
        assert interaction['confidence'] == 'candidate'
        assert 'max_time_delta' in interaction['evidence']

    def test_heuristic_window_excludes_distant_records(self):
        resp = make_response(self.URL_A, '2020-01-01T00:01:00Z', '<urn:uuid:resp-1>')
        req = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>')

        report = run_query([resp, req], max_time_delta=5)

        assert report['interactions'] == []
        assert len(report['unpaired']['requests']) == 1
        assert len(report['unpaired']['responses']) == 1

    def test_heuristic_requires_same_url(self):
        resp = make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>')
        req = make_request(self.URL_B, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>')

        report = run_query([resp, req], max_time_delta=5)

        assert report['interactions'] == []

    def test_ambiguous_multiple_requests_same_url(self):
        # same address requested twice in a short time: pairing by url
        # alone would mismatch, so the ambiguity must be preserved
        resp = make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>')
        req_1 = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>')
        req_2 = make_request(self.URL_A, '2020-01-01T00:00:01Z', '<urn:uuid:req-2>')

        report = run_query([resp, req_1, req_2], max_time_delta=10)

        assert report['interactions'] == []
        assert len(report['ambiguous']) == 1
        entry = report['ambiguous'][0]
        assert entry['record']['record_id'] == '<urn:uuid:resp-1>'
        assert entry['reason'] == 'multiple-requests-within-time-window'
        assert [cand['record_id'] for cand in entry['candidates']] == \
            ['<urn:uuid:req-1>', '<urn:uuid:req-2>']

    def test_contended_request_stays_ambiguous(self):
        # one request is the only candidate for two responses:
        # it can not be assigned to either with certainty
        req = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>')
        resp_1 = make_response(self.URL_A, '2020-01-01T00:00:01Z', '<urn:uuid:resp-1>')
        resp_2 = make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-2>')

        report = run_query([req, resp_1, resp_2], max_time_delta=10)

        assert report['interactions'] == []
        assert len(report['ambiguous']) == 2
        assert set(entry['reason'] for entry in report['ambiguous']) == \
            {'candidate-request-claimed-by-multiple-responses'}

    def test_duplicate_record_id_conflict(self):
        resp_1 = make_response(self.URL_A, '2020-01-01T00:00:01Z', '<urn:uuid:dup>')
        resp_2 = make_response(self.URL_B, '2020-01-01T00:00:02Z', '<urn:uuid:dup>')
        req = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>',
                           warc_extra='WARC-Concurrent-To: <urn:uuid:dup>\r\n')

        report = run_query([resp_1, resp_2, req])

        assert report['interactions'] == []
        conflict_types = [entry['type'] for entry in report['conflicts']]
        assert 'duplicate-record-id' in conflict_types
        assert 'ambiguous-reference' in conflict_types

    def test_unresolved_reference_conflict(self):
        req = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>',
                           warc_extra='WARC-Concurrent-To: <urn:uuid:missing>\r\n')

        report = run_query([req])

        assert report['interactions'] == []
        assert [entry['type'] for entry in report['conflicts']] == \
            ['unresolved-reference']
        assert report['conflicts'][0]['reference'] == '<urn:uuid:missing>'

    def test_contradictory_links_conflict(self):
        # two requests both claim the same response: the contradiction
        # must be reported, not silently overwritten
        resp = make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>')
        req_1 = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>',
                             warc_extra='WARC-Concurrent-To: <urn:uuid:resp-1>\r\n')
        req_2 = make_request(self.URL_A, '2020-01-01T00:00:01Z', '<urn:uuid:req-2>',
                             warc_extra='WARC-Concurrent-To: <urn:uuid:resp-1>\r\n')

        report = run_query([resp, req_1, req_2], max_time_delta=10)

        assert report['interactions'] == []
        assert [entry['type'] for entry in report['conflicts']] == \
            ['contradictory-links']
        conflict = report['conflicts'][0]
        assert conflict['record']['record_id'] == '<urn:uuid:resp-1>'
        assert len(conflict['linked_records']) == 2
        # the contradiction is kept as ambiguity, and heuristic pairing
        # must not "fix" it afterwards
        assert len(report['ambiguous']) == 1
        assert report['ambiguous'][0]['reason'] == 'contradictory-explicit-links'

    def test_redirect_follow_up_is_separate_interaction(self):
        resp_1 = make_response(self.URL_A, '2020-01-01T00:00:01Z', '<urn:uuid:resp-1>',
                               status='301 Moved Permanently',
                               http_extra='Location: http://example.com/b\r\n')
        req_1 = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>',
                             warc_extra='WARC-Concurrent-To: <urn:uuid:resp-1>\r\n')
        resp_2 = make_response(self.URL_B, '2020-01-01T00:00:03Z', '<urn:uuid:resp-2>')
        req_2 = make_request(self.URL_B, '2020-01-01T00:00:02Z', '<urn:uuid:req-2>',
                             warc_extra='WARC-Concurrent-To: <urn:uuid:resp-2>\r\n')

        report = run_query([resp_1, req_1, resp_2, req_2])

        assert len(report['interactions']) == 2
        by_request = {entry['request']['record_id']: entry
                      for entry in report['interactions']}
        redirect = by_request['<urn:uuid:req-1>']['response']['redirect']
        assert redirect['status'] == '301'
        assert redirect['location'] == 'http://example.com/b'
        # the redirect target request is its own interaction
        assert 'redirect' not in by_request['<urn:uuid:req-2>']['response']

    def test_url_filter(self):
        resp_a = make_response(self.URL_A, '2020-01-01T00:00:01Z', '<urn:uuid:resp-1>')
        req_a = make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>')
        resp_b = make_response(self.URL_B, '2020-01-01T00:00:03Z', '<urn:uuid:resp-2>')
        req_b = make_request(self.URL_B, '2020-01-01T00:00:02Z', '<urn:uuid:req-2>')

        records = [resp_a, req_a, resp_b, req_b]
        report = run_query(records, max_time_delta=5, urls=[self.URL_A])

        assert len(report['interactions']) == 1
        assert report['interactions'][0]['request']['url'] == self.URL_A

        report = run_query(records, max_time_delta=5)
        assert len(report['interactions']) == 2

    def test_shuffle_invariance_explicit(self):
        records = [
            make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>'),
            make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>',
                         warc_extra='WARC-Concurrent-To: <urn:uuid:resp-1>\r\n'),
            make_response(self.URL_B, '2020-01-01T00:00:05Z', '<urn:uuid:resp-2>'),
            make_request(self.URL_B, '2020-01-01T00:00:04Z', '<urn:uuid:req-2>',
                         warc_extra='WARC-Concurrent-To: <urn:uuid:resp-2>\r\n'),
        ]
        assert_shuffle_invariant(records, max_time_delta=10)

    def test_shuffle_invariance_ambiguous_stays_ambiguous(self):
        # not enough evidence: shuffling must not turn the ambiguous
        # pairing into a definite answer
        records = [
            make_response(self.URL_A, '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>'),
            make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>'),
            make_request(self.URL_A, '2020-01-01T00:00:01Z', '<urn:uuid:req-2>'),
            make_response(self.URL_B, '2020-01-01T00:00:05Z', '<urn:uuid:resp-2>'),
            make_request(self.URL_B, '2020-01-01T00:00:04Z', '<urn:uuid:req-3>'),
        ]
        assert_shuffle_invariant(records, max_time_delta=10)

        report = run_query(records, max_time_delta=10)
        assert len(report['interactions']) == 1
        assert len(report['ambiguous']) == 1

    def test_shuffle_invariance_conflicts(self):
        records = [
            make_response(self.URL_A, '2020-01-01T00:00:01Z', '<urn:uuid:dup>'),
            make_response(self.URL_B, '2020-01-01T00:00:02Z', '<urn:uuid:dup>'),
            make_request(self.URL_A, '2020-01-01T00:00:00Z', '<urn:uuid:req-1>',
                         warc_extra='WARC-Concurrent-To: <urn:uuid:dup>\r\n'),
        ]
        assert_shuffle_invariant(records, max_time_delta=10)


# ============================================================================
def test_cli_interactions(capsys, tmp_path):
    records = [
        make_response('http://example.com/a', '2020-01-01T00:00:02Z', '<urn:uuid:resp-1>'),
        make_request('http://example.com/a', '2020-01-01T00:00:00Z', '<urn:uuid:req-1>',
                     warc_extra='WARC-Concurrent-To: <urn:uuid:resp-1>\r\n'),
        make_response('http://example.com/b', '2020-01-01T00:00:04Z', '<urn:uuid:resp-2>'),
        make_request('http://example.com/b', '2020-01-01T00:00:03Z', '<urn:uuid:req-2>'),
    ]

    path = str(tmp_path / 'test.warc')
    with open(path, 'wb') as out:
        out.write(b''.join(records))

    main(args=['interactions', '--max-time-delta', '5', path])
    report = json.loads(capsys.readouterr().out)

    assert len(report['interactions']) == 2
    assert report['interactions'][0]['confidence'] == 'explicit'
    assert report['interactions'][1]['confidence'] == 'candidate'

    # without --max-time-delta, only the explicit pair is reported
    main(args=['interactions', path])
    report = json.loads(capsys.readouterr().out)

    assert len(report['interactions']) == 1
    assert len(report['unpaired']['requests']) == 1
    assert len(report['unpaired']['responses']) == 1
