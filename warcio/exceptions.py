#=================================================================
class ArchiveLoadFailed(Exception):
    def __init__(self, reason):
        self.msg = str(reason)
        super(ArchiveLoadFailed, self).__init__(self.msg)


#=================================================================
class SegmentReassemblyError(ArchiveLoadFailed):
    """Base class for errors encountered while reassembling segmented
    WARC records (WARC-Segment-Number / ``continuation`` records)."""


#=================================================================
class SegmentConflictError(SegmentReassemblyError):
    """Raised when two copies of the same segment number disagree,
    or when segment metadata is internally inconsistent.

    Conflicting segments are never silently accepted: the caller must
    decide which copy is authoritative.
    """
    def __init__(self, reason, segment_number=None, origin_id=None):
        self.segment_number = segment_number
        self.origin_id = origin_id
        super(SegmentConflictError, self).__init__(reason)


#=================================================================
class MissingSegmentError(SegmentReassemblyError):
    """Raised when streaming a reassembled record reaches a gap in the
    segment numbering. The bytes on either side of the gap are never
    concatenated.

    ``missing_numbers`` lists the segment numbers that could not be
    located, in ascending order.
    """
    def __init__(self, reason, missing_numbers=None):
        self.missing_numbers = list(missing_numbers) if missing_numbers else []
        super(MissingSegmentError, self).__init__(reason)
