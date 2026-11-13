#!/usr/bin/env python
# -*- coding: utf-8 -*-

import gzip
import pytest

from io import BytesIO

from warcio import (WARCSegmentReassembler, SegmentConflictError,
                    MissingSegmentError, SegmentReassemblyError,
                    ArchiveIterator)
from warcio.segmentreassembler import ReassembledRecord


HEAD_ID = '<urn:uuid:head-record-0001>'
URI = 'http://example.com/large-resource'
DATE = '2026-01-01T00:00:00Z'


def _warc_record(rec_type, record_id, block, extra_headers,
                 protocol='WARC/1.1'):
    lines = [protocol]
    lines.append('WARC-Type: ' + rec_type)
    lines.append('WARC-Record-ID: ' + record_id)
    lines.append('WARC-Target-URI: ' + URI)
    lines.append('WARC-Date: ' + DATE)
    for name, value in extra_headers:
        lines.append('{0}: {1}'.format(name, value))
    lines.append('Content-Length: ' + str(len(block)))
    lines.append('')
    lines.append('')
    return ('\r\n'.join(lines)).encode('utf-8') + block + b'\r\n\r\n'


def _head_record(block):
    return _warc_record('resource', HEAD_ID, block,
                        [('WARC-Segment-Number', '1'),
                         ('Content-Type', 'application/octet-stream')])


def _continuation_record(number, block, total_length=None, record_id=None):
    headers = [('WARC-Segment-Number', str(number)),
               ('WARC-Segment-Origin-ID', HEAD_ID)]
    if total_length is not None:
        headers.append(('WARC-Segment-Total-Length', str(total_length)))
    record_id = record_id or '<urn:uuid:cont-{0:04d}>'.format(number)
    return _warc_record('continuation', record_id, block, headers)


def _write_member(fileobj, raw_record, use_gzip):
    if use_gzip:
        with gzip.GzipFile(fileobj=fileobj, mode='wb') as gz:
            gz.write(raw_record)
    else:
        fileobj.write(raw_record)


def _write_segment_file(path, raw_records, use_gzip):
    with open(str(path), 'wb') as fileobj:
        for raw_record in raw_records:
            _write_member(fileobj, raw_record, use_gzip)


@pytest.fixture
def segments():
    blocks = [b'zero-one-' * 20, b'segment-two-' * 30, b'three-end-' * 25]
    total = sum(len(block) for block in blocks)
    raws = [_head_record(blocks[0]),
            _continuation_record(2, blocks[1]),
            _continuation_record(3, blocks[2], total_length=total)]
    return blocks, total, raws


def _reassemble(paths):
    reassembler = WARCSegmentReassembler(list(paths))
    return reassembler, reassembler.reassemble(HEAD_ID)


@pytest.mark.parametrize('use_gzip', [False, True])
def test_complete_reassembly(tmp_path, segments, use_gzip):
    blocks, total, raws = segments
    path = tmp_path / ('complete.warc' + ('.gz' if use_gzip else ''))
    _write_segment_file(path, raws, use_gzip)

    with WARCSegmentReassembler([str(path)]) as reassembler:
        result = reassembler.reassemble(HEAD_ID)

        assert isinstance(result, ReassembledRecord)
        assert result.is_complete()
        assert result.complete is True
        assert result.missing_segments == []
        assert result.declared_total_length == total
        assert result.actual_length == total
        assert result.length == total
        assert result.rec_headers.get_header('WARC-Segment-Number') is None
        assert result.rec_headers.get_header(
            'WARC-Segment-Total-Length') is None
        assert result.rec_headers.get_header('Content-Length') == str(total)

        body = result.raw_stream.read()
        assert body == b''.join(blocks)
        assert any('verified' in note for note in result.problems)

        infos = result.get_segment_info()
        assert [info['number'] for info in infos] == [1, 2, 3]
        assert all(info['source'].endswith(path.name) for info in infos)
        assert all(info['offset'] is not None for info in infos)
        assert result.segment_numbers() == [1, 2, 3]
        assert result.get_segment_info(2)['origin_id'] == HEAD_ID
        assert result.get_segment_info(99) is None
        assert result.sources == (str(path),)


def test_out_of_order_inputs(tmp_path, segments):
    blocks, total, raws = segments
    paths = []
    for index, order in enumerate([(2,), (3,), (1,)]):
        path = tmp_path / 'part-{0}.warc.gz'.format(index)
        _write_segment_file(path, [raws[number - 1] for number in order], True)
        paths.append(str(path))

    with WARCSegmentReassembler(paths) as reassembler:
        result = reassembler.reassemble(HEAD_ID)

        assert result.is_complete()
        assert result.raw_stream.read() == b''.join(blocks)
        sources = result.get_segment_info()
        assert [info['number'] for info in sources] == [1, 2, 3]
        contributing = {info['source'] for info in sources}
        assert contributing == set(paths)


def test_missing_middle_segment(tmp_path, segments):
    blocks, total, raws = segments
    paths = []
    for index, number in enumerate([1, 3]):
        path = tmp_path / 'gap-{0}.warc'.format(index)
        _write_segment_file(path, [raws[number - 1]], False)
        paths.append(str(path))

    with WARCSegmentReassembler(paths) as reassembler:
        result = reassembler.reassemble(HEAD_ID)

        assert not result.is_complete()
        assert result.missing_segments == [2]
        assert any('missing segment(s): 2' in note for note in result.problems)
        assert result.declared_total_length == total
        assert result.actual_length == len(blocks[0]) + len(blocks[2])

        stream = result.raw_stream
        prefix = stream.read(len(blocks[0]))
        assert prefix == blocks[0]
        with pytest.raises(MissingSegmentError) as exc:
            stream.read()
        assert exc.value.missing_numbers == [2]
        # the bytes after the gap must never be spliced onto segment 1
        stream2 = reassembler.reassemble(HEAD_ID).raw_stream
        with pytest.raises(MissingSegmentError):
            stream2.read()
        assert blocks[2] not in blocks[0]


def test_missing_tail_segment(tmp_path, segments):
    blocks, total, raws = segments
    path = tmp_path / 'no-tail.warc'
    _write_segment_file(path, [raws[0], raws[1]], False)

    with WARCSegmentReassembler([str(path)]) as reassembler:
        result = reassembler.reassemble(HEAD_ID)

        assert not result.is_complete()
        # the number of trailing segments is unknowable without the terminal
        # total-length marker; the verdict must say so instead of guessing
        assert result.missing_segments == []
        assert any('end condition not met' in note
                   for note in result.problems)
        assert any('possibly missing' in note for note in result.problems)
        assert result.raw_stream.read() == blocks[0] + blocks[1]


def test_identical_duplicate_accepted(tmp_path, segments):
    blocks, total, raws = segments
    first = tmp_path / 'a.warc.gz'
    second = tmp_path / 'b.warc.gz'
    _write_segment_file(first, [raws[0], raws[1]], True)
    _write_segment_file(second, [raws[1], raws[2]], True)

    with WARCSegmentReassembler([str(first), str(second)]) as reassembler:
        result = reassembler.reassemble(HEAD_ID)

        assert result.is_complete()
        assert result.raw_stream.read() == b''.join(blocks)
        duplicate = result.get_segment_info(2)
        assert str(second) in duplicate['duplicate_sources']


def test_conflicting_duplicate_rejected(tmp_path, segments):
    blocks, total, raws = segments
    first = tmp_path / 'a.warc'
    second = tmp_path / 'b.warc'
    conflict_block = b'TAMPERED-' * 40
    conflicting = _continuation_record(2, conflict_block)
    _write_segment_file(first, [raws[0], raws[1]], False)
    _write_segment_file(second, [conflicting, raws[2]], False)

    reassembler = WARCSegmentReassembler([str(first), str(second)])
    with pytest.raises(SegmentConflictError) as exc:
        reassembler.scan()
    assert exc.value.segment_number == 2
    assert exc.value.origin_id == HEAD_ID
    assert 'conflicting duplicate segment 2' in str(exc.value)


def test_declared_total_length_mismatch(tmp_path, segments):
    blocks, total, raws = segments
    wrong_total = total + 7
    raws_wrong = [raws[0], raws[1],
                  _continuation_record(3, blocks[2], total_length=wrong_total)]
    path = tmp_path / 'wrong-length.warc'
    _write_segment_file(path, raws_wrong, False)

    with WARCSegmentReassembler([str(path)]) as reassembler:
        result = reassembler.reassemble(HEAD_ID)

        assert not result.is_complete()
        assert result.declared_total_length == wrong_total
        assert result.actual_length == total
        assert any('declared total length' in note
                   for note in result.problems)
        assert result.raw_stream.read() == b''.join(blocks)


def test_orphan_continuation_without_head(tmp_path, segments):
    blocks, total, raws = segments
    path = tmp_path / 'orphan.warc'
    _write_segment_file(path, [raws[1], raws[2]], False)

    with WARCSegmentReassembler([str(path)]) as reassembler:
        with pytest.raises(SegmentReassemblyError) as exc:
            reassembler.reassemble(HEAD_ID)
        assert 'first (identity) segment is missing' in str(exc.value)


def test_continuation_without_origin_id_rejected(tmp_path, segments):
    block = b'no-origin'
    raw = _warc_record('continuation', '<urn:uuid:lost>', block,
                       [('WARC-Segment-Number', '2')])
    path = tmp_path / 'no-origin.warc'
    _write_segment_file(path, [raw], False)

    with pytest.raises(SegmentConflictError):
        WARCSegmentReassembler([str(path)]).scan()


def test_head_must_be_segment_number_one(tmp_path, segments):
    raw = _warc_record('resource', HEAD_ID, b'bad-head',
                       [('WARC-Segment-Number', '2')])
    path = tmp_path / 'bad-head.warc'
    _write_segment_file(path, [raw], False)

    with pytest.raises(SegmentConflictError) as exc:
        WARCSegmentReassembler([str(path)]).scan()
    assert exc.value.segment_number == 2


def test_continuation_number_one_rejected(tmp_path, segments):
    raw = _continuation_record(1, b'wrong-number')
    path = tmp_path / 'cont-one.warc'
    _write_segment_file(path, [raw], False)

    with pytest.raises(SegmentConflictError) as exc:
        WARCSegmentReassembler([str(path)]).scan()
    assert exc.value.segment_number == 1


def test_duplicate_with_conflicting_marker_rejected(tmp_path, segments):
    blocks, total, raws = segments
    first = tmp_path / 'a.warc'
    second = tmp_path / 'b.warc'
    remarkered = _continuation_record(2, blocks[1], total_length=total + 1)
    _write_segment_file(first, [raws[0], raws[1]], False)
    _write_segment_file(second, [remarkered, raws[2]], False)

    with pytest.raises(SegmentConflictError) as exc:
        WARCSegmentReassembler([str(first), str(second)]).scan()
    assert exc.value.segment_number == 2


def test_unsegmented_records_pass_through(tmp_path, segments):
    blocks, total, raws = segments
    plain = _warc_record(
        'resource', '<urn:uuid:plain>', b'ordinary-record-body',
        [('Content-Type', 'text/plain')])
    path = tmp_path / 'mixed.warc'
    _write_segment_file(path, [plain, raws[0], raws[1], raws[2]], False)

    with WARCSegmentReassembler([str(path)]) as reassembler:
        ordinary = reassembler.unsegmented_records()
        assert len(ordinary) == 1
        assert ordinary[0].raw_stream.read() == b'ordinary-record-body'

        result = reassembler.reassemble(HEAD_ID)
        assert result.is_complete()
        assert result.raw_stream.read() == b''.join(blocks)


def test_iteration_and_file_object_input(segments):
    blocks, total, raws = segments
    buffer = BytesIO()
    for raw in raws:
        _write_member(buffer, raw, True)
    buffer.seek(0)

    with WARCSegmentReassembler([buffer]) as reassembler:
        results = list(reassembler)
        assert len(results) == 1
        result = results[0]
        assert result.is_complete()
        assert result.raw_stream.read() == b''.join(blocks)
        assert result.get_segment_info(1)['source'] == '<stream>'


def test_reassembled_record_iterates_like_warc(tmp_path, segments):
    blocks, total, raws = segments
    path = tmp_path / 'complete.warc'
    _write_segment_file(path, raws, False)

    with WARCSegmentReassembler([str(path)]) as reassembler:
        result = reassembler.reassemble(HEAD_ID)
        assert result.format == 'warc'
        assert result.rec_type == 'resource'
        assert result.rec_headers.get_header(
            'WARC-Target-URI') == URI
        # raw_stream is a genuine streaming, non-buffering reader
        assert result.raw_stream.readable()
        chunked = b''.join(iter(lambda: result.raw_stream.read(17), b''))
        assert chunked == b''.join(blocks)


def test_duplicate_physical_file_records(tmp_path, segments):
    blocks, total, raws = segments
    path = tmp_path / 'twice.warc.gz'
    _write_segment_file(path, raws + raws, True)

    with WARCSegmentReassembler([str(path)]) as reassembler:
        result = reassembler.reassemble(HEAD_ID)
        assert result.is_complete()
        assert result.raw_stream.read() == b''.join(blocks)
        assert len(result.get_segment_info()) == 3
