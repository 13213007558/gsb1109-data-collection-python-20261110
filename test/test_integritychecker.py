"""Tests for the layered offline integrity checker and its contrast fixtures."""

import json
from io import BytesIO

import pytest

from warcio.cli import main
from warcio.integritychecker import (
    IntegrityChecker,
    VERDICT_COMPLETE,
    VERDICT_INCOMPLETE,
    VERDICT_INCONCLUSIVE,
    VERDICT_NOT_APPLICABLE,
)

from . import get_test_file
from . import integrityfixtures as fx


def _check(data, name='synthetic.warc'):
    checker = IntegrityChecker()
    reports, file_error = checker.check_file(name, BytesIO(data))
    return reports, file_error


def _by_id(reports):
    return {report.warc_record_id: report for report in reports}


def _layer(report, name):
    return report.layers[name]


@pytest.fixture(scope='module', autouse=True)
def _ensure_contrast_fixtures():
    # Idempotently materialize the hand-built contrast archives so the CLI
    # tests can address them through the shared test/data directory.
    fx.write_fixtures()


# ============================================================================
# Positive control
# ============================================================================
def test_complete_control_is_complete_on_every_layer():
    reports, file_error = _check(fx.fixture_complete_control())
    assert file_error is None
    assert [report.verdict for report in reports] == [
        VERDICT_COMPLETE, VERDICT_COMPLETE]

    response = reports[0]
    assert _layer(response, 'warc').verdict == VERDICT_COMPLETE
    assert _layer(response, 'http').verdict == VERDICT_COMPLETE
    assert _layer(response, 'payload').verdict == VERDICT_COMPLETE

    # The three lengths count different layers' octets.
    warc_metrics = _layer(response, 'warc').metrics
    http_metrics = _layer(response, 'http').metrics
    payload_metrics = _layer(response, 'payload').metrics
    assert warc_metrics['declared_block_length'] == \
        warc_metrics['actual_block_bytes']
    assert http_metrics['content_length_declared'] == \
        http_metrics['wire_body_bytes_actual']
    assert payload_metrics['decoded_payload_bytes'] == \
        len(b'<html><body>complete control body</body></html>')
    # Every verdict is traceable to a concrete evidence line.
    expected_length = http_metrics['content_length_declared']
    assert any('Content-Length {0} matches'.format(expected_length) in line
               for line in response.evidence)


def test_real_valid_archives_are_fully_complete():
    for path in ('example.warc', 'example.warc.gz'):
        reports, file_error = _check(
            open(get_test_file(path), 'rb').read(), path)
        assert file_error is None
        assert all(report.verdict == VERDICT_COMPLETE for report in reports)


def test_real_chunked_archive_frames_and_decodes_cleanly():
    reports, file_error = _check(
        open(get_test_file('example-iana.org-chunked.warc'), 'rb').read())
    assert file_error is None
    response = next(report for report in reports
                    if report.warc_type == 'response')
    assert _layer(response, 'http').metrics['framing'] == 'chunked'
    assert _layer(response, 'http').verdict == VERDICT_COMPLETE
    assert _layer(response, 'http').metrics['stored_body_digest_match'] is True
    assert _layer(response, 'payload').verdict == VERDICT_COMPLETE


# ============================================================================
# The core requirement: an intact outer layer must not vouch for the inner one
# ============================================================================
def test_inner_truncation_keeps_warc_layer_complete():
    reports, _ = _check(fx.fixture_inner_truncated())
    by_id = _by_id(reports)

    cl_report = by_id['<urn:uuid:inner-cl-response>']
    assert _layer(cl_report, 'warc').verdict == VERDICT_COMPLETE
    assert _layer(cl_report, 'http').verdict == VERDICT_INCOMPLETE
    assert _layer(cl_report, 'payload').verdict == VERDICT_INCOMPLETE
    assert cl_report.verdict == VERDICT_INCOMPLETE
    http_metrics = _layer(cl_report, 'http').metrics
    # HTTP Content-Length (100) is about wire body octets, not WARC block ones.
    assert http_metrics['content_length_declared'] == 100
    actual = http_metrics['wire_body_bytes_actual']
    assert actual < 100
    assert any(('{0} missing'.format(100 - actual)) in reason
               for reason in _layer(cl_report, 'http').reasons)
    assert any('HTTP body interrupted' in reason
               for reason in _layer(cl_report, 'http').reasons)

    chunk_report = by_id['<urn:uuid:inner-chunk-response>']
    assert _layer(chunk_report, 'warc').verdict == VERDICT_COMPLETE
    assert _layer(chunk_report, 'http').verdict == VERDICT_INCOMPLETE
    assert any('interrupted inside chunk' in reason
               for reason in _layer(chunk_report, 'http').reasons)

    good_report = by_id['<urn:uuid:inner-good-response>']
    assert good_report.verdict == VERDICT_COMPLETE


def test_outer_truncation_is_a_warc_layer_failure():
    reports, file_error = _check(fx.fixture_outer_truncated())
    assert file_error is not None
    cut = next(report for report in reports
               if report.warc_record_id == '<urn:uuid:outer-cut>')
    assert _layer(cut, 'warc').verdict == VERDICT_INCOMPLETE
    # Without a full block the inner message cannot be judged either way.
    assert _layer(cut, 'http').verdict == VERDICT_INCONCLUSIVE
    assert _layer(cut, 'payload').verdict == VERDICT_INCONCLUSIVE
    assert any('record block truncated' in reason
               for reason in _layer(cut, 'warc').reasons)


def test_gzip_member_truncation_is_detected_at_the_envelope():
    reports, file_error = _check(fx.fixture_gzip_member_truncated())
    assert file_error is not None
    assert 'gzip member deflate stream is truncated' in file_error
    member = reports[-1]
    assert _layer(member, 'warc').verdict == VERDICT_INCOMPLETE
    assert reports[0].verdict == VERDICT_COMPLETE


def test_gzip_trailer_corruption_is_detected_but_deflate_still_parses():
    gz = fx.build_warc_gz([
        fx.warc_record('warcinfo', None, b'abc\r\n',
                       record_id='<urn:uuid:gz-a>'),
        fx.warc_record('warcinfo', None, b'def\r\n',
                       record_id='<urn:uuid:gz-b>'),
    ])
    corrupted = bytearray(gz)
    corrupted[-7] ^= 0xFF  # flip a CRC byte in the final member trailer
    reports, file_error = _check(bytes(corrupted))
    assert 'gzip trailer mismatch' in file_error
    assert reports[-1].layers['warc'].verdict == VERDICT_INCOMPLETE
    assert reports[0].verdict == VERDICT_COMPLETE


def test_gzip_checker_works_on_a_non_seekable_stream():
    from warcio.archiveiterator import UnseekableYetTellable

    data = open(get_test_file('example.warc.gz'), 'rb').read()
    stream = UnseekableYetTellable(BytesIO(data))
    reports, file_error = IntegrityChecker().check_file('stream', stream)
    assert file_error is None
    assert len(reports) == 6
    assert all(report.verdict == VERDICT_COMPLETE for report in reports)


# ============================================================================
# HEAD and body-less status codes are distinct from truncation
# ============================================================================
def test_head_response_with_empty_body_is_complete():
    reports, _ = _check(fx.fixture_head())
    response, request = reports
    assert request.layers['http'].metrics['method'] == 'HEAD'
    assert _layer(response, 'http').verdict == VERDICT_COMPLETE
    assert _layer(response, 'http').metrics['framing'] == 'head-response'
    assert _layer(response, 'payload').verdict == VERDICT_NOT_APPLICABLE
    assert any('HEAD' in line for line in response.evidence)


def test_bodyless_status_codes_are_complete_with_zero_body():
    reports, _ = _check(fx.fixture_nobody_status())
    assert all(report.verdict == VERDICT_COMPLETE for report in reports)
    assert _layer(reports[0], 'http').metrics['framing'] == \
        'no-body-status-204'
    assert _layer(reports[1], 'http').metrics['framing'] == \
        'no-body-status-304'


def test_body_on_a_204_is_inconclusive_not_complete():
    reports, _ = _check(fx.fixture_conflicting_lengths())
    body_204 = _by_id(reports)['<urn:uuid:conflict-204>']
    assert _layer(body_204, 'http').verdict == VERDICT_INCONCLUSIVE
    assert any('must not carry a body' in reason
               for reason in _layer(body_204, 'http').reasons)


# ============================================================================
# Transfer interruptions, including a content-encoding cut behind valid HTTP
# ============================================================================
def test_transfer_interruption_variants():
    reports, _ = _check(fx.fixture_transfer_interrupted())
    by_id = _by_id(reports)

    no_terminal = by_id['<urn:uuid:interrupt-chunk>']
    assert _layer(no_terminal, 'http').verdict == VERDICT_INCOMPLETE
    assert any('before the terminal' in reason
               for reason in _layer(no_terminal, 'http').reasons)

    short_chunk = by_id['<urn:uuid:interrupt-short-chunk>']
    assert _layer(short_chunk, 'http').verdict == VERDICT_INCOMPLETE

    gzip_cut = by_id['<urn:uuid:interrupt-gzip>']
    # HTTP framing itself is sound; only the decoded payload is broken.
    assert _layer(gzip_cut, 'http').verdict == VERDICT_COMPLETE
    assert _layer(gzip_cut, 'payload').verdict == VERDICT_INCOMPLETE
    assert any('gzip content-encoding stream is truncated' in reason
               for reason in _layer(gzip_cut, 'payload').reasons)
    assert gzip_cut.verdict == VERDICT_INCOMPLETE


# ============================================================================
# Contradictory length declarations and undelimited bodies
# ============================================================================
def test_conflicting_lengths_are_inconclusive_without_guessing():
    reports, _ = _check(fx.fixture_conflicting_lengths())
    by_id = _by_id(reports)

    both = by_id['<urn:uuid:conflict-both>']
    assert _layer(both, 'http').verdict == VERDICT_INCONCLUSIVE
    assert _layer(both, 'http').metrics['framing'] == \
        'conflicting-transfer-length'
    # Chunk delimiters are intact, so the decodable body is still complete.
    assert _layer(both, 'payload').verdict == VERDICT_COMPLETE
    assert both.verdict == VERDICT_INCONCLUSIVE

    duplicate = by_id['<urn:uuid:conflict-dup>']
    assert _layer(duplicate, 'http').verdict == VERDICT_INCONCLUSIVE
    assert any('contradictory Content-Length' in reason
               for reason in _layer(duplicate, 'http').reasons)

    invalid = by_id['<urn:uuid:conflict-invalid>']
    assert _layer(invalid, 'http').verdict == VERDICT_INCONCLUSIVE
    assert _layer(invalid, 'http').metrics['framing'] == \
        'invalid-content-length'


def test_close_delimited_body_is_inconclusive():
    reports, _ = _check(fx.fixture_close_delimited())
    report = reports[0]
    assert _layer(report, 'warc').verdict == VERDICT_COMPLETE
    assert _layer(report, 'http').verdict == VERDICT_INCONCLUSIVE
    assert _layer(report, 'http').metrics['framing'] == 'close-delimited'
    assert _layer(report, 'payload').verdict == VERDICT_INCONCLUSIVE
    assert any('cannot vouch' in line for line in report.evidence)


# ============================================================================
# Digest cross-checks on real archives
# ============================================================================
def test_block_and_payload_digests_are_verified_on_their_own_layers():
    reports, _ = _check(
        open(get_test_file('example.warc'), 'rb').read())
    response = next(report for report in reports
                    if report.warc_type == 'response')
    assert _layer(response, 'warc').metrics['block_digest_match'] is True
    assert _layer(response, 'http').metrics['stored_body_digest_match'] is True

    revisit = next(report for report in reports
                   if report.warc_type == 'revisit')
    # Revisit digests point at the referenced capture, not this block.
    assert any('revisit record' in reason
               for reason in _layer(revisit, 'warc').reasons)
    assert any('revisit record' in reason
               for reason in _layer(revisit, 'http').reasons)


def test_deliberately_bad_digest_is_reported_incomplete():
    reports, _ = _check(
        open(get_test_file('example-digest.warc'), 'rb').read())
    bad = reports[0]
    assert _layer(bad, 'http').verdict == VERDICT_INCOMPLETE
    assert any('WARC-Payload-Digest mismatch' in reason
               for reason in _layer(bad, 'http').reasons)


def test_real_truncated_archive_fails_inner_and_outer_checks():
    reports, _ = _check(
        open(get_test_file('example-trunc.warc'), 'rb').read())
    response = next(report for report in reports
                    if report.warc_type == 'response')
    assert _layer(response, 'warc').verdict == VERDICT_INCOMPLETE
    assert _layer(response, 'http').verdict == VERDICT_INCOMPLETE
    assert _layer(response, 'payload').verdict == VERDICT_INCOMPLETE
    # Scanning resynchronizes and still reaches the trailing request record.
    assert reports[-1].warc_type == 'request'
    assert reports[-1].verdict == VERDICT_COMPLETE


# ============================================================================
# Reports are traceable and machine-readable
# ============================================================================
def test_report_dict_carries_identity_layers_and_evidence():
    reports, _ = _check(fx.fixture_complete_control())
    payload = reports[0].to_dict()
    assert payload['offset'] == 0
    assert payload['warc_record_id'] == '<urn:uuid:control-response>'
    assert set(payload['layers']) == {'warc', 'http', 'payload'}
    for layer in payload['layers'].values():
        assert set(layer) == {'layer', 'verdict', 'metrics', 'reasons'}
    assert payload['evidence']


def test_cli_json_output_and_only_filter(capsys):
    paths = [get_test_file('integrity/fixture-inner-truncated.warc')]
    with pytest.raises(SystemExit) as exc:
        main(args=['integrity', '--json'] + paths)
    assert exc.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert data['summary']['incomplete'] == 2
    assert data['summary']['complete'] == 1
    assert len(data['reports']) == 3
    assert all(set(report) >= {'file', 'record_index', 'offset', 'verdict',
                               'layers', 'evidence'}
               for report in data['reports'])

    with pytest.raises(SystemExit) as exc:
        main(args=['integrity', '--only', 'incomplete', '-q'] + paths)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    record_lines = [line for line in out.splitlines()
                    if line.startswith('  #')]
    assert record_lines
    assert all('INCOMPLETE' in line for line in record_lines)


def test_cli_complete_archive_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(args=['integrity', '-q',
                   get_test_file('integrity/fixture-complete-control.warc')])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert 'COMPLETE' in out


def test_input_file_is_opened_read_only(tmp_path):
    source = tmp_path / 'copy.warc'
    source.write_bytes(fx.fixture_inner_truncated())
    before = source.stat().st_mtime_ns
    reports, file_error = IntegrityChecker().check_file(
        str(source), open(str(source), 'rb'))
    assert source.stat().st_mtime_ns == before
    assert source.read_bytes() == fx.fixture_inner_truncated()
    assert any(report.verdict == VERDICT_INCOMPLETE for report in reports)


def test_non_warc_input_is_inconclusive_not_crash():
    reports, file_error = _check(b'this is definitely not a warc\n')
    assert file_error is None
    assert reports[0].verdict == VERDICT_INCONCLUSIVE
    assert 'not a WARC file' in reports[0].layers['warc'].reasons[0]


# ============================================================================
# Additional boundary cases
# ============================================================================
def test_warc_truncated_header_marks_capture_time_truncation():
    block = fx.http_response('200 OK', [('Content-Length', '5')],
                             body=b'hello')
    record = fx.warc_record(
        'response', 'http://example.com/t', block,
        extra_headers=[('WARC-Truncated', 'length')])
    reports, _ = _check(fx.build_warc([record]))
    assert _layer(reports[0], 'warc').verdict == VERDICT_INCOMPLETE
    assert any('WARC-Truncated' in reason
               for reason in _layer(reports[0], 'warc').reasons)


def test_chunked_then_gzip_decoding_chain_is_checked_per_layer():
    import gzip

    encoded = gzip.compress(b'combined encoding test ' * 20)
    chunked = b''
    for index in range(0, len(encoded), 64):
        piece = encoded[index:index + 64]
        chunked += ('{0:x}\r\n'.format(len(piece))).encode() + piece + b'\r\n'
    chunked += b'0\r\n\r\n'

    block = fx.http_response(
        '200 OK',
        [('Transfer-Encoding', 'chunked'),
         ('Content-Encoding', 'gzip')],
        body=chunked)
    record = fx.warc_record('response', 'http://example.com/cg', block)
    reports, _ = _check(fx.build_warc([record]))
    report = reports[0]
    assert _layer(report, 'http').metrics['framing'] == 'chunked'
    assert _layer(report, 'http').verdict == VERDICT_COMPLETE
    assert _layer(report, 'payload').metrics['content_encoding'] == 'gzip'
    assert _layer(report, 'payload').verdict == VERDICT_COMPLETE
    assert _layer(report, 'payload').metrics['decoded_payload_bytes'] == 460


def test_orphan_head_like_response_without_request_is_inconclusive():
    block = fx.http_response('200 OK', [('Content-Length', '55')], body=b'')
    record = fx.warc_record('response', 'http://example.com/h', block,
                            record_id='<urn:uuid:orphan-head>')
    reports, _ = _check(fx.build_warc([record]))
    http_layer = _layer(reports[0], 'http')
    assert http_layer.verdict == VERDICT_INCONCLUSIVE
    assert any('HEAD response would explain' in reason
               for reason in http_layer.reasons)


def test_truncated_deflate_stream_fails_only_the_payload_layer():
    import zlib

    compressor = zlib.compressobj()
    full = compressor.compress(b'deflate ' * 500) + compressor.flush()
    partial = full[:30]
    block = fx.http_response(
        '200 OK',
        [('Content-Encoding', 'deflate'),
         ('Content-Length', str(len(partial)))],
        body=partial)
    record = fx.warc_record('response', 'http://example.com/d', block)
    reports, _ = _check(fx.build_warc([record]))
    report = reports[0]
    assert _layer(report, 'http').verdict == VERDICT_COMPLETE
    assert _layer(report, 'payload').verdict == VERDICT_INCOMPLETE
    assert any('deflate content-encoding stream is truncated' in reason
               for reason in _layer(report, 'payload').reasons)


def test_invalid_warc_content_length_is_inconclusive_not_crash():
    block = fx.http_response('200 OK', [('Content-Length', '3')],
                             body=b'abc')
    record = fx.warc_record('response', 'http://example.com/bad-warc',
                            block, declared_length='not-an-int')
    reports, file_error = _check(fx.build_warc([record]))
    assert file_error is not None
    assert _layer(reports[0], 'warc').verdict == VERDICT_INCONCLUSIVE
