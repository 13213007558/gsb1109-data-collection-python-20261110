"""
Selection strategy for picking the most appropriate memento for a given
target time.

Asking the Wayback Machine for "the closest snapshot" frequently returns a
redirect, a login/interstitial page, or an error capture instead of usable
content. The functions here make the choice explicit and deterministic:
candidates from the CDX index are filtered by their temporal relation to the
requested time, their allowed time direction, capture status code, media
type, and an optional payload requirement.

The selection logic in this module is pure (it only inspects
:class:`wayback.CdxRecord` metadata), so it can be exercised fully offline.
The client layer in :class:`wayback.WaybackClient` additionally verifies the
timestamp of the memento that is actually played back.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, List, Optional, Sequence, Tuple

from ._models import CdxRecord
from ._utils import ensure_utc_datetime
from .exceptions import NoCompatibleMementoError


class Direction(str, Enum):
    """
    Which temporal direction a selected memento may lie in relative to the
    requested target time.

    .. py:attribute:: nearest

        Pick the closest capture whether it is before or after the target.

    .. py:attribute:: before

        Only consider captures at or before the target time. A future
        capture is never substituted when nothing matches.

    .. py:attribute:: after

        Only consider captures at or after the target time.
    """

    nearest = 'nearest'
    before = 'before'
    after = 'after'


class TimeRelation(str, Enum):
    """
    How a specific capture time relates to the requested target time.

    .. py:attribute:: at

        The capture time is exactly the target time.

    .. py:attribute:: before

        The capture is earlier than the target time.

    .. py:attribute:: after

        The capture is later than the target time.
    """

    at = 'at'
    before = 'before'
    after = 'after'


class RejectionReason(str, Enum):
    """
    The category of restriction that caused a candidate to be excluded.

    These values are part of the public API (they are surfaced on
    :class:`wayback.exceptions.NoCompatibleMementoError`), so they are kept
    stable strings rather than generated from free-form messages.

    .. py:attribute:: time_direction

        The capture lies on the wrong side of the target time for the
        requested :class:`Direction` (e.g. a future capture when
        ``direction='before'``).

    .. py:attribute:: distance

        The capture is farther from the target time than the maximum
        allowed distance.

    .. py:attribute:: status_code

        The capture's status code is not in the allowed set (e.g. it is a
        redirect or login response).

    .. py:attribute:: media_type

        The capture's media (MIME) type does not match any allowed type.

    .. py:attribute:: payload

        An optional payload requirement was not satisfied. At the CDX stage
        this comes from the :attr:`MementoCriteria.payload` predicate; at
        playback time it comes from the client's content check.

    .. py:attribute:: playback

        The memento could not be played back (the Wayback Machine raised a
        playback error for it).

    .. py:attribute:: timestamp_mismatch

        The memento actually served by the Wayback Machine has a timestamp
        that violates the requested direction or maximum distance, even
        though the CDX record looked acceptable.
    """

    time_direction = 'time_direction'
    distance = 'distance'
    status_code = 'status_code'
    media_type = 'media_type'
    payload = 'payload'
    playback = 'playback'
    timestamp_mismatch = 'timestamp_mismatch'


#: A predicate over a CDX record, used as a metadata-level payload requirement.
PayloadPredicate = Callable[[CdxRecord], bool]


@dataclass(frozen=True)
class MementoCriteria:
    """
    The restrictions used to decide whether a CDX capture is an acceptable
    memento for a target time.

    Attributes
    ----------
    direction : Direction
        Which side(s) of the target time are allowed. Defaults to
        :attr:`Direction.nearest`.
    allowed_status : frozenset of int, optional
        Acceptable capture status codes, e.g. ``frozenset({200})``. A
        capture whose status code is not in this set is rejected. When
        ``None`` (the default), status codes are not restricted.
    media_types : tuple of str, optional
        Acceptable media types. Matching supports exact values
        (``'text/html'``), type wildcards (``'text/*'``), and ``'*'`` /
        ``'*/*'`` to match anything. Comparison is case-insensitive and
        ignores parameters (so ``'text/html; charset=utf-8'`` matches
        ``'text/html'``). When ``None`` (the default), media type is not
        restricted.
    payload : callable, optional
        A predicate taking a :class:`wayback.CdxRecord` and returning a
        boolean. Records for which it returns false are rejected with
        :attr:`RejectionReason.payload`. This runs against CDX metadata, so
        it can check fields such as :attr:`CdxRecord.length` or
        :attr:`CdxRecord.digest` without fetching content.
    """

    direction: Direction = Direction.nearest
    allowed_status: Optional[frozenset] = None
    media_types: Optional[Tuple[str, ...]] = None
    payload: Optional[PayloadPredicate] = None


@dataclass(frozen=True)
class SnapshotSelection:
    """
    One acceptable candidate together with the information used to rank it.

    Attributes
    ----------
    record : CdxRecord
        The CDX record for the candidate capture.
    target : datetime.datetime
        The requested target time (UTC).
    distance : datetime.timedelta
        The absolute time between the capture and the target.
    relation : TimeRelation
        Whether the capture is at, before, or after the target time.
    """

    record: CdxRecord
    target: datetime
    distance: timedelta
    relation: TimeRelation


def normalize_media_type(media_type: Optional[str]) -> str:
    """Lower-case a media type and strip its parameters."""
    if not media_type:
        return ''
    return media_type.split(';', 1)[0].strip().lower()


def media_type_matches(mimetype: Optional[str], allowed: Optional[Sequence[str]]) -> bool:
    """
    Determine whether a capture's ``mimetype`` satisfies an allowed list.

    ``allowed`` entries may be exact types (``'text/html'``), type
    wildcards (``'text/*'``), or ``'*'`` / ``'*/*'``. When ``allowed`` is
    ``None``, everything matches.
    """
    if allowed is None:
        return True

    actual = normalize_media_type(mimetype)
    for wanted_raw in allowed:
        wanted = normalize_media_type(wanted_raw)
        if wanted in ('*', '*/*'):
            return True
        if wanted.endswith('/*'):
            if actual.split('/', 1)[0] == wanted[:-2]:
                return True
        elif wanted == actual:
            return True
    return False


def build_criteria(
    direction=Direction.nearest,
    allowed_status=None,
    media_types=None,
    payload=None,
) -> MementoCriteria:
    """
    Normalize loosely-typed selection arguments into a :class:`MementoCriteria`.

    ``direction`` accepts a :class:`Direction` or its string value. Status
    codes become a frozenset of ints and media types a tuple of strings.
    """
    if isinstance(direction, str) and not isinstance(direction, Direction):
        direction = Direction(direction)
    if not isinstance(direction, Direction):
        raise TypeError(f'`direction` must be a Direction or string, not {type(direction).__name__}')

    normalized_status = None
    if allowed_status is not None:
        if isinstance(allowed_status, int):
            allowed_status = (allowed_status,)
        normalized_status = frozenset(int(status) for status in allowed_status)

    normalized_media = None
    if media_types is not None:
        if isinstance(media_types, str):
            media_types = (media_types,)
        normalized_media = tuple(media_types)

    if payload is not None and not callable(payload):
        raise TypeError('`payload` must be callable')

    return MementoCriteria(
        direction=direction,
        allowed_status=normalized_status,
        media_types=normalized_media,
        payload=payload,
    )


def time_relation(capture_time: datetime, target: datetime) -> TimeRelation:
    """Return whether ``capture_time`` is at, before, or after ``target``."""
    if capture_time < target:
        return TimeRelation.before
    if capture_time > target:
        return TimeRelation.after
    return TimeRelation.at


def evaluate_candidate(
    record: CdxRecord,
    target: datetime,
    criteria: MementoCriteria,
    max_distance: Optional[timedelta] = None,
) -> Optional[RejectionReason]:
    """
    Evaluate one candidate against the selection criteria.

    Returns the :class:`RejectionReason` that excludes it, or ``None`` if it
    is acceptable. Checks are ordered from cheapest/most structural (time) to
    most specific (payload).
    """
    relation = time_relation(record.timestamp, target)
    distance = abs(record.timestamp - target)

    if criteria.direction == Direction.before and relation == TimeRelation.after:
        return RejectionReason.time_direction
    if criteria.direction == Direction.after and relation == TimeRelation.before:
        return RejectionReason.time_direction

    if max_distance is not None and distance > max_distance:
        return RejectionReason.distance

    if criteria.allowed_status is not None and record.statuscode not in criteria.allowed_status:
        return RejectionReason.status_code

    if not media_type_matches(record.mimetype, criteria.media_types):
        return RejectionReason.media_type

    if criteria.payload is not None and not criteria.payload(record):
        return RejectionReason.payload

    return None


# Deterministic ordering for candidates that are equally distant: an exact
# match wins, otherwise the *earlier* capture is preferred (we never lean into
# the future to break a tie), then timestamp/URL/digest guarantee total order.
_RELATION_RANK = {
    TimeRelation.at: 0,
    TimeRelation.before: 1,
    TimeRelation.after: 2,
}


def rank_mementos(
    records,
    target,
    criteria: Optional[MementoCriteria] = None,
    max_distance: Optional[timedelta] = None,
) -> Tuple[List[SnapshotSelection], List[Tuple[CdxRecord, RejectionReason]]]:
    """
    Split CDX records into ranked, acceptable selections and rejections.

    Acceptable selections are ordered by (1) absolute distance from the
    target, (2) relation, preferring an exact match and then the earlier
    capture for equal-distance ties, (3) capture timestamp, URL, and digest
    so the ordering is total and stable regardless of input ordering.

    Returns
    -------
    eligible : list of SnapshotSelection
        Acceptable candidates in best-first order.
    rejected : list of (CdxRecord, RejectionReason)
        Excluded candidates with the category of restriction that excluded
        them.
    """
    target = ensure_utc_datetime(target)
    criteria = criteria if criteria is not None else MementoCriteria()

    eligible: List[SnapshotSelection] = []
    rejected: List[Tuple[CdxRecord, RejectionReason]] = []

    for record in records:
        reason = evaluate_candidate(record, target, criteria, max_distance)
        if reason is not None:
            rejected.append((record, reason))
            continue

        relation = time_relation(record.timestamp, target)
        eligible.append(
            SnapshotSelection(
                record=record,
                target=target,
                distance=abs(record.timestamp - target),
                relation=relation,
            )
        )

    eligible.sort(
        key=lambda selection: (
            selection.distance,
            _RELATION_RANK[selection.relation],
            selection.record.timestamp,
            selection.record.original,
            selection.record.digest,
        )
    )
    return eligible, rejected


def label_record(record: CdxRecord) -> str:
    """Build a compact, human-readable label for a rejected record."""
    return f'{record.original} @ {record.timestamp.strftime("%Y%m%d%H%M%S")}'


def format_no_compatible_message(url, target, rejections) -> str:
    """
    Build the explanatory message for a NoCompatibleMementoError.

    ``rejections`` is a sequence of ``(label, reason_value)`` pairs. The
    message states how many candidates each category of restriction removed
    and makes explicit that restrictions were not relaxed into the future.
    """
    location = f' for {url}' if url else ''
    if not rejections:
        return (
            f'No compatible memento was found{location} near {target.isoformat()}: '
            f'the CDX index contained no candidates to consider.'
        )

    counts: dict[str, int] = {}
    for _label, reason in rejections:
        counts[reason] = counts.get(reason, 0) + 1
    summary = ', '.join(f'{reason}={count}' for reason, count in sorted(counts.items()))

    return (
        f'No compatible memento was found{location} near {target.isoformat()}: '
        f'{len(rejections)} candidate(s) were excluded by the selection criteria '
        f'({summary}). Restrictions were not relaxed, so no capture from a '
        f'disallowed time direction was substituted.'
    )


def select_memento(
    records,
    target,
    criteria: Optional[MementoCriteria] = None,
    max_distance: Optional[timedelta] = None,
    url: Optional[str] = None,
) -> SnapshotSelection:
    """
    Select the best acceptable memento for ``target`` from CDX ``records``.

    Parameters
    ----------
    records : iterable of CdxRecord
        Candidate captures, typically from
        :meth:`wayback.WaybackClient.search`.
    target : datetime.datetime or datetime.date or str
        The desired point in time.
    criteria : MementoCriteria, optional
        Selection restrictions. Defaults to a nearest-in-time match with no
        status, media-type, or payload filtering.
    max_distance : datetime.timedelta, optional
        Maximum acceptable distance from the target. Candidates farther away
        are rejected with :attr:`RejectionReason.distance`.
    url : str, optional
        The URL being queried, used only to make error messages clearer.

    Returns
    -------
    SnapshotSelection
        The best acceptable candidate.

    Raises
    ------
    wayback.exceptions.NoCompatibleMementoError
        If no candidate satisfies the criteria. The exception explains which
        category of restriction excluded every candidate.
    """
    target = ensure_utc_datetime(target)
    eligible, rejected = rank_mementos(records, target, criteria, max_distance)
    if not eligible:
        rejection_pairs = [(label_record(record), reason.value) for record, reason in rejected]
        raise NoCompatibleMementoError(
            format_no_compatible_message(url, target, rejection_pairs),
            target=target,
            rejections=rejection_pairs,
            url=url,
        )
    return eligible[0]
