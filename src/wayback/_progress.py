"""
Saveable progress for resumable, paged CDX searches.

The CDX API pages through results with an opaque, server-managed
``resumeKey``. A long search (e.g. a decade of captures) can be interrupted
mid-way, and restarting it from the first page would waste a lot of work.
:class:`SearchProgress` captures everything needed to continue a search in a
later process:

* the exact query parameters the search was bound to (so a resumed search can
  never silently use different parameters),
* the server-side continuation cursor (``resumeKey``) and the boundary record
  it is anchored on,
* a cursor-scoped *replay guard* containing every record the current cursor
  could re-return, which eliminates the boundary duplicates the CDX server
  emits (including when a search is paused in the middle of a page), and
* the records that were handed to a consumer but never confirmed as handled,
  so an unconfirmed record is never counted as finished.

A progress object is explicitly saved and loaded by user code (e.g. as JSON
to disk). It is deliberately *not* a fixed snapshot: the CDX index is live and
new captures can appear while a search is paused, so resuming continues the
current index from a cursor rather than reproducing an immutable result set.
"""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from ._models import CdxRecord
from ._utils import format_timestamp, parse_timestamp
from .exceptions import ResumeKeyError, UnexpectedResponseFormat

# Bumped when the serialized shape of SearchProgress changes in a way that is
# not backwards compatible.
PROGRESS_VERSION = 1

# CDX query parameters that describe the search itself, as opposed to the
# internal pagination cursor. A saved progress never persists the cursor as
# part of its bound query -- it is tracked separately in ``resume_key``.
_CDX_QUERY_KEYS = (
    'url',
    'matchType',
    'limit',
    'offset',
    'from',
    'to',
    'filter',
    'fastLatest',
    'collapse',
    'showResumeKey',
    'resolveRevisits',
)

# Maps CDX HTTP API parameter names back to the matching keyword arguments of
# WaybackClient.search().
_QUERY_TO_KWARG = {
    'url': 'url',
    'matchType': 'match_type',
    'offset': 'offset',
    'from': 'from_date',
    'to': 'to_date',
    'filter': 'filter_field',
    'fastLatest': 'fast_latest',
    'collapse': 'collapse',
    'resolveRevisits': 'resolve_revisits',
    'limit': 'limit',
}


def cdx_record_to_line(record):
    """
    Serialize a :class:`CdxRecord` back into the single-line CDX text format.

    This is the canonical, unambiguous identity used to track records inside
    :class:`SearchProgress` (pending/confirmed bookkeeping and the replay
    guard). Round-tripping a record through :func:`cdx_line_to_record` and this
    function is stable.
    """
    statuscode = '-' if record.statuscode is None else str(record.statuscode)
    length = '-' if record.length is None else str(record.length)
    return ' '.join(
        (
            record.urlkey,
            format_timestamp(record.timestamp),
            record.original,
            record.mimetype,
            statuscode,
            record.digest,
            length,
        )
    )


def cdx_line_to_record(text):
    """
    Parse a single canonical CDX line into a :class:`CdxRecord`.

    Raises
    ------
    UnexpectedResponseFormat
        If the line cannot be parsed into the expected CDX fields.
    """
    try:
        urlkey, raw_timestamp, original, mimetype, raw_status, digest, raw_length, *_ = text.split(' ')
        statuscode = None if raw_status == '-' else int(raw_status)
        length = None if raw_length == '-' else int(raw_length)
        timestamp = parse_timestamp(raw_timestamp)
    except Exception as err:
        raise UnexpectedResponseFormat(f'Could not parse CDX output: "{text}"') from err

    return CdxRecord(
        urlkey=urlkey,
        timestamp=timestamp,
        original=original,
        mimetype=mimetype,
        statuscode=statuscode,
        digest=digest,
        length=length,
    )


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _normalize_query(query):
    """
    Return a copy of a CDX query dict with tuple values converted to lists.

    JSON persistence turns lists and tuples into the same array form, so
    normalizing keeps a freshly built query and a loaded one comparable even
    when an argument like ``filter_field`` was originally a tuple.
    """
    normalized = {}
    for key, value in query.items():
        normalized[key] = list(value) if isinstance(value, tuple) else copy.deepcopy(value)
    return normalized


class SearchProgress:
    """
    Persistent progress for a paged :meth:`WaybackClient.search`.

    You normally do not construct one directly -- pass ``track_progress=True``
    to :meth:`WaybackClient.search` to get a fresh instance, or load a
    previously saved one with :meth:`load` / :meth:`from_dict`. Pass an
    existing instance back into a later ``search()`` call to resume.

    The object tracks several distinct positions, which is what makes resuming
    both safe and correct:

    * **The server cursor** (:attr:`resume_key`) is where the CDX server would
      continue paging. It only advances when a full page ending in a new resume
      key is received.
    * **The replay guard** (:attr:`replay_guard`) holds every record the
      current cursor could re-return. When a page is fetched and its next cursor
      is known, the guard is rotated to that page's boundary record; records
      fetched for the new cursor extend it. This removes the CDX server's
      boundary duplicates even when a search was paused in the middle of a page.
    * **The confirmed frontier** (:attr:`pending_records`) holds records that
      were yielded to a consumer but not yet confirmed via
      :meth:`mark_processed`. Only confirmed records count as finished, so on
      resume these are re-delivered from local progress before any new page is
      requested.

    Note
    ----
    Search results come from the live CDX index, not a fixed snapshot. If new
    captures matching the query are added while a search is paused, resuming
    reflects that changing index; do not treat a resumed run as an immutable,
    point-in-time snapshot.

    Attributes
    ----------
    query : dict
        The canonical CDX query parameters the search is bound to (read-only
        view). Does not include ``resumeKey``.
    resume_key : str or None
        The server-side continuation cursor for the next page, or ``None`` if
        the first page has not been fetched yet.
    resume_timestamp : str or None
        The Wayback timestamp of the boundary record the cursor is anchored on.
    seen_count : int
        Number of distinct records received from the server (replays excluded).
    processed_count : int
        Number of records the consumer has confirmed via
        :meth:`mark_processed`.
    is_complete : bool
        ``True`` only when every page has been fetched *and* every yielded
        record has been confirmed as processed.
    """

    def __init__(
        self,
        query,
        *,
        resume_key=None,
        resume_timestamp=None,
        replay_guard=None,
        pending_records=None,
        seen_count=0,
        processed_count=0,
        server_complete=False,
        started_at=None,
        updated_at=None,
        last_processed_timestamp=None,
    ):
        if not isinstance(query, dict) or 'url' not in query:
            raise ValueError('SearchProgress requires a CDX query dict that includes a "url" entry.')

        # Keep only recognized query parameters and never let the cursor leak
        # into the bound query. Normalize tuples to lists for JSON stability.
        filtered = {key: value for key, value in query.items() if key in _CDX_QUERY_KEYS}
        self._query = _normalize_query(filtered)
        if 'url' not in self._query:
            raise ValueError('SearchProgress requires a CDX query dict that includes a "url" entry.')

        self._resume_key = resume_key
        self._resume_timestamp = resume_timestamp
        # Cursor-scoped set of canonical lines that may be replayed. An ordered
        # list preserves page order while membership checks stay explicit.
        self._replay_guard = list(replay_guard or [])
        # Preserve insertion order while avoiding duplicate pending entries.
        self._pending = list(pending_records or [])
        self._seen_count = int(seen_count)
        self._processed_count = int(processed_count)
        self._server_complete = bool(server_complete)
        self._started_at = started_at or _now_iso()
        self._updated_at = updated_at or self._started_at
        self._last_processed_timestamp = last_processed_timestamp

    # -- Construction / persistence ---------------------------------------

    @classmethod
    def start(cls, query):
        """
        Create fresh progress bound to the given canonical CDX query dict.
        """
        return cls(copy.deepcopy(query))

    def to_dict(self):
        """
        Serialize the progress to a plain, JSON-serializable dict.
        """
        return {
            'version': PROGRESS_VERSION,
            'query': copy.deepcopy(self._query),
            'resume_key': self._resume_key,
            'resume_timestamp': self._resume_timestamp,
            'replay_guard': list(self._replay_guard),
            'pending_records': list(self._pending),
            'seen_count': self._seen_count,
            'processed_count': self._processed_count,
            'server_complete': self._server_complete,
            'started_at': self._started_at,
            'updated_at': self._updated_at,
            'last_processed_timestamp': self._last_processed_timestamp,
        }

    @classmethod
    def from_dict(cls, data):
        """
        Recreate progress from a dict produced by :meth:`to_dict`.

        Raises
        ------
        ValueError
            If the data uses an unsupported serialization version.
        """
        if not isinstance(data, dict):
            raise ValueError('SearchProgress data must be a dict.')
        version = data.get('version', PROGRESS_VERSION)
        if version != PROGRESS_VERSION:
            raise ValueError(
                f'Unsupported SearchProgress version {version!r}; '
                f'this package supports version {PROGRESS_VERSION}.'
            )

        return cls(
            data['query'],
            resume_key=data.get('resume_key'),
            resume_timestamp=data.get('resume_timestamp'),
            replay_guard=data.get('replay_guard'),
            pending_records=data.get('pending_records'),
            seen_count=data.get('seen_count', 0),
            processed_count=data.get('processed_count', 0),
            server_complete=data.get('server_complete', False),
            started_at=data.get('started_at'),
            updated_at=data.get('updated_at'),
            last_processed_timestamp=data.get('last_processed_timestamp'),
        )

    def save(self, path):
        """
        Save the progress as JSON to ``path`` (a string or :class:`pathlib.Path`).

        Parent directories are created if needed.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write('\n')

    @classmethod
    def load(cls, path):
        """
        Load progress previously written with :meth:`save`.
        """
        with open(path, 'r', encoding='utf-8') as handle:
            return cls.from_dict(json.load(handle))

    # -- Read-only views ---------------------------------------------------

    @property
    def query(self):
        return copy.deepcopy(self._query)

    @property
    def resume_key(self):
        return self._resume_key

    @property
    def resume_timestamp(self):
        return self._resume_timestamp

    @property
    def replay_guard(self):
        """Canonical lines the current cursor may re-return."""
        return tuple(self._replay_guard)

    @property
    def seen_count(self):
        return self._seen_count

    @property
    def processed_count(self):
        return self._processed_count

    @property
    def pending_count(self):
        return len(self._pending)

    @property
    def pending_records(self):
        """Records yielded but not yet confirmed, as canonical CDX lines."""
        return tuple(self._pending)

    @property
    def server_complete(self):
        """Whether the server has reported that no further pages exist."""
        return self._server_complete

    @property
    def is_complete(self):
        return self._server_complete and not self._pending

    @property
    def started_at(self):
        return self._started_at

    @property
    def updated_at(self):
        return self._updated_at

    # -- Request / recovery parameter construction ------------------------

    def to_query_dict(self):
        """
        Build the parameter dict for the next CDX request.

        The bound query is always used, with the current ``resumeKey`` added
        when one is available.
        """
        params = copy.deepcopy(self._query)
        if self._resume_key is not None:
            params['resumeKey'] = self._resume_key
        return params

    def matches_query(self, query):
        """
        Return whether ``query`` (a canonical CDX query dict) is equivalent to
        the one this progress is bound to. The ``resumeKey`` is ignored, and
        list/tuple values (e.g. filters) compare equal.
        """
        left = _normalize_query({key: value for key, value in query.items() if key in _CDX_QUERY_KEYS})
        return left == self._query

    def to_search_kwargs(self):
        """
        Reconstruct :meth:`WaybackClient.search` keyword arguments that
        reproduce the originally bound query. Useful for a full restart.
        """
        kwargs = {}
        for cdx_key, value in self._query.items():
            kwarg = _QUERY_TO_KWARG.get(cdx_key)
            if kwarg is None:
                continue
            if cdx_key in ('from', 'to'):
                kwargs[kwarg] = parse_timestamp(value)
            elif cdx_key in ('limit', 'offset'):
                kwargs[kwarg] = int(value)
            elif cdx_key in ('fastLatest', 'resolveRevisits'):
                kwargs[kwarg] = str(value).lower() == 'true'
            else:
                kwargs[kwarg] = value
        return kwargs

    def to_boundary_kwargs(self):
        """
        Build search kwargs that continue *without* the (possibly invalid)
        server cursor, advancing the lower date bound to a safe boundary.

        If records are still unconfirmed, the boundary is the earliest such
        record's timestamp, guaranteeing every unconfirmed record falls inside
        the new date range and will be encountered again. Otherwise it uses the
        last confirmed record (or the cursor boundary).

        This is one of the executable recovery options offered when a resume
        key stops being valid. It starts a fresh traversal, so pass it to a new
        ``search(..., track_progress=True)`` rather than reusing this progress.
        """
        kwargs = self.to_search_kwargs()
        # An offset was relative to the original, full result set and does not
        # make sense against the narrowed recovery window, so always page it
        # from the start.
        kwargs.pop('offset', None)
        if self._pending:
            boundary = min(line.split(' ')[1] for line in self._pending)
        else:
            boundary = self._last_processed_timestamp or self._resume_timestamp
        if boundary is not None:
            boundary_date = parse_timestamp(boundary)
            current_from = kwargs.get('from_date')
            if current_from is None or boundary_date > current_from:
                kwargs['from_date'] = boundary_date
        return kwargs

    # -- Internal state transitions (used by WaybackClient.search) --------

    def touch(self):
        self._updated_at = _now_iso()

    def advance_cursor(self, resume_key, boundary_line=None, resume_timestamp=None):
        """
        Move to a newly received server cursor.

        The replay guard is rotated so it only covers records the new cursor
        can re-return, starting with the boundary line that ended the current
        page (the record the server will repeat first on the next request).
        """
        self._resume_key = resume_key
        self._resume_timestamp = resume_timestamp
        self._replay_guard = [boundary_line] if boundary_line else []
        self.touch()

    def is_replay(self, raw_line):
        """
        Whether ``raw_line`` (exactly as received from the CDX server) has
        already been delivered under the current cursor and is a server-side
        boundary/overlap replay that must be suppressed rather than yielded or
        counted again.
        """
        return raw_line in self._replay_guard

    def add_replay_line(self, raw_line):
        """
        Remember a raw CDX line as already seen under the current cursor, so a
        later replay of it is recognized. Called for every distinct raw data
        line, including ones that parse to malformed records that are never
        yielded.
        """
        if raw_line not in self._replay_guard:
            self._replay_guard.append(raw_line)
            self.touch()

    def note_record(self, record):
        """
        Track a freshly fetched, newly delivered :class:`CdxRecord` on the
        unconfirmed frontier. The record stays pending until the consumer calls
        :meth:`mark_processed`, and each distinct record increments the seen
        count exactly once.

        Replay suppression itself is handled separately with raw wire lines via
        :meth:`add_replay_line` and :meth:`is_replay`, because the CDX wire
        format can differ slightly from a re-serialized record (e.g. an
        implicit default port that the client normalizes away).
        """
        line = cdx_record_to_line(record)
        if line not in self._pending:
            self._pending.append(line)
            self._seen_count += 1
            self.touch()

    def pending_record_objects(self):
        """
        Reconstruct unconfirmed records as :class:`CdxRecord` objects in the
        order they were originally yielded.
        """
        return [cdx_line_to_record(line) for line in self._pending]

    def mark_processed(self, record):
        """
        Confirm that a consumer has finished handling ``record``.

        ``record`` may be a :class:`CdxRecord` or a canonical CDX line. A
        record that has not been confirmed is never counted as complete, and
        confirmation is idempotent.

        Returns
        -------
        bool
            ``True`` if this confirmed a previously pending record, otherwise
            ``False`` (e.g. a duplicate confirmation).
        """
        line = cdx_record_to_line(record) if isinstance(record, CdxRecord) else record
        if line in self._pending:
            self._pending.remove(line)
            self._processed_count += 1
            self._last_processed_timestamp = line.split(' ')[1]
            self.touch()
            return True
        return False

    def mark_server_complete(self):
        """Mark that the server has no further pages for this query."""
        self._server_complete = True
        self.touch()

    def raise_resume_key_error(self, response=None, reason=None):
        """
        Build a :class:`ResumeKeyError` with concrete, executable recovery
        options for an invalidated continuation cursor.
        """
        message = (
            'The Wayback Machine did not accept the saved resumeKey, so the '
            'search cannot continue exactly from its old boundary. Resume keys '
            'are server-managed cursors that can expire or be invalidated when '
            'the index changes.\n'
            'You can recover in either of these ways (each starts a fresh '
            'search, since the old cursor is unusable):\n'
            '  1. Re-run the full original query from the beginning:\n'
            f'     client.search(track_progress=True, **{self.to_search_kwargs()!r})\n'
            '  2. Resume from the earliest unconfirmed (or last confirmed) '
            'boundary, without the old cursor:\n'
            f'     client.search(track_progress=True, **{self.to_boundary_kwargs()!r})'
        )
        if reason:
            message = f'{reason}\n{message}'
        return ResumeKeyError(message, progress=self, response=response)
