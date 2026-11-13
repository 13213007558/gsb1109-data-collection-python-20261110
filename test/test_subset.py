import json
import os

from io import BytesIO

from warcio.archiveiterator import ArchiveIterator
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders
from warcio.subset import (SubsetExporter, SubsetLimitExceeded, SubsetPolicy,
                           export_subset)
from warcio.warcwriter import WARCWriter

from . import get_test_file

import pytest


DATE_A = '2020-01-01T00:00:00Z'
DATE_B = '2020-01-02T00:00:00Z'
DATE_C = '2020-01-03T00:00:00Z'
DATE_D = '2020-01-04T00:00:00Z'

PAYLOAD = b'shared payload body'


# ============================================================================
def _http_headers(content_type='text/plain'):
    return StatusAndHeaders('200 OK',
                            [('Content-Type', content_type),
                             ('Content-Length', str(len(PAYLOAD)))],
                            protocol='HTTP/1.0')


def _response(builder, rec_id, uri, date, payload=PAYLOAD):
    return builder.create_warc_record(
        uri, 'response', payload=BytesIO(payload), length=len(payload),
        http_headers=_http_headers(),
        warc_headers_dict={'WARC-Record-ID': rec_id, 'WARC-Date': date})


def _request(builder, rec_id, uri, date, concurrent_to):
    record = builder.create_warc_record(
        uri, 'request', payload=BytesIO(b''), length=0,
        http_headers=StatusAndHeaders('GET / HTTP/1.0',
                                      [('Host', 'example.com')],
                                      protocol='HTTP/1.0'),
        warc_headers_dict={'WARC-Record-ID': rec_id, 'WARC-Date': date})
    record.rec_headers.add_header('WARC-Concurrent-To', concurrent_to)
    return record


def _revisit(builder, rec_id, uri, date, refers_to, refers_to_date,
             digest):
    return builder.create_revisit_record(
        uri, digest, refers_to, refers_to_date,
        warc_headers_dict={'WARC-Record-ID': rec_id, 'WARC-Date': date})


def build_test_archive(filename):
    builder = RecordBuilder()
    records = []

    records.append(builder.create_warcinfo_record(
        'subset-test.warc.gz', {'software': 'warcio test',
                                'format': 'WARC File Format 1.0'}))

    resp_a = _response(builder, '<urn:uuid:resp-a>',
                       'http://example.com/a', DATE_A)
    records.append(resp_a)
    records.append(_request(builder, '<urn:uuid:req-a>',
                            'http://example.com/a', DATE_A,
                            '<urn:uuid:resp-a>'))

    resp_b = _response(builder, '<urn:uuid:resp-b>',
                       'http://example.com/b', DATE_B)
    records.append(resp_b)
    records.append(_request(builder, '<urn:uuid:req-b>',
                            'http://example.com/b', DATE_B,
                            '<urn:uuid:resp-b>'))

    digest = resp_b.rec_headers.get_header('WARC-Payload-Digest')
    records.append(_revisit(builder, '<urn:uuid:revisit-b>',
                            'http://example.com/b', DATE_C,
                            'http://example.com/b', DATE_B, digest))
    records.append(_request(builder, '<urn:uuid:req-revisit-b>',
                            'http://example.com/b', DATE_C,
                            '<urn:uuid:revisit-b>'))

    records.append(_response(builder, '<urn:uuid:resp-c>',
                             'http://example.com/c', DATE_D))
    records.append(_request(builder, '<urn:uuid:req-c>',
                            'http://example.com/c', DATE_D,
                            '<urn:uuid:resp-c>'))

    with open(filename, 'wb') as out:
        writer = WARCWriter(out, gzip=False)
        for record in records:
            writer.write_record(record)

    return filename


# ============================================================================
@pytest.fixture
def archive(tmpdir):
    return build_test_archive(str(tmpdir.join('source.warc')))


def _records(filename):
    with open(filename, 'rb') as stream:
        return list(ArchiveIterator(stream))


def _ids(filename):
    return [record.rec_headers.get_header('WARC-Record-ID')
            for record in _records(filename)]


def _payloads(filename):
    result = {}
    with open(filename, 'rb') as stream:
        for record in ArchiveIterator(stream):
            result[record.rec_headers.get_header('WARC-Record-ID')] = \
                record.raw_stream.read()
    return result


# ============================================================================
class TestSubsetPolicy(object):
    def test_all_and_none(self):
        policy = SubsetPolicy(follow='all')
        assert policy.follows(SubsetPolicy.CONCURRENT)
        assert policy.follows(SubsetPolicy.REFERS_TO)
        assert policy.follows(SubsetPolicy.PAYLOAD)
        assert policy.include_warcinfo

        policy = SubsetPolicy(follow='none')
        assert not policy.follows(SubsetPolicy.CONCURRENT)
        assert not policy.follows(SubsetPolicy.REFERS_TO)

        policy = SubsetPolicy(follow='concurrent,refers-to')
        assert policy.follows(SubsetPolicy.CONCURRENT)
        assert not policy.follows(SubsetPolicy.PAYLOAD)
        assert not policy.warcinfo

    def test_unknown_policy_rejected(self):
        with pytest.raises(ValueError):
            SubsetPolicy(follow='concurrent,typo')


# ============================================================================
class TestSubsetExporter(object):
    def test_url_seed_includes_pairs_warcinfo_and_revisit(self, archive, tmpdir):
        output = str(tmpdir.join('subset.warc'))
        manifest = str(tmpdir.join('manifest.json'))

        exporter = SubsetExporter(
            [archive], output=output, manifest=manifest,
            policy=SubsetPolicy(follow='all'),
            seed_uris=['http://example.com/b'])
        exporter.export()

        ids = _ids(output)

        # warcinfo, page b response, its request, the revisit, and the
        # revisit request. Response a is only pulled in as shared payload.
        assert ids[0] == '<urn:uuid:resp-a>' or ids[0].startswith('<urn:uuid:')
        assert '<urn:uuid:resp-b>' in ids
        assert '<urn:uuid:req-b>' in ids
        assert '<urn:uuid:revisit-b>' in ids
        assert '<urn:uuid:req-revisit-b>' in ids

        payloads = _payloads(output)
        assert payloads['<urn:uuid:resp-b>'] == PAYLOAD
        assert payloads['<urn:uuid:revisit-b>'] == b''

        data = json.load(open(manifest))
        assert data['complete'] is True
        assert data['status'] == 'complete'
        assert data['missing'] == []
        assert exporter.verify() == []

    def test_output_order_matches_source_order(self, archive, tmpdir):
        output = str(tmpdir.join('subset.warc'))

        SubsetExporter([archive], output=output,
                       policy=SubsetPolicy(follow='all'),
                       seed_uris=['http://example.com/a',
                                  'http://example.com/b']).export()

        source_ids = _ids(archive)
        subset_ids = _ids(output)
        assert subset_ids == [rec_id for rec_id in source_ids
                              if rec_id in set(subset_ids)]

    def test_shared_payload_included_once(self, archive, tmpdir):
        output = str(tmpdir.join('subset.warc'))

        exporter = SubsetExporter(
            [archive], output=output, policy=SubsetPolicy(follow='all'),
            seed_ids=['<urn:uuid:resp-b>', '<urn:uuid:resp-c>'])
        exporter.export()

        ids = _ids(output)
        assert len(ids) == len(set(ids))
        assert ids.count('<urn:uuid:resp-b>') == 1
        assert ids.count('<urn:uuid:resp-c>') == 1
        assert exporter.verify() == []

    def test_manifest_lists_missing_pair_when_policy_skips(self, archive,
                                                           tmpdir):
        output = str(tmpdir.join('subset.warc'))
        manifest = str(tmpdir.join('manifest.json'))

        exporter = SubsetExporter(
            [archive], output=output, manifest=manifest,
            policy=SubsetPolicy(follow='none', include_warcinfo=False),
            seed_ids=['<urn:uuid:resp-a>'])
        exporter.export()

        data = json.load(open(manifest))
        assert data['complete'] is False
        assert data['status'] == 'incomplete'
        values = data['missing_values']
        assert '<urn:uuid:req-a>' in values
        assert 'warcinfo' in [item['kind'] for item in data['missing']]

        problems = exporter.verify(require_warcinfo=True)
        assert any('WARC-Concurrent-To' in problem for problem in problems)
        assert any('no warcinfo record' in problem for problem in problems)

    def test_revisit_missing_payload_is_reported(self, archive, tmpdir):
        output = str(tmpdir.join('subset.warc'))
        manifest = str(tmpdir.join('manifest.json'))

        exporter = SubsetExporter(
            [archive], output=output, manifest=manifest,
            policy=SubsetPolicy(follow='none', include_warcinfo=False),
            seed_ids=['<urn:uuid:revisit-b>'])
        exporter.export()

        data = json.load(open(manifest))
        assert data['complete'] is False
        assert any(item['kind'] == 'payload-digest'
                   for item in data['missing'])
        problems = exporter.verify(require_warcinfo=False)
        assert any('revisit payload not included' in problem
                   for problem in problems)

        # The revisit still keeps its original ID and digest.
        revisit = [record for record in _records(output)
                   if record.rec_type == 'revisit'][0]
        assert revisit.rec_headers.get_header('WARC-Record-ID') == \
            '<urn:uuid:revisit-b>'
        assert revisit.rec_headers.get_header('WARC-Payload-Digest')

    def test_unmatched_seed_is_reported(self, archive, tmpdir):
        output = str(tmpdir.join('subset.warc'))
        manifest = str(tmpdir.join('manifest.json'))

        exporter = SubsetExporter(
            [archive], output=output, manifest=manifest,
            seeds=['http://example.com/missing'])
        exporter.export()

        data = json.load(open(manifest))
        assert data['complete'] is False
        assert data['record_count'] == 0
        assert data['missing_values'] == ['http://example.com/missing']

    def test_max_records_rejected_before_writing(self, archive, tmpdir):
        output = str(tmpdir.join('subset.warc'))

        with pytest.raises(SubsetLimitExceeded):
            SubsetExporter(
                [archive], output=output, policy=SubsetPolicy(follow='all'),
                seed_uris=['http://example.com/a'],
                max_records=1).export()

        assert not os.path.exists(output)

    def test_compressed_output_matches_record_sizes(self, archive, tmpdir):
        output = str(tmpdir.join('subset.warc.gz'))
        manifest = str(tmpdir.join('manifest.json'))

        exporter = SubsetExporter(
            [archive], output=output, manifest=manifest,
            policy=SubsetPolicy(follow='all'),
            seed_uris=['http://example.com/a'])
        exporter.export()

        data = json.load(open(manifest))
        with open(output, 'rb') as stream:
            it = ArchiveIterator(stream)
            for index, record in enumerate(it):
                item = data['records'][index]
                assert str(it.get_record_offset()) == item['export_offset']
                assert str(it.get_record_length()) == item['export_length']

        assert exporter.verify() == []

    def test_convenience_wrapper(self, archive, tmpdir):
        output = str(tmpdir.join('subset.warc'))

        exporter = export_subset([archive], output=output,
                                 seeds=['http://example.com/a'],
                                 follow='all')

        assert exporter.verify() == []
        assert json.loads(json.dumps(exporter.manifest_data()))['complete']


# ============================================================================
class TestSubsetCli(object):
    def test_cli_export_and_verify(self, archive, tmpdir, capsys):
        from warcio.cli import main

        output = str(tmpdir.join('subset.warc.gz'))
        manifest = str(tmpdir.join('manifest.json'))

        main(['subset', archive, output, '-u', 'http://example.com/a',
              '-m', manifest, '--verify'])

        assert 'Subset complete' in capsys.readouterr().err
        assert os.path.exists(output)
        assert json.load(open(manifest))['complete'] is True

    def test_cli_policy_without_pairs_returns_missing(self, archive, tmpdir,
                                                     capsys):
        from warcio.cli import main

        output = str(tmpdir.join('subset.warc'))
        manifest = str(tmpdir.join('manifest.json'))

        main(['subset', archive, output, '-r', '<urn:uuid:resp-a>',
              '-f', 'none', '--no-warcinfo', '-m', manifest])

        assert 'Subset incomplete' in capsys.readouterr().err
        data = json.load(open(manifest))
        assert data['complete'] is False
        assert '<urn:uuid:req-a>' in data['missing_values']
    def test_cli_rejects_bad_policy(self, archive, tmpdir):
        from warcio.cli import main

        with pytest.raises(SystemExit):
            main(['subset', archive, str(tmpdir.join('out.warc')),
                  '-u', 'http://example.com/a', '-f', 'concurrent,typo'])


# ============================================================================
def test_existing_example_archive():
    source = get_test_file('example.warc.gz')

    exporter = SubsetExporter(
        [source], policy=SubsetPolicy(follow='all'),
        seed_uris=['http://example.com/'])

    exporter.export()

    data = exporter.manifest_data()
    assert data['complete'] is True
    assert data['record_count'] == 6
    assert len({record['record_id'] for record in data['records']}) == 6
