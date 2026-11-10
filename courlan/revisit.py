"""
Configurable revisit policy for crawl outcomes.

A 404, a temporary timeout and repeated 429 responses must not collapse into
one generic "unvisited" state. This module turns an observation — an HTTP
status code, an exception category, the observation time and optional
server-side waiting hints — into a decision: either the earliest time the
address may be fetched again, or a reason for which crawling it is paused.

Time is always supplied by the caller, so the policy is deterministic and
fully testable without real ``sleep`` calls. Schedules are keyed per URL and
are independent across sites; an outcome that is not a confirmed success is
never recorded as one.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from threading import RLock
from typing import Any

from .clean import normalize_url

__all__ = [
    "OutcomeCategory",
    "FetchOutcome",
    "RevisitDecision",
    "RevisitPolicy",
    "RevisitSchedule",
    "RetryAfterError",
    "classify_exception",
    "format_timeline",
]


class OutcomeCategory(Enum):
    "Category of a fetch observation, used directly for exceptions."

    SUCCESS = "success"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TEMPORARY = "temporary"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


# status codes carrying an explicit Retry-After waiting hint
_RETRY_AFTER_STATUS = frozenset({429, 503})
# status codes that are not failures and reset the failure history
_SUCCESS_STATUS = frozenset(range(200, 300)) | {304}
# permanent client errors that are not worth retrying
_PERMANENT_STATUS = frozenset({400, 401, 403, 405, 410, 414, 451})
# transient server/client errors eligible for backoff
_TEMPORARY_STATUS = frozenset(
    {408, 409, 412, 413, 416, 421, 423, 425, 500, 502, 503, 504, 507}
)
# a conservative ceiling for a Retry-After value expressed in seconds
_MAX_RETRY_AFTER_SECONDS = 365 * 24 * 3600
# categories for which a delay can be configured
_RETRYABLE_CATEGORIES = (
    OutcomeCategory.NOT_FOUND,
    OutcomeCategory.RATE_LIMITED,
    OutcomeCategory.TEMPORARY,
    OutcomeCategory.UNKNOWN,
)
_DEFAULT_DELAYS = {
    OutcomeCategory.NOT_FOUND: timedelta(days=1),
    OutcomeCategory.RATE_LIMITED: timedelta(minutes=1),
    OutcomeCategory.TEMPORARY: timedelta(seconds=30),
    OutcomeCategory.UNKNOWN: timedelta(minutes=5),
}


class RetryAfterError(ValueError):
    "Raised when a Retry-After hint cannot be parsed."


def classify_exception(error: BaseException) -> OutcomeCategory:
    """Map a fetch exception to an outcome category.

    Network-level stdlib errors (timeouts, connection and OS errors) are
    treated as temporary; anything else is unknown and therefore a failure,
    never a success."""
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return OutcomeCategory.TEMPORARY
    return OutcomeCategory.UNKNOWN


@dataclass(frozen=True)
class FetchOutcome:
    """Result of one fetch attempt.

    Args:
        observed_at: when the observation was made (timezone-naive or aware,
            consistently with the clock used by the caller).
        status_code: HTTP status code, if a response was received; it takes
            precedence over ``category`` for the classification.
        category: outcome category for exceptions or response-less failures.
        retry_after: server-side waiting hint (``Retry-After``), either the
            number of seconds to wait or an absolute HTTP date.
        error: exception raised while fetching, for context.
    """

    observed_at: datetime
    status_code: int | None = None
    category: OutcomeCategory | None = None
    retry_after: float | str | datetime | None = None
    error: BaseException | None = None

    def __post_init__(self) -> None:
        if self.status_code is None and self.category is None:
            raise ValueError("FetchOutcome needs a status_code or a category")

    @classmethod
    def from_exception(
        cls,
        observed_at: datetime,
        error: BaseException,
        category: OutcomeCategory | None = None,
        retry_after: float | str | datetime | None = None,
    ) -> "FetchOutcome":
        "Build an outcome from an exception, classifying it when no category is given."
        return cls(
            observed_at=observed_at,
            category=category or classify_exception(error),
            retry_after=retry_after,
            error=error,
        )


@dataclass(frozen=True)
class RevisitDecision:
    """Next step for one address after an observation.

    Either ``retry_at`` is set and the URL may be fetched again at that time,
    or ``paused_reason`` is set and crawling the URL is suspended (possibly
    permanently) until an explicit manual resume.
    """

    url: str
    category: OutcomeCategory
    observed_at: datetime
    retry_at: datetime | None = None
    delay: timedelta | None = None
    paused_reason: str | None = None
    failure_count: int = 0
    server_wait: bool = False

    @property
    def retryable(self) -> bool:
        "Tell if the URL may be fetched again without manual intervention."
        return self.retry_at is not None

    @property
    def paused(self) -> bool:
        "Tell if crawling the URL is suspended pending a manual resume."
        return self.paused_reason is not None


def _categorize(outcome: FetchOutcome) -> OutcomeCategory:
    "Derive the outcome category, from the status code when present."
    if outcome.status_code is not None:
        code = outcome.status_code
        if code in _SUCCESS_STATUS:
            return OutcomeCategory.SUCCESS
        if code == 404:
            return OutcomeCategory.NOT_FOUND
        if code == 429:
            return OutcomeCategory.RATE_LIMITED
        if code in _PERMANENT_STATUS:
            return OutcomeCategory.PERMANENT
        if code in _TEMPORARY_STATUS or 500 <= code < 600:
            return OutcomeCategory.TEMPORARY
        return OutcomeCategory.UNKNOWN
    return outcome.category or OutcomeCategory.UNKNOWN


def _parse_http_date(text: str) -> datetime:
    "Parse an HTTP-date (RFC 7231) into a datetime, raise RetryAfterError if invalid."
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError) as err:
        raise RetryAfterError(f"unparseable Retry-After date: {text!r}") from err
    if parsed is None:
        raise RetryAfterError(f"unparseable Retry-After date: {text!r}")
    # naive clock in, naive datetime out; aware callers handle conversion
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _parse_retry_after(
    value: float | str | datetime, observed_at: datetime
) -> timedelta:
    """Parse a Retry-After hint into a non-negative timedelta.

    Accepts a number of seconds or an HTTP-date string; raises
    RetryAfterError on unparseable or absurd values."""
    if isinstance(value, datetime):
        return max(timedelta(0), value - observed_at)
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = value.strip()
        if not text:
            raise RetryAfterError("empty Retry-After value")
        try:
            seconds = float(text)
        except ValueError:
            parsed = _parse_http_date(text)
            return max(timedelta(0), parsed - observed_at)
    if seconds < 0 or seconds > _MAX_RETRY_AFTER_SECONDS:
        raise RetryAfterError(f"Retry-After value out of range: {value!r}")
    return timedelta(seconds=seconds)


class RevisitPolicy:
    """Turn fetch observations into retry timestamps or pause reasons.

    The policy only computes points in time: it never sleeps and never
    performs I/O. Every delay is derived from the supplied observation time,
    so the same sequence of observations always yields the same decisions.

    Args:
        delays: mapping of a retryable category to its first-retry delay.
        backoff_factor: multiplier applied to the base delay on each
            consecutive failure of the same URL.
        max_delay: ceiling for a computed retry delay.
        max_attempts: number of consecutive failures that triggers a pause:
            the URL is paused on the Nth failure.
        unknown_attempts: same limit applied to unknown outcomes (an unknown
            result is never a success).
    """

    def __init__(
        self,
        delays: Mapping[OutcomeCategory, float | timedelta] | None = None,
        backoff_factor: float = 2.0,
        max_delay: float | timedelta = timedelta(days=7),
        max_attempts: int = 5,
        unknown_attempts: int = 3,
    ) -> None:
        if backoff_factor < 1:
            raise ValueError("backoff_factor must be >= 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if unknown_attempts < 1:
            raise ValueError("unknown_attempts must be >= 1")
        self.backoff_factor = float(backoff_factor)
        self.max_delay = self._as_delta(max_delay)
        self.max_attempts = max_attempts
        self.unknown_attempts = unknown_attempts
        self.delays: dict[OutcomeCategory, timedelta] = dict(_DEFAULT_DELAYS)
        for category, delay in (delays or {}).items():
            if category not in _RETRYABLE_CATEGORIES:
                raise ValueError(f"no configurable delay for category {category!r}")
            self.delays[category] = self._as_delta(delay)

    @staticmethod
    def _as_delta(value: float | timedelta) -> timedelta:
        if isinstance(value, timedelta):
            return value
        if value < 0:
            raise ValueError("delays must be non-negative")
        return timedelta(seconds=float(value))

    def evaluate(
        self,
        url: str,
        outcome: FetchOutcome,
        failure_count: int = 0,
        last_retry_at: datetime | None = None,
        last_category: OutcomeCategory | None = None,
    ) -> RevisitDecision:
        """Compute the decision for one observation without storing it.

        Args:
            url: address the observation belongs to.
            outcome: the fetch observation.
            failure_count: consecutive failures already recorded for the URL.
            last_retry_at: previously scheduled retry time, used to keep the
                exponential backoff monotonic across observations.
            last_category: category of the previous recorded failure; the
                monotonic guard only applies to two failures of the same
                kind, so a 404 cooldown cannot swallow a later timeout.
        """
        category = _categorize(outcome)
        observed_at = outcome.observed_at
        if category is OutcomeCategory.SUCCESS:
            return RevisitDecision(
                url=url,
                category=category,
                observed_at=observed_at,
                failure_count=0,
            )

        attempts = failure_count + 1
        server_wait = False
        reason: str | None = None
        retry_at: datetime | None = None
        delay: timedelta | None = None

        if category is OutcomeCategory.PERMANENT:
            reason = f"permanent failure (status {outcome.status_code or 'n/a'})"
        elif attempts >= self._attempt_cap(category):
            reason = f"paused after {attempts} consecutive {category.value} failures"
        else:
            if outcome.retry_after is not None and (
                outcome.status_code in _RETRY_AFTER_STATUS
                or category is OutcomeCategory.RATE_LIMITED
            ):
                delay = _parse_retry_after(outcome.retry_after, observed_at)
                server_wait = True
            else:
                delay = self._backoff_delay(
                    category,
                    attempts,
                    observed_at,
                    last_retry_at if last_category is category else None,
                )
            retry_at = observed_at + delay

        return RevisitDecision(
            url=url,
            category=category,
            observed_at=observed_at,
            retry_at=retry_at,
            delay=delay,
            paused_reason=reason,
            failure_count=attempts,
            server_wait=server_wait,
        )

    def _attempt_cap(self, category: OutcomeCategory) -> int:
        if category is OutcomeCategory.UNKNOWN:
            return self.unknown_attempts
        return self.max_attempts

    def _backoff_delay(
        self,
        category: OutcomeCategory,
        attempts: int,
        observed_at: datetime,
        last_retry_at: datetime | None,
    ) -> timedelta:
        "Exponential backoff from the category base delay, kept monotonic."
        delay = min(
            self.max_delay,
            self.delays[category] * (self.backoff_factor ** (attempts - 1)),
        )
        if last_retry_at is not None:
            # never schedule earlier than the previous plan allowed
            delay = max(timedelta(0), max(delay, last_retry_at - observed_at))
        return delay


@dataclass
class _ScheduleEntry:
    "Mutable per-URL state."

    url: str
    failure_count: int = 0
    last_observed_at: datetime | None = None
    last_category: OutcomeCategory | None = None
    retry_at: datetime | None = None
    paused_reason: str | None = None
    history: list[RevisitDecision] = field(default_factory=list)

    @property
    def available(self) -> bool:
        "Tell if nothing currently prevents the URL from being drawn."
        return self.retry_at is None and self.paused_reason is None


def _default_normalize(url: str) -> str:
    "Key URLs in their normalized trailing-slash-less form."
    return normalize_url(url, trailing_slash=False)


class RevisitSchedule:
    """Per-URL revisit plans, independent from site to site.

    Each URL keeps its own failure streak and schedule; a failure on one host
    never affects another. A confirmed success resets the streak and clears
    any pause, while outcomes whose meaning is unknown stay failures until
    such a success arrives.

    Args:
        policy: policy used to evaluate observations (a default one if none).
        url_normalizer: function applied to URLs before they are keyed.
    """

    def __init__(
        self,
        policy: RevisitPolicy | None = None,
        url_normalizer: Callable[[str], str] | None = None,
    ) -> None:
        self.policy = policy or RevisitPolicy()
        self._normalize = url_normalizer or _default_normalize
        self._entries: dict[str, _ScheduleEntry] = {}
        self._lock = RLock()

    def __getstate__(self) -> dict[str, Any]:
        "Picklable state: the lock is excluded."
        with self._lock:
            return {
                "policy": self.policy,
                "_normalize": self._normalize,
                "_entries": self._entries,
            }

    def __setstate__(self, state: dict[str, Any]) -> None:
        "Restore state after unpickling, older schedules included."
        self._lock = RLock()
        self.policy = state.get("policy", RevisitPolicy())
        self._normalize = state.get("_normalize", _default_normalize)
        self._entries = state.get("_entries", {})

    # RECORDING

    def record(self, url: str, outcome: FetchOutcome) -> RevisitDecision:
        "Store an observation and return the updated decision for the URL."
        key = self._normalize(url)
        with self._lock:
            entry = self._entries.get(key)
            decision = self.policy.evaluate(
                key,
                outcome,
                failure_count=0 if entry is None else entry.failure_count,
                last_retry_at=None if entry is None else entry.retry_at,
                last_category=None if entry is None else entry.last_category,
            )
            if entry is None:
                entry = _ScheduleEntry(url=key)
                self._entries[key] = entry
            entry.last_observed_at = outcome.observed_at
            entry.last_category = decision.category
            if decision.category is OutcomeCategory.SUCCESS:
                # a confirmed success resets the whole history
                entry.failure_count = 0
                entry.retry_at = None
                entry.paused_reason = None
            else:
                entry.failure_count = decision.failure_count
                entry.retry_at = decision.retry_at
                entry.paused_reason = decision.paused_reason
            entry.history.append(decision)
            return decision

    # QUERIES

    def next_visit_at(self, url: str) -> datetime | None:
        """Return the earliest fetch time for the URL.

        ``None`` means it is immediately available (or unknown); a future
        datetime means it is cooling down; a permanently paused URL reports
        ``None`` as well, which :meth:`pause_reason` disambiguates."""
        key = self._normalize(url)
        with self._lock:
            entry = self._entries.get(key)
            return None if entry is None else entry.retry_at

    def is_available(self, url: str, now: datetime) -> bool:
        "Tell if the URL can be fetched at ``now`` (paused URLs never are)."
        key = self._normalize(url)
        with self._lock:
            entry = self._entries.get(key)
            # an unrecorded URL carries no restriction
            if entry is None:
                return True
            if entry.paused_reason is not None:
                return False
            return entry.retry_at is None or entry.retry_at <= now

    def is_paused(self, url: str) -> bool:
        "Tell if the URL is suspended pending a manual resume."
        key = self._normalize(url)
        with self._lock:
            entry = self._entries.get(key)
            return entry is not None and entry.paused_reason is not None

    def pause_reason(self, url: str) -> str | None:
        "Return the reason why the URL is paused, or None."
        key = self._normalize(url)
        with self._lock:
            entry = self._entries.get(key)
            return None if entry is None else entry.paused_reason

    def failure_count(self, url: str) -> int:
        "Return the number of consecutive failures recorded for the URL."
        key = self._normalize(url)
        with self._lock:
            entry = self._entries.get(key)
            return 0 if entry is None else entry.failure_count

    def get_due_urls(self, now: datetime) -> list[str]:
        "Return known URLs that are neither paused nor cooling down at ``now``."
        with self._lock:
            return [
                entry.url
                for entry in self._entries.values()
                if entry.paused_reason is None
                and (entry.retry_at is None or entry.retry_at <= now)
            ]

    def get_paused_urls(self) -> dict[str, str]:
        "Return paused URLs mapped to their pause reason."
        with self._lock:
            return {
                entry.url: entry.paused_reason
                for entry in self._entries.values()
                if entry.paused_reason is not None
            }

    # MANUAL RECOVERY

    def resume(self, url: str, at: datetime | None = None) -> datetime | None:
        """Manually clear a cooldown or pause and make the URL fetchable.

        Args:
            at: time from which the URL is fetchable; ``None`` makes it
                immediately available.

        Returns:
            the effective availability time, or ``None`` for immediate.
        """
        key = self._normalize(url)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _ScheduleEntry(url=key)
                self._entries[key] = entry
            entry.paused_reason = None
            entry.retry_at = at
            return entry.retry_at

    def reset(self, url: str | None = None) -> None:
        """Clear the history of one URL, or of the whole schedule."""
        with self._lock:
            if url is None:
                self._entries.clear()
            else:
                self._entries.pop(self._normalize(url), None)

    # TIMELINE

    def timeline(self, url: str) -> list[RevisitDecision]:
        "Return the recorded decisions for one URL, in observation order."
        key = self._normalize(url)
        with self._lock:
            entry = self._entries.get(key)
            return [] if entry is None else list(entry.history)

    def replay(
        self,
        url: str,
        outcomes: list[FetchOutcome],
    ) -> list[RevisitDecision]:
        """Replay a series of observations on a fresh per-URL state.

        Useful for tests and for previewing how the plan evolves along a
        timeline; the stored schedule is not modified."""
        key = self._normalize(url)
        decisions: list[RevisitDecision] = []
        failure_count = 0
        last_retry_at: datetime | None = None
        last_category: OutcomeCategory | None = None
        for outcome in outcomes:
            decision = self.policy.evaluate(
                key, outcome, failure_count, last_retry_at, last_category
            )
            decisions.append(decision)
            if decision.category is OutcomeCategory.SUCCESS:
                failure_count = 0
                last_retry_at = None
                last_category = None
            else:
                failure_count = decision.failure_count
                last_retry_at = decision.retry_at
                last_category = decision.category
        return decisions

    # SNAPSHOTS

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, url: object) -> bool:
        if not isinstance(url, str):
            return False
        with self._lock:
            return self._normalize(url) in self._entries

    def snapshot(self) -> dict[str, dict[str, Any]]:
        "Return a point-in-time copy of all per-URL states."
        with self._lock:
            return {
                key: {
                    "url": entry.url,
                    "failure_count": entry.failure_count,
                    "last_observed_at": entry.last_observed_at,
                    "last_category": entry.last_category,
                    "retry_at": entry.retry_at,
                    "paused_reason": entry.paused_reason,
                }
                for key, entry in self._entries.items()
            }


def format_timeline(url: str, decisions: list[RevisitDecision]) -> str:
    """Render a sequence of decisions as a human-readable timeline.

    Pure string formatting: no clock is read and nothing is slept."""
    lines = [f"revisit timeline for {url}"]
    for step, decision in enumerate(decisions, 1):
        if decision.category is OutcomeCategory.SUCCESS:
            plan = "available immediately (failure streak reset)"
        elif decision.paused:
            plan = f"paused: {decision.paused_reason}"
        else:
            hint = "server Retry-After" if decision.server_wait else "backoff"
            plan = (
                f"next visit at {decision.retry_at:%Y-%m-%d %H:%M:%S} "
                f"({decision.delay}, {hint})"
            )
        lines.append(
            f"{step:>2}. {decision.observed_at:%Y-%m-%d %H:%M:%S} "
            f"{decision.category.value:<12} failures={decision.failure_count} -> {plan}"
        )
    return "\n".join(lines)
