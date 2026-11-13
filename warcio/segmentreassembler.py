"""
Offline reassembly of segmented WARC records (WARC 1.1).

A large response may be split by the crawler into a first WARC record
(``WARC-Segment-Number: 1``) and one or more ``continuation`` records that
carry ``WARC-Segment-Origin-ID`` pointing at the first record. The last
segment is marked with ``WARC-Segment-Total-Length``.

The :class:`WARCSegmentReassembler` accepts the input files in any order,
groups the pieces back together and validates:

* the identity of the first (head) record and every continuation's
  ``WARC-Segment-Origin-ID``;
* the ``WARC-Segment-Number`` sequence;
* the declared ``WARC-Segment-Total-Length`` against the bytes actually
  reassembled;
* the end condition (the highest segment carries the total length marker).

Duplicate copies of a segment are tolerated only when they are byte for byte
identical; conflicting copies raise :class:`SegmentConflictError`. A missing
middle segment never results in the surrounding bytes being concatenated:
the reassembled record reports the gap and its stream raises
:class:`MissingSegmentError` when the gap is reached.
"""

import os
import tempfile

from io import RawIOBase

from warcio.archiveiterator import ArchiveIterator
from warcio.exceptions import (SegmentConflictError, MissingSegmentError,
                               SegmentReassemblyError)
from warcio.recordloader import ArcWarcRecord
from warcio.statusandheaders import StatusAndHeaders
from warcio.utils import BUFF_SIZE


# ============================================================================
class SegmentInfo(object):
    """Provenance and metadata for one physical WARC segment.

    The segment block itself is spooled in ``spool`` (a seekable temporary
    file) so the reassembled record can be streamed without keeping the whole
    logical record in memory.
    """

    def __init__(self, number, record_id, origin_id, block_length,
                 total_length, is_head, is_terminal, source, offset,
                 member_length, spool, content_type):
        self.number = number
        self.record_id = record_id
        self.origin_id = origin_id
        self.block_length = block_length
        self.total_length = total_length
        self.is_head = is_head
        self.is_terminal = is_terminal
        self.source = source
        self.offset = offset
        self.member_length = member_length
        self.spool = spool
        self.content_type = content_type
        self.duplicate_sources = []

    def as_dict(self):
        """Return the queryable metadata without the spooled data stream."""
        return {
            'number': self.number,
            'record_id': self.record_id,
            'origin_id': self.origin_id,
            'block_length': self.block_length,
            'total_length': self.total_length,
            'is_head': self.is_head,
            'is_terminal': self.is_terminal,
            'source': self.source,
            'offset': self.offset,
            'member_length': self.member_length,
            'content_type': self.content_type,
            'duplicate_sources': list(self.duplicate_sources),
        }

    def close(self):
        if self.spool is not None:
            self.spool.close()
            self.spool = None


# ============================================================================
class _SegmentGroup(object):
    """Accumulates the segments of one logical record, keyed by origin id."""

    def __init__(self, origin_id):
        self.origin_id = origin_id
        self.head = None
        self.segments = {}
        self.head_record = None

    def add(self, info, record=None):
        if info.is_head:
            if self.head is not None:
                self._check_duplicate(self.head, info)
                self.head.duplicate_sources.append(info.source)
                info.close()
                return
            self.head = info
            self.head_record = record
        else:
            existing = self.segments.get(info.number)
            if existing is not None:
                self._check_duplicate(existing, info)
                existing.duplicate_sources.append(info.source)
                info.close()
                return
            self.segments[info.number] = info

    @staticmethod
    def _check_duplicate(existing, candidate):
        existing.spool.seek(0)
        candidate.spool.seek(0)
        same_bytes = existing.spool.read() == candidate.spool.read()
        if (not same_bytes or
                existing.total_length != candidate.total_length or
                existing.is_terminal != candidate.is_terminal or
                existing.record_id != candidate.record_id):
            msg = ('conflicting duplicate segment {num} for record {rid}: '
                   'copies from {src1} and {src2} do not match').format(
                        num=candidate.number, rid=candidate.origin_id,
                        src1=existing.source, src2=candidate.source)
            raise SegmentConflictError(msg, segment_number=candidate.number,
                                       origin_id=candidate.origin_id)


# ============================================================================
class SegmentedBlockStream(RawIOBase):
    """Read-only stream over the spooled segments of a logical record.

    Segments are served in segment-number order. If the next available segment
    does not directly follow the current one, a :class:`MissingSegmentError`
    is raised rather than joining the bytes on either side of the gap.
    """

    def __init__(self, segments):
        self._segments = segments
        self._index = 0
        self._current = segments[0].spool if segments else None
        if self._current is not None:
            self._current.seek(0)

    def readable(self):
        return True

    def read(self, size=-1):
        if self._current is None:
            return b''

        if size is None or size < 0:
            chunks = []
            while True:
                buf = self._current.read(BUFF_SIZE)
                if not buf:
                    if not self._advance():
                        break
                    continue
                chunks.append(buf)
            return b''.join(chunks)

        result = bytearray()
        remaining = size
        while remaining > 0:
            buf = self._current.read(remaining)
            if buf:
                result.extend(buf)
                remaining -= len(buf)
                continue
            if not self._advance():
                break
        return bytes(result)

    def readinto(self, byte_buffer):
        data = self.read(len(byte_buffer))
        byte_buffer[:len(data)] = data
        return len(data)

    def _advance(self):
        if self._index + 1 >= len(self._segments):
            self._current = None
            return False

        previous_number = self._segments[self._index].number
        next_info = self._segments[self._index + 1]
        if next_info.number != previous_number + 1:
            missing = list(range(previous_number + 1, next_info.number))
            msg = ('segment gap for record {rid}: missing segment(s) '
                   '{missing}, cannot concatenate segments {prev} and '
                   '{nxt}').format(rid=next_info.origin_id,
                                   missing=','.join(str(n) for n in missing),
                                   prev=previous_number, nxt=next_info.number)
            raise MissingSegmentError(msg, missing_numbers=missing)

        self._index += 1
        self._current = self._segments[self._index].spool
        self._current.seek(0)
        return True

    def close(self):
        # The spooled segments are owned by the reassembler so that the same
        # logical record can be reassembled more than once; releasing them is
        # the caller's responsibility via WARCSegmentReassembler.close().
        self._segments = []
        self._current = None
        super(SegmentedBlockStream, self).close()


# ============================================================================
class ReassembledRecord(ArcWarcRecord):
    """A logical WARC record rebuilt from one or more physical segments.

    Beyond the regular :class:`ArcWarcRecord` interface, the object carries
    the reassembly verdict (``is_complete`` / ``problems``), the declared and
    actual block length, the missing segment numbers and per-segment
    provenance which remains queryable after the scan.
    """

    def __init__(self, head_record, raw_stream, length, segment_infos,
                 complete, problems, declared_total_length):
        # clone so rebuilding the same record does not mutate the head
        # record retained by the reassembler between builds
        rec_headers = StatusAndHeaders(
            head_record.rec_headers.statusline,
            list(head_record.rec_headers.headers),
            protocol=head_record.rec_headers.protocol)
        rec_headers.remove_header('WARC-Segment-Number')
        rec_headers.remove_header('WARC-Segment-Total-Length')
        rec_headers.replace_header('Content-Length', str(length))

        super(ReassembledRecord, self).__init__(
            head_record.format, head_record.rec_type, rec_headers,
            raw_stream, head_record.http_headers,
            head_record.content_type, length)

        self._segment_infos = tuple(segment_infos)
        self._complete = complete
        self._problems = tuple(problems)
        self.declared_total_length = declared_total_length
        self.actual_length = length
        self.missing_segments = self._compute_missing_segments()

    def _compute_missing_segments(self):
        present = sorted(info.number for info in self._segment_infos)
        if not present:
            return []
        return [number for number in range(1, present[-1] + 1)
                if number not in present]

    def is_complete(self):
        """Return ``True`` when every validation check succeeded."""
        return self._complete

    @property
    def complete(self):
        return self._complete

    @property
    def problems(self):
        """Human-readable explanations when ``is_complete()`` is False."""
        return self._problems

    def segment_numbers(self):
        """Segment numbers that were actually found, in ascending order."""
        return sorted(info.number for info in self._segment_infos)

    def get_segment_info(self, number=None):
        """Provenance for one segment, or all segments when ``number`` is None.
        """
        if number is None:
            return tuple(info.as_dict() for info in self._segment_infos)
        for info in self._segment_infos:
            if info.number == number:
                return info.as_dict()
        return None

    @property
    def sources(self):
        """Distinct input files that contributed segments."""
        names = []
        for info in self._segment_infos:
            if info.source not in names:
                names.append(info.source)
        return tuple(names)


# ============================================================================
class WARCSegmentReassembler(object):
    """Scan WARC inputs and rebuild their segmented records.

    ``inputs`` is a path, a readable file object or an iterable of either.
    Plain (uncompressed) and gzip chunk WARC files are both accepted and the
    inputs may be supplied in any order.
    """

    SEGMENT_HEADER = 'WARC-Segment-Number'
    ORIGIN_HEADER = 'WARC-Segment-Origin-ID'
    TOTAL_LENGTH_HEADER = 'WARC-Segment-Total-Length'

    def __init__(self, inputs, block_size=BUFF_SIZE):
        self.inputs = self._normalize_inputs(inputs)
        self.block_size = block_size
        self.groups = {}
        self.unsegmented = []
        self._scanned = False

    # -- public API ---------------------------------------------------------

    def scan(self):
        """Read every input once and index the segments. Returns self."""
        for source in self.inputs:
            self._scan_source(source)
        self._scanned = True
        return self

    def reassemble(self, origin_id=None):
        """Return a single :class:`ReassembledRecord`.

        ``origin_id`` selects the logical record; it may be omitted only when
        exactly one segmented record was found.
        """
        self._ensure_scanned()
        if origin_id is None:
            if len(self.groups) != 1:
                raise SegmentReassemblyError(
                    'origin_id is required when {0} segmented records were '
                    'found'.format(len(self.groups)))
            origin_id = next(iter(self.groups))
        return self._build_record(self._get_group(origin_id))

    def reassemble_all(self):
        """Return every reassembled segmented record, origin id sorted."""
        self._ensure_scanned()
        return [self._build_record(self._get_group(origin_id))
                for origin_id in sorted(self.groups)]

    def __iter__(self):
        self._ensure_scanned()
        self._iter_ids = sorted(self.groups)
        return self

    def __next__(self):
        if not self._iter_ids:
            raise StopIteration
        origin_id = self._iter_ids.pop(0)
        return self._build_record(self._get_group(origin_id))

    def unsegmented_records(self):
        """Return ordinary (non-segmented) records found during the scan."""
        self._ensure_scanned()
        records = []
        for info, record in self.unsegmented:
            info.spool.seek(0)
            records.append(ArcWarcRecord(record.format, record.rec_type,
                                         record.rec_headers, info.spool,
                                         record.http_headers,
                                         record.content_type, info.block_length))
        return records

    def close(self):
        """Release the spooled segment blocks held by this reassembler."""
        for group in self.groups.values():
            if group.head is not None:
                group.head.close()
            for info in group.segments.values():
                info.close()
        for info, _record in self.unsegmented:
            info.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    # -- scanning -----------------------------------------------------------

    def _scan_source(self, source):
        if hasattr(source, 'read'):
            source_name = getattr(source, 'name', '<stream>')
            self._scan_stream(source, source_name)
            return

        with open(source, 'rb') as fileobj:
            self._scan_stream(fileobj, os.fspath(source))

    def _scan_stream(self, fileobj, source_name):
        iterator = ArchiveIterator(fileobj, block_size=self.block_size)
        for record in iterator:
            segment_number = record.rec_headers.get_header(self.SEGMENT_HEADER)
            if segment_number is None:
                info = self._store_unsegmented(record, source_name)
            else:
                info = self._store_segment(record, source_name,
                                           segment_number)
            # location is captured after spooling the block, since reading
            # the member length drains the record stream
            info.offset, info.member_length = self._member_location(iterator)

    @staticmethod
    def _member_location(iterator):
        try:
            return iterator.get_record_offset(), iterator.get_record_length()
        except Exception:
            return None, None

    def _store_unsegmented(self, record, source_name):
        spool, block_length = self._spool_block(record)
        info = SegmentInfo(
            number=1,
            record_id=record.rec_headers.get_header('WARC-Record-ID'),
            origin_id=None,
            block_length=block_length,
            total_length=None,
            is_head=True,
            is_terminal=False,
            source=source_name,
            offset=None,
            member_length=None,
            spool=spool,
            content_type=record.content_type)
        self.unsegmented.append((info, record))
        return info

    def _store_segment(self, record, source_name, segment_number):
        try:
            number = int(segment_number)
        except (ValueError, TypeError):
            raise SegmentConflictError(
                'invalid WARC-Segment-Number {0!r} in {1}'.format(
                    segment_number, source_name),
                origin_id=record.rec_headers.get_header(self.ORIGIN_HEADER))

        is_head = record.rec_type != 'continuation'
        origin_id = (record.rec_headers.get_header('WARC-Record-ID')
                     if is_head
                     else record.rec_headers.get_header(self.ORIGIN_HEADER))

        if is_head and number != 1:
            raise SegmentConflictError(
                'first segment of {0} must declare WARC-Segment-Number 1, '
                'found {1} in {2}'.format(origin_id, number, source_name),
                segment_number=number, origin_id=origin_id)

        if not is_head and not origin_id:
            raise SegmentConflictError(
                'continuation record {0} in {1} is missing '
                'WARC-Segment-Origin-ID'.format(
                    record.rec_headers.get_header('WARC-Record-ID'),
                    source_name),
                segment_number=number)

        if not is_head and number < 2:
            raise SegmentConflictError(
                'continuation record {0} in {1} must declare a '
                'WARC-Segment-Number greater than 1, found {2}'.format(
                    record.rec_headers.get_header('WARC-Record-ID'),
                    source_name, number),
                segment_number=number, origin_id=origin_id)

        total_length = self._parse_total_length(record, source_name, origin_id)
        spool, block_length = self._spool_block(record)
        info = SegmentInfo(
            number=number,
            record_id=record.rec_headers.get_header('WARC-Record-ID'),
            origin_id=origin_id,
            block_length=block_length,
            total_length=total_length,
            is_head=is_head,
            is_terminal=total_length is not None,
            source=source_name,
            offset=None,
            member_length=None,
            spool=spool,
            content_type=record.content_type)

        group = self.groups.setdefault(origin_id, _SegmentGroup(origin_id))
        group.add(info, record if is_head else None)
        return info

    def _parse_total_length(self, record, source_name, origin_id):
        raw = record.rec_headers.get_header(self.TOTAL_LENGTH_HEADER)
        if raw is None:
            return None
        try:
            total_length = int(raw)
        except (ValueError, TypeError):
            raise SegmentConflictError(
                'invalid {0} {1!r} in {2}'.format(
                    self.TOTAL_LENGTH_HEADER, raw, source_name),
                origin_id=origin_id)
        if total_length < 0:
            raise SegmentConflictError(
                'negative {0} {1} in {2}'.format(
                    self.TOTAL_LENGTH_HEADER, total_length, source_name),
                origin_id=origin_id)
        return total_length

    @staticmethod
    def _spool_block(record):
        spool = tempfile.SpooledTemporaryFile(max_size=512 * 1024)
        block_length = 0
        while True:
            buf = record.raw_stream.read(BUFF_SIZE)
            if not buf:
                break
            spool.write(buf)
            block_length += len(buf)
        spool.seek(0)
        return spool, block_length

    # -- reassembly ---------------------------------------------------------

    def _get_group(self, origin_id):
        try:
            return self.groups[origin_id]
        except KeyError:
            raise SegmentReassemblyError(
                'no segments found for record {0}'.format(origin_id))

    def _build_record(self, group):
        if group.head is None:
            raise SegmentReassemblyError(
                'cannot reassemble {0}: first (identity) segment is missing, '
                'only continuation segments {1} were provided'.format(
                    group.origin_id,
                    sorted(group.segments)))

        segment_infos = [group.head]
        segment_infos.extend(info for _, info in
                             sorted(group.segments.items()))
        segment_infos.sort(key=lambda info: info.number)

        problems = []
        complete = True

        present_numbers = [info.number for info in segment_infos]
        highest = present_numbers[-1]
        missing = [number for number in range(1, highest + 1)
                   if number not in present_numbers]
        if missing:
            complete = False
            problems.append(
                'missing segment(s): {0}'.format(
                    ', '.join(str(number) for number in missing)))

        declared = self._consistent_total_length(group)
        actual_length = sum(info.block_length for info in segment_infos)

        terminal = next((info for info in reversed(segment_infos)
                         if info.is_terminal), None)
        if terminal is None:
            complete = False
            problems.append(
                'end condition not met: highest available segment {0} carries '
                'no {1} marker, so one or more trailing segments are '
                'possibly missing'.format(highest, self.TOTAL_LENGTH_HEADER))
        elif terminal.number != highest:
            complete = False
            problems.append(
                'end condition not met: {0} marker found on segment {1} '
                'instead of the final segment {2}'.format(
                    self.TOTAL_LENGTH_HEADER, terminal.number, highest))

        if declared is not None and not missing and terminal is not None:
            if actual_length != declared:
                complete = False
                problems.append(
                    'declared total length {0} does not match reassembled '
                    'block length {1}'.format(declared, actual_length))

        if complete:
            problems.append(
                'all {0} segment(s) present, {1} bytes reassembled, total '
                'length marker verified'.format(len(segment_infos),
                                                actual_length))

        raw_stream = SegmentedBlockStream(segment_infos)
        return ReassembledRecord(group.head_record, raw_stream,
                                 actual_length, segment_infos, complete,
                                 problems, declared)

    @staticmethod
    def _consistent_total_length(group):
        declared = [info.total_length for info in
                    [group.head] + list(group.segments.values())
                    if info.total_length is not None]
        if not declared:
            return None
        if len(set(declared)) > 1:
            msg = ('conflicting {0} declarations for {1}: {2}'.format(
                WARCSegmentReassembler.TOTAL_LENGTH_HEADER,
                group.origin_id, sorted(set(declared))))
            raise SegmentConflictError(msg, origin_id=group.origin_id)
        return declared[0]

    # -- helpers ------------------------------------------------------------

    def _ensure_scanned(self):
        if not self._scanned:
            self.scan()

    @staticmethod
    def _normalize_inputs(inputs):
        if isinstance(inputs, (str, bytes, os.PathLike)) or hasattr(inputs, 'read'):
            return [inputs]
        return list(inputs)
