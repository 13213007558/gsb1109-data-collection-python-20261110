import json
import os

import pytest

from io import BytesIO

from warcio.archiveiterator import ArchiveIterator
from warcio.cli import main
from warcio.redactor import (
    DEFAULT_SENSITIVE_HEADERS,
    RecordRedactor,
    RedactionError,
    RedactionPolicy,
    sanitize_file,
    verify_redacted_archive,
)
from warcio.statusandheaders import StatusAndHeaders

from . import get_test_file
from .test_writer import FixedTestWARCWriter


# ============================================================================
def _request_record(writer, uri, http_headers, body=b''):
    from six.moves.urllib.parse import urlsplit
    target = urlsplit(uri)
    request_target = target.path or '/'
    if target.query:
        request_target += '?' + target.query
    headers = StatusAndHeaders(
        'POST {0} HTTP/1.1'.format(request_target),
        list(http_headers),
        protocol='HTTP/1.1',
        is_http_request=True,
    )
    return writer.create_warc_record(
        uri, 'request',
        payload=BytesIO(body),
        http_headers=headers,
        length=len(body),
    )


def _response_record(writer, uri, http_headers, body=b''):
    headers = StatusAndHeaders('200 OK', list(http_headers), protocol='HTTP/1.1')
    return writer.create_warc_record(
        uri, 'response',
        payload=BytesIO(body),
        http_headers=headers,
        length=len(body),
    )


def _write_to_file(records, tmp_path, name='in.warc', gzip=False):
    path = os.path.join(str(tmp_path), name)
    writer = FixedTestWARCWriter(gzip=gzip)
    for record in records:
        writer.write_record(record)
    with open(path, 'wb') as fh:
        fh.write(writer.get_contents())
    return path


def _read_records(path):
    # Buffer each body while iterating: ArchiveIterator drains the previous
    # record's stream when advancing, so a body read after materializing the
    # full list would otherwise be empty.
    records = []
    with open(path, 'rb') as fh:
        for record in ArchiveIterator(fh):
            record.raw_stream = BytesIO(record.raw_stream.read())
            records.append(record)
    # Advancing the iterator also drains our replacement streams; rewind.
    for record in records:
        record.raw_stream.seek(0)
    return records


# ============================================================================
class TestHeaderRedaction:
    def test_removes_default_sensitive_headers_case_insensitive(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        record = _request_record(writer, 'http://example.com/login', [
            ('Host', 'example.com'),
            ('Cookie', 'session=supersecret'),
            ('Authorization', 'Bearer token-abc'),
            ('Proxy-Authorization', 'Basic xyz'),
            ('X-Keep', 'visible'),
        ])
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy())

        assert problems == []
        assert results[0].status == 'redacted'
        targets = {a['target'] for a in results[0].actions}
        assert targets == {
            'http-header:cookie',
            'http-header:authorization',
            'http-header:proxy-authorization',
        }

        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc'))
        names = {name.lower() for name, _ in out_records[0].http_headers.headers}
        assert 'cookie' not in names
        assert 'authorization' not in names
        assert 'proxy-authorization' not in names
        assert 'x-keep' in names

    def test_all_duplicate_headers_removed(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        record = _response_record(writer, 'http://example.com/', [
            ('Content-Type', 'text/html'),
            ('Set-Cookie', 'a=1'),
            ('set-cookie', 'b=2'),
            ('SET-COOKIE', 'c=3'),
        ])
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy())

        assert problems == []
        cookie_actions = [a for a in results[0].actions
                          if a['target'] == 'http-header:set-cookie']
        assert cookie_actions[0]['count'] == 3

        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc'))
        names = [name.lower() for name, _ in out_records[0].http_headers.headers]
        assert 'set-cookie' not in names

    def test_custom_header_list_overrides_defaults(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        record = _request_record(writer, 'http://example.com/', [
            ('Cookie', 'session=keep-me'),
            ('X-Tenant-Secret', '42'),
        ])
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy(sensitive_headers=['x-tenant-secret']))

        assert problems == []
        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc'))
        names = {name.lower() for name, _ in out_records[0].http_headers.headers}
        assert 'x-tenant-secret' not in names
        assert 'cookie' in names

    def test_default_policy_constant_contents(self):
        assert 'cookie' in DEFAULT_SENSITIVE_HEADERS
        assert 'set-cookie' in DEFAULT_SENSITIVE_HEADERS
        assert 'authorization' in DEFAULT_SENSITIVE_HEADERS
        assert 'proxy-authorization' in DEFAULT_SENSITIVE_HEADERS


# ============================================================================
class TestFormFieldRedaction:
    def test_removes_named_form_fields_and_updates_length(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        body = b'user=alice&password=hunter2&token=abc&keep=yes'
        record = _request_record(writer, 'http://example.com/login', [
            ('Host', 'example.com'),
            ('Content-Type', 'application/x-www-form-urlencoded'),
            ('Content-Length', str(len(body))),
        ], body=body)
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy(sensitive_form_fields=['password', 'token']))

        assert problems == []
        assert results[0].status == 'redacted'
        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc'))
        new_body = out_records[0].raw_stream.read()
        assert new_body == b'user=alice&keep=yes'
        assert b'hunter2' not in new_body
        assert b'token=abc' not in new_body
        assert out_records[0].http_headers.get_header('Content-Length') == \
            str(len(new_body))

    def test_matches_urlencoded_field_names(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        body = b'order%5Fid=7&email=me%40x.io&keep=1'
        record = _request_record(writer, 'http://example.com/', [
            ('Content-Type', 'application/x-www-form-urlencoded'),
            ('Content-Length', str(len(body))),
        ], body=body)
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy(sensitive_form_fields=['order_id', 'email']))

        assert problems == []
        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc'))
        new_body = out_records[0].raw_stream.read()
        assert new_body == b'keep=1'

    def test_form_redaction_only_applies_to_request_records(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        body = b'password=hunter2'
        record = _response_record(writer, 'http://example.com/form', [
            ('Content-Type', 'application/x-www-form-urlencoded'),
            ('Content-Length', str(len(body))),
        ], body=body)
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy(sensitive_form_fields=['password']))

        assert problems == []
        assert results[0].status == 'retained'
        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc'))
        assert out_records[0].raw_stream.read() == body


# ============================================================================
class TestRecordRedactorUnit:
    def test_redact_in_memory_record_reports_actions(self):
        writer = FixedTestWARCWriter(gzip=False)
        record = _request_record(writer, 'http://example.com/', [
            ('Cookie', 'a=1'),
            ('Authorization', 'Bearer x'),
        ], body=b'')

        output, result = RecordRedactor(RedactionPolicy()).redact(record)

        assert result.status == 'redacted'
        targets = {a['target'] for a in result.actions}
        assert targets == {'http-header:cookie', 'http-header:authorization'}
        # The rebuilt record is writable and lost both sensitive headers.
        names = {n.lower() for n, _ in output.http_headers.headers}
        assert not (names & {'cookie', 'authorization'})


# ============================================================================
class TestConservativePolicies:
    def test_compressed_payload_body_is_never_rewritten(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        body = b'password=hunter2&keep=1'
        record = _request_record(writer, 'http://example.com/', [
            ('Cookie', 'session=secret'),
            ('Content-Type', 'application/x-www-form-urlencoded'),
            ('Content-Encoding', 'gzip'),
            ('Content-Length', str(len(body))),
        ], body=body)
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy(sensitive_form_fields=['password']))

        assert problems == []
        # Header is still redacted...
        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc'))
        names = {name.lower() for name, _ in out_records[0].http_headers.headers}
        assert 'cookie' not in names
        # ...but the encoded body is left intact and the skip is reported.
        assert out_records[0].raw_stream.read() == body
        skip = [a for a in results[0].actions if a['action'] == 'skipped']
        assert skip and 'content-encoding' in skip[0]['reason']

    def test_chunked_transfer_encoding_body_is_never_rewritten(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        body = b'password=hunter2'
        record = _request_record(writer, 'http://example.com/', [
            ('Content-Type', 'application/x-www-form-urlencoded'),
            ('Transfer-Encoding', 'chunked'),
        ], body=body)
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy(sensitive_form_fields=['password']))

        assert problems == []
        assert results[0].status == 'retained'
        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc'))
        assert out_records[0].raw_stream.read() == body
        assert any(a['action'] == 'skipped' for a in results[0].actions)

    def test_unparseable_form_fails_and_keeps_original_bytes(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        body = b'password=hunter2&garbage'
        record = _request_record(writer, 'http://example.com/', [
            ('Cookie', 'session=secret'),
            ('Content-Type', 'application/x-www-form-urlencoded'),
            ('Content-Length', str(len(body))),
        ], body=body)
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy(sensitive_form_fields=['password']))

        assert problems == []
        assert results[0].status == 'failed'
        assert results[0].reasons
        # Original bytes and original sensitive header both survive, and the
        # manifest never claims the record was cleaned.
        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc'))
        assert out_records[0].raw_stream.read() == body
        names = {name.lower() for name, _ in out_records[0].http_headers.headers}
        assert 'cookie' in names

    def test_unparseable_form_lenient_mode_is_retained_with_warning(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        body = b'garbage'
        record = _request_record(writer, 'http://example.com/', [
            ('Content-Type', 'application/x-www-form-urlencoded'),
        ], body=body)
        path = _write_to_file([record], tmp_path)

        policy = RedactionPolicy(sensitive_form_fields=['password'],
                                 fail_on_parse_error=False)
        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'), policy=policy)

        assert problems == []
        assert results[0].status == 'retained'
        assert any(a['action'] == 'skipped' for a in results[0].actions)

    def test_strict_mode_raises_on_failed_record(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        record = _request_record(writer, 'http://example.com/', [
            ('Content-Type', 'application/x-www-form-urlencoded'),
        ], body=b'password=hunter2&garbage')
        path = _write_to_file([record], tmp_path)

        with pytest.raises(RedactionError):
            sanitize_file(
                path, os.path.join(str(tmp_path), 'out.warc'),
                policy=RedactionPolicy(sensitive_form_fields=['password']),
                strict=True)


# ============================================================================
class TestDigestsAndLengths:
    def test_digests_recomputed_and_independently_verified(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        body = b'password=hunter2&keep=yes'
        record = _request_record(writer, 'http://example.com/', [
            ('Cookie', 'session=secret'),
            ('Content-Type', 'application/x-www-form-urlencoded'),
            ('Content-Length', str(len(body))),
        ], body=body)
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy(sensitive_form_fields=['password']))

        assert problems == []
        result = results[0]
        assert result.new_block_digest != result.old_block_digest
        # Re-read from disk and verify against the declared digests again.
        assert verify_redacted_archive(
            os.path.join(str(tmp_path), 'out.warc')) == []

    def test_retained_record_keeps_identical_digests(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        record = _response_record(writer, 'http://example.com/', [
            ('Content-Type', 'text/plain'),
        ], body=b'hello world')
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy())

        assert problems == []
        result = results[0]
        assert result.status == 'retained'
        assert result.new_block_digest == result.old_block_digest
        assert result.new_payload_digest == result.old_payload_digest

    def test_revisit_header_edit_preserves_payload_digest_reference(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        revisit_http = StatusAndHeaders('200 OK', [
            ('Content-Type', 'text/html'),
            ('Set-Cookie', 'session=secret'),
        ], protocol='HTTP/1.1')
        record = writer.create_revisit_record(
            'http://example.com/',
            digest='sha1:B6QJ6BNJ3R4B23XXMRKZKHLPGJY2VE4O',
            refers_to_uri='http://example.com/foo',
            refers_to_date='1999-01-01T00:00:00Z',
            http_headers=revisit_http,
        )
        path = _write_to_file([record], tmp_path)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc'),
            policy=RedactionPolicy())

        assert problems == []
        result = results[0]
        assert result.status == 'redacted'
        # The payload digest references the original capture and must not
        # be recomputed away during a header-only revisit edit.
        assert result.new_payload_digest == result.old_payload_digest
        # The block changed (Set-Cookie removed), so its digest changes.
        assert result.new_block_digest != result.old_block_digest
        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc'))
        names = {n.lower() for n, _ in out_records[0].http_headers.headers}
        assert 'set-cookie' not in names
        assert verify_redacted_archive(
            os.path.join(str(tmp_path), 'out.warc')) == []

    def test_gzip_output_passes_library_digest_check(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=True)
        record = _request_record(writer, 'http://example.com/', [
            ('Cookie', 'session=secret'),
            ('Content-Type', 'application/x-www-form-urlencoded'),
        ], body=b'password=hunter2&keep=1')
        path = _write_to_file([record], tmp_path, name='in.warc.gz', gzip=True)

        results, problems, _pre_existing = sanitize_file(
            path, os.path.join(str(tmp_path), 'out.warc.gz'),
            policy=RedactionPolicy(sensitive_form_fields=['password']),
            gzip=True)

        assert problems == []
        out_records = _read_records(os.path.join(str(tmp_path), 'out.warc.gz'))
        new_body = out_records[0].raw_stream.read()
        assert new_body == b'keep=1'
        assert b'hunter2' not in new_body


# ============================================================================
class TestManifestAndSafety:
    def test_manifest_maps_source_to_output_records(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        records = [
            _response_record(writer, 'http://example.com/a',
                             [('Content-Type', 'text/plain')], body=b'a'),
            _request_record(writer, 'http://example.com/b',
                            [('Cookie', 'x=y')], body=b''),
        ]
        path = _write_to_file(records, tmp_path)
        out_path = os.path.join(str(tmp_path), 'out.warc')
        manifest_path = os.path.join(str(tmp_path), 'map.jsonl')

        results, problems, _pre_existing = sanitize_file(
            path, out_path, policy=RedactionPolicy(),
            manifest_path=manifest_path)

        assert problems == []
        entries = [json.loads(line) for line in open(manifest_path)]
        assert len(entries) == 2
        assert entries[0]['status'] == 'retained'
        assert entries[1]['status'] == 'redacted'
        for entry in entries:
            assert entry['source']['record_id'] == entry['output']['record_id']
            assert entry['source']['offset'] is not None
            assert entry['output']['offset'] is not None
            assert 'digests' in entry

    def test_refuses_to_overwrite_input(self, tmp_path):
        path = os.path.join(str(tmp_path), 'same.warc')
        with open(path, 'wb') as fh:
            fh.write(b'')
        with pytest.raises(RedactionError):
            sanitize_file(path, path)

    def test_input_file_is_not_modified(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        record = _request_record(writer, 'http://example.com/', [
            ('Cookie', 'session=secret'),
        ], body=b'')
        path = _write_to_file([record], tmp_path)

        before = open(path, 'rb').read()
        os.chmod(path, 0o444)
        try:
            sanitize_file(path, os.path.join(str(tmp_path), 'out.warc'),
                          policy=RedactionPolicy())
        finally:
            os.chmod(path, 0o644)
        assert open(path, 'rb').read() == before


# ============================================================================
class TestCli:
    def test_redact_cli(self, tmp_path, capsys):
        writer = FixedTestWARCWriter(gzip=False)
        record = _request_record(writer, 'http://example.com/', [
            ('Cookie', 'session=secret'),
            ('Content-Type', 'application/x-www-form-urlencoded'),
        ], body=b'password=hunter2&keep=1')
        path = _write_to_file([record], tmp_path)
        out_path = os.path.join(str(tmp_path), 'out.warc')
        manifest = os.path.join(str(tmp_path), 'map.jsonl')

        main(args=['redact', path, out_path,
                   '--form-field', 'password',
                   '--manifest', manifest])

        captured = capsys.readouterr()
        assert '1 records processed' in captured.out
        assert '1 redacted' in captured.out
        assert verify_redacted_archive(out_path) == []
        entries = [json.loads(line) for line in open(manifest)]
        assert entries[0]['status'] == 'redacted'

    def test_redact_cli_strict_exit_code(self, tmp_path):
        writer = FixedTestWARCWriter(gzip=False)
        record = _request_record(writer, 'http://example.com/', [
            ('Content-Type', 'application/x-www-form-urlencoded'),
        ], body=b'password=hunter2&garbage')
        path = _write_to_file([record], tmp_path)

        with pytest.raises(SystemExit) as exc:
            main(args=['redact', path,
                       os.path.join(str(tmp_path), 'out.warc'),
                       '--form-field', 'password', '--strict'])
        assert exc.value.code == 1


# ============================================================================
class TestFixtureAcceptance:
    """Acceptance by re-reading output and verifying digests, across the
    real bundled archives (chunked framing, hex digests, ARC, truncation)."""

    def test_chunked_warc_with_hex_digests_verifies_clean(self, tmp_path):
        out = os.path.join(str(tmp_path), 'out.warc')
        results, introduced, pre_existing = sanitize_file(
            get_test_file('example-iana.org-chunked.warc'), out,
            policy=RedactionPolicy())

        assert introduced == []
        assert pre_existing == []
        # The source stores hex digests on chunked records; verification
        # must accept the encoding and the framing rather than false-fail.
        assert verify_redacted_archive(out) == []
        assert all(r.status == 'retained' for r in results)

    def test_pre_existing_broken_digest_is_not_counted_as_introduced(self, tmp_path):
        out = os.path.join(str(tmp_path), 'out.warc')
        results, introduced, pre_existing = sanitize_file(
            get_test_file('example-trunc.warc'), out,
            policy=RedactionPolicy(), strict=True)

        assert introduced == []
        assert pre_existing, 'expected the truncated source digest failure'
        flagged = [r for r in results if r.source_digest_failed]
        assert flagged and all(
            any(reason.startswith('pre-existing source issue')
                for reason in r.reasons) for r in flagged)

    def test_arc_input_round_trips_with_valid_digests(self, tmp_path):
        out = os.path.join(str(tmp_path), 'out.warc')
        results, introduced, pre_existing = sanitize_file(
            get_test_file('example.arc'), out, policy=RedactionPolicy())

        assert introduced == []
        assert pre_existing == []
        assert verify_redacted_archive(out) == []
        # ARC is normalized to WARC on read; the output must re-parse.
        assert len(_read_records(out)) == len(results)

    def test_gzipped_fixture_redaction_verifies(self, tmp_path):
        out = os.path.join(str(tmp_path), 'out.warc.gz')
        results, introduced, pre_existing = sanitize_file(
            get_test_file('post-test.warc.gz'), out,
            policy=RedactionPolicy(sensitive_form_fields=['test']),
            gzip=True)

        assert introduced == []
        assert pre_existing == []
        assert verify_redacted_archive(out) == []
        redacted = [r for r in results if r.status == 'redacted']
        assert redacted and all(
            r.new_block_digest != r.old_block_digest for r in redacted)
