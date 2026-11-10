"""
Time-aware sampling of CDX index records.

When a URL has thousands of captures but a researcher only wants to download a
small number of snapshots (the "budget"), it is important not to spend the
whole budget on captures from a single day. The functions in this module pick a
representative subset of snapshots using only the metadata already present in
CDX index records -- timestamps and content digests -- so no memento bodies
need to be downloaded before the selection is made.

Selection priorities, in order:

1. The first and last capture in the requested time range (the two sides of
   the covered timeline, including anything adjacent to a long empty window).
2. The first capture of every content run, i.e. every point where the digest
   changes. A change point after a long capture gap is preferred over one in a
   densely captured period.
3. When duplicate digests are kept (the default), one time representative --
   the last capture -- of each repeated-digest run.

If the budget is too small to cover every candidate, the omitted time spans
are reported back as :class:`CoverageGap` instances instead of being silently
dropped.
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import List, NamedTuple, Optional, Sequence, Tuple

from ._models import CdxRecord
from ._utils import ensure_utc_datetime


class DuplicatePolicy(Enum):
    """
    How to treat multiple captures whose ``digest`` is identical (i.e. captures
    of unchanged content).

    **Values**

    .. py:attribute:: representative

        Keep a time representative (the last capture) of every run of repeated
        digests, in addition to the first capture where the content appeared.
        This shows both when the content appeared and how long it persisted
        unchanged.

    .. py:attribute:: drop

        Deduplicate repeated digests. Only the first capture of each distinct
        digest run is eligible for selection, leaving more of the budget
        available for content changes elsewhere.
    """

    representative = 'representative'
    drop = 'drop'


class SelectionReason(NamedTuple):
    """
    Explains why one snapshot was selected.

    .. py:attribute:: record
       :type: CdxRecord

       The selected CDX index record.

    .. py:attribute:: role
       :type: str

       One of ``'boundary'`` (first or last capture in the time range),
       ``'change'`` (first capture of a new content digest), or
       ``'representative'`` (time representative of a repeated-digest run).

    .. py:attribute:: rationale
       :type: str

       A human-readable explanation of the selection.

    .. py:attribute:: gap_before
       :type: Optional[datetime.timedelta]

       For content-change selections, the time since the previous capture.
       :data:`None` for other roles.
    """

    record: CdxRecord
    role: str
    rationale: str
    gap_before: Optional[timedelta]


class CoverageGap(NamedTuple):
    """
    A time span that could not be covered within the budget.

    .. py:attribute:: start
       :type: datetime.datetime

       Start of the omitted time span (UTC).

    .. py:attribute:: end
       :type: datetime.datetime

       End of the omitted time span (UTC).

    .. py:attribute:: reason
       :type: str

       Why the span is omitted, e.g. ``'budget_exhausted'`` or
       ``'no_captures_in_range'``.

    .. py:attribute:: skipped_captures
       :type: int

       Number of index records that fall inside the omitted span.
    """

    start: datetime
    end: datetime
    reason: str
    skipped_captures: int


class SampleSelection(NamedTuple):
    """
    The result of a sampling run.

    .. py:attribute:: records
       :type: tuple[CdxRecord]

       The selected records, in chronological order. Never longer than the
       requested budget.

    .. py:attribute:: reasons
       :type: tuple[SelectionReason]

       Coverage rationale for each record in :attr:`records`, in the same
       order.

    .. py:attribute:: gaps
       :type: tuple[CoverageGap]

       Time spans the budget could not cover. Empty when every candidate fit
       within the budget (apart from any explicitly requested range that had no
       captures at all).

    .. py:attribute:: budget
       :type: int

       The actual number of snapshots selected.

    .. py:attribute:: requested_budget
       :type: int

       The budget that was requested.

    .. py:attribute:: duplicate_policy
       :type: DuplicatePolicy

       The policy that was applied to repeated digests.

    .. py:attribute:: from_date
       :type: Optional[datetime.datetime]

       The inclusive start of the sampled time range, if one was given.

    .. py:attribute:: to_date
       :type: Optional[datetime.datetime]

       The inclusive end of the sampled time range, if one was given.

    .. py:attribute:: total_captures
       :type: int

       Number of index records considered within the time range.

    .. py:attribute:: distinct_digests
       :type: int

       Number of distinct content digests among the considered records.
    """

    records: Tuple[CdxRecord, ...]
    reasons: Tuple[SelectionReason, ...]
    gaps: Tuple[CoverageGap, ...]
    budget: int
    requested_budget: int
    duplicate_policy: DuplicatePolicy
    from_date: Optional[datetime]
    to_date: Optional[datetime]
    total_captures: int
    distinct_digests: int

    def report(self) -> str:
        """
        Render a plain-text selection list with the coverage rationale for
        every chosen snapshot, followed by the time spans that the budget could
        not cover.
        """
        lines = [
            f'Selected {self.budget} of {self.total_captures} captures '
            f'(budget: {self.requested_budget}, duplicates: {self.duplicate_policy.value}).'
        ]
        if self.from_date or self.to_date:
            lines.append(f'Time range: {self.from_date or "earliest"} to {self.to_date or "latest"}.')
        lines.append('')
        for reason in self.reasons:
            timestamp = reason.record.timestamp.isoformat()
            if reason.gap_before is not None:
                lines.append(
                    f'- {timestamp}  [{reason.role}] {reason.rationale} '
                    f'(gap before: {_format_duration(reason.gap_before)})'
                )
            else:
                lines.append(f'- {timestamp}  [{reason.role}] {reason.rationale}')
        if self.gaps:
            lines.append('')
            lines.append('Uncovered time spans:')
            for gap in self.gaps:
                lines.append(
                    f'- {gap.start.isoformat()} to {gap.end.isoformat()} '
                    f'({gap.reason}, {gap.skipped_captures} capture(s) skipped)'
                )
        else:
            lines.append('')
            lines.append('All eligible candidates fit within the budget; no time spans were omitted.')
        return '\n'.join(lines)


# A candidate eligible for selection. `priority` lower values win when the
# budget has to be trimmed: 0 = timeline boundary, 1 = content change point,
# 2 = repeated-digest representative.
class _Candidate(NamedTuple):
    record_index: int
    role: str
    rationale: str
    gap_before: Optional[timedelta]
    priority: int
    rank: float


def _normalize_policy(duplicate_policy) -> DuplicatePolicy:
    if isinstance(duplicate_policy, DuplicatePolicy):
        return duplicate_policy
    if isinstance(duplicate_policy, str):
        try:
            return DuplicatePolicy(duplicate_policy)
        except ValueError:
            pass
    raise ValueError(
        f'`duplicate_policy` must be one of {[policy.value for policy in DuplicatePolicy]}, '
        f'not {duplicate_policy!r}'
    )


def _content_runs(records: Sequence[CdxRecord]) -> List[Tuple[int, int]]:
    """Group consecutive records sharing a digest into (start, end) index runs."""
    runs: List[Tuple[int, int]] = []
    run_start = 0
    for index in range(1, len(records) + 1):
        if index == len(records) or records[index].digest != records[run_start].digest:
            runs.append((run_start, index - 1))
            run_start = index
    return runs


def _build_candidates(
    records: Sequence[CdxRecord], runs: Sequence[Tuple[int, int]], policy: DuplicatePolicy
) -> List[_Candidate]:
    candidates: List[_Candidate] = []
    last_index = len(records) - 1

    # Priority 1: the two sides of the covered timeline.
    candidates.append(_Candidate(0, 'boundary', 'first capture in the requested time range', None, 0, 0.0))
    if last_index != 0:
        candidates.append(
            _Candidate(last_index, 'boundary', 'last capture in the requested time range', None, 0, 0.0)
        )

    # Priority 2: every point where the content digest changes. The gap since
    # the previous capture determines which change points survive a tight
    # budget: the longer the empty window, the higher the rank.
    for run_index, (start, end) in enumerate(runs):
        if run_index == 0:
            continue
        gap_before = records[start].timestamp - records[start - 1].timestamp
        candidates.append(
            _Candidate(
                start,
                'change',
                f'first capture of digest {records[start].digest}; content changed here',
                gap_before,
                1,
                gap_before.total_seconds(),
            )
        )

    # Priority 3: time representatives of unchanged-content runs.
    if policy is DuplicatePolicy.representative:
        for start, end in runs:
            if end != start and end != last_index:
                candidates.append(
                    _Candidate(
                        end,
                        'representative',
                        (
                            f'last of {end - start + 1} repeated captures with digest '
                            f'{records[end].digest}; marks how long this content persisted'
                        ),
                        None,
                        2,
                        float(end - start),
                    )
                )

    return candidates


def _run_for_index(runs: Sequence[Tuple[int, int]], index: int) -> Tuple[int, int]:
    for start, end in runs:
        if start <= index <= end:
            return start, end
    raise ValueError(f'index {index} is not part of any content run')


def _span_for_candidate(
    records: Sequence[CdxRecord], runs: Sequence[Tuple[int, int]], candidate: _Candidate
) -> Tuple[datetime, datetime]:
    """Turn a dropped candidate into the raw time span it would have covered."""
    run_start, run_end = _run_for_index(runs, candidate.record_index)
    span_end = records[run_end].timestamp

    if candidate.role == 'change':
        # Include the empty window before the change point itself.
        span_start = records[run_start - 1].timestamp
    else:
        span_start = records[run_start].timestamp

    return span_start, span_end


def _merge_spans(raw_spans: Sequence[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    if not raw_spans:
        return []
    ordered = sorted(raw_spans, key=lambda item: (item[0], item[1]))
    merged: List[Tuple[datetime, datetime]] = []
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def _build_gaps(
    records: Sequence[CdxRecord],
    runs: Sequence[Tuple[int, int]],
    dropped_candidates: Sequence[_Candidate],
    kept_indices: frozenset,
) -> Tuple[CoverageGap, ...]:
    raw_spans = [_span_for_candidate(records, runs, candidate) for candidate in dropped_candidates]
    gaps = []
    # Spans are merged (and therefore disjoint) before counting so a record in
    # overlapping spans is reported exactly once.
    for start, end in _merge_spans(raw_spans):
        skipped = sum(
            1
            for index, record in enumerate(records)
            if index not in kept_indices and start <= record.timestamp <= end
        )
        gaps.append(CoverageGap(start, end, 'budget_exhausted', skipped))
    return tuple(gaps)


def _format_duration(duration: timedelta) -> str:
    seconds = int(duration.total_seconds())
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f'{days}d')
    if hours:
        parts.append(f'{hours}h')
    if minutes:
        parts.append(f'{minutes}m')
    if seconds or not parts:
        parts.append(f'{seconds}s')
    return ' '.join(parts)


def sample_snapshots(
    records: Sequence[CdxRecord],
    budget: int,
    *,
    duplicate_policy=DuplicatePolicy.representative,
    from_date=None,
    to_date=None,
) -> SampleSelection:
    """
    Select a budgeted, time-aware sample of snapshots from CDX index records.

    Only index metadata (timestamps and digests) is used; no memento bodies are
    fetched.

    Parameters
    ----------
    records : sequence of CdxRecord
        Index records, e.g. from :meth:`WaybackClient.search`. They do not need
        to be pre-sorted.
    budget : int
        Maximum number of snapshots to select. Must be at least 1.
    duplicate_policy : DuplicatePolicy or str, default: DuplicatePolicy.representative
        ``'representative'`` keeps a time representative of every run of
        repeated digests; ``'drop'`` deduplicates them.
    from_date : datetime or date or str, optional
        Inclusive start of the time range to consider. Naive values are treated
        as UTC.
    to_date : datetime or date or str, optional
        Inclusive end of the time range to consider. Naive values are treated
        as UTC.

    Returns
    -------
    SampleSelection
        The selected records, per-record rationale, and any time spans the
        budget could not cover.

    Raises
    ------
    TypeError
        If ``budget`` is not an integer.
    ValueError
        If ``budget`` is less than 1 or ``duplicate_policy`` is unknown.
    """
    if not isinstance(budget, int) or isinstance(budget, bool):
        raise TypeError(f'`budget` must be an integer, not {type(budget).__name__}')
    if budget < 1:
        raise ValueError(f'`budget` must be at least 1, not {budget}')
    policy = _normalize_policy(duplicate_policy)

    if from_date is not None:
        from_date = ensure_utc_datetime(from_date)
    if to_date is not None:
        to_date = ensure_utc_datetime(to_date)
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ValueError('`from_date` must not be later than `to_date`')

    in_range = tuple(
        sorted(
            (
                record
                for record in records
                if (from_date is None or record.timestamp >= from_date)
                and (to_date is None or record.timestamp <= to_date)
            ),
            key=lambda record: record.timestamp,
        )
    )

    if not in_range:
        empty_gap: Tuple[CoverageGap, ...] = ()
        if from_date is not None and to_date is not None:
            empty_gap = (CoverageGap(from_date, to_date, 'no_captures_in_range', 0),)
        return SampleSelection(
            records=(),
            reasons=(),
            gaps=empty_gap,
            budget=0,
            requested_budget=budget,
            duplicate_policy=policy,
            from_date=from_date,
            to_date=to_date,
            total_captures=0,
            distinct_digests=0,
        )

    runs = _content_runs(in_range)
    candidates = _build_candidates(in_range, runs, policy)

    # Boundaries first, then change points after the longest empty windows,
    # then representatives of the longest repeated-digest runs. Ties are broken
    # chronologically so results are deterministic. A boundary and a change
    # point can land on the same record, so dedupe by index (the higher-
    # priority role wins) before the budget is sliced.
    ranked = sorted(
        candidates, key=lambda candidate: (candidate.priority, -candidate.rank, candidate.record_index)
    )
    unique_ranked: List[_Candidate] = []
    seen_indices = set()
    for candidate in ranked:
        if candidate.record_index in seen_indices:
            continue
        seen_indices.add(candidate.record_index)
        unique_ranked.append(candidate)
    ranked = unique_ranked
    kept_candidates = ranked[:budget]
    dropped_candidates = ranked[budget:]

    kept_by_index = {candidate.record_index: candidate for candidate in kept_candidates}
    kept_indices = frozenset(kept_by_index)
    selected_indices = sorted(kept_by_index)
    selected_records = tuple(in_range[index] for index in selected_indices)
    reasons = tuple(
        SelectionReason(
            record=in_range[index],
            role=kept_by_index[index].role,
            rationale=kept_by_index[index].rationale,
            gap_before=kept_by_index[index].gap_before,
        )
        for index in selected_indices
    )

    gaps = list(_build_gaps(in_range, runs, dropped_candidates, kept_indices))

    # Report requested range portions that contain no captures at all.
    if from_date is not None and from_date < in_range[0].timestamp:
        gaps.insert(0, CoverageGap(from_date, in_range[0].timestamp, 'no_captures_in_range', 0))
    if to_date is not None and to_date > in_range[-1].timestamp:
        gaps.append(CoverageGap(in_range[-1].timestamp, to_date, 'no_captures_in_range', 0))

    return SampleSelection(
        records=selected_records,
        reasons=reasons,
        gaps=tuple(gaps),
        budget=len(selected_records),
        requested_budget=budget,
        duplicate_policy=policy,
        from_date=from_date,
        to_date=to_date,
        total_captures=len(in_range),
        distinct_digests=len({record.digest for record in in_range}),
    )
