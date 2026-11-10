"""Explicitly enabled racing of safe-to-repeat read requests across mirrors.

The entry point :func:`race_read` fans a single read request out to several
mirror URLs of the same public data. The primary mirror is contacted first and
each further mirror is only started after a short staggered ``delay``, so a
healthy primary is never put under unnecessary load.

Participation is opt-in and restricted to read requests that the caller
explicitly declares safe to repeat (``idempotent=True``). A mirror only wins
when its response passes both the status check and, if supplied, the content
check: a mirror that answers first with an error can never steal the result.
Responses that do not win are closed, attempts that have not started when the
race ends are never sent, and the per-request ``timeout`` is independent of the
overall ``budget``.

Every attempt keeps an :class:`AttemptRecord` with the request URL, lifecycle
status, timing and validation detail, which is returned (or attached to the
raised :class:`RaceError`) so callers retain the full attempt history.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .exceptions import RequestException
from .models import Response
from .sessions import Session

logger = logging.getLogger(__name__)

#: HTTP methods that are safe to issue repeatedly without side effects.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Status values stored on :class:`AttemptRecord`.
WON = "won"
REJECTED = "rejected"
FAILED = "failed"
ABANDONED = "abandoned"
NOT_STARTED = "not_started"

#: Bounded wait for in-flight attempts during cleanup. Requests normally
#: return earlier because they carry their own per-request ``timeout``.
_CLEANUP_GRACE = 5.0


class RaceError(RequestException):
    """Raised when no mirror produced a response satisfying every check."""

    def __init__(
        self,
        message: str,
        *,
        attempts: list[AttemptRecord],
        budget_timeout: bool,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.budget_timeout = budget_timeout


@dataclass
class AttemptRecord:
    """Lifecycle record of a single mirror attempt."""

    name: str
    url: str
    started: bool = False
    finished: bool = False
    status: str = "pending"
    detail: str = ""
    response: Response | None = None
    error: Exception | None = None
    started_at: float | None = None
    finished_at: float | None = None
    elapsed: float | None = None
    response_closed: bool = False


@dataclass
class RaceResult:
    """The winning response together with the full attempt history."""

    response: Response
    winner: str
    attempts: list[AttemptRecord]


StatusValidator = Callable[[Response], bool]
ContentValidator = Callable[[Response], bool]


class _MirrorAttempt(threading.Thread):
    """Worker thread that performs one mirror request and validates it."""

    def __init__(
        self,
        *,
        index: int,
        name: str,
        url: str,
        session: Session,
        method: str,
        timeout: float | tuple[float, float] | None,
        request_kwargs: dict[str, Any],
        status_validator: StatusValidator,
        content_validator: ContentValidator | None,
        condition: threading.Condition,
    ) -> None:
        super().__init__(name=f"requests-race-{name}", daemon=True)
        self.index = index
        self.record = AttemptRecord(name=name, url=url)
        self.discard = threading.Event()
        self._condition = condition
        self._session = session
        self._method = method
        self._timeout = timeout
        self._request_kwargs = request_kwargs
        self._status_validator = status_validator
        self._content_validator = content_validator

    def run(self) -> None:
        record = self.record
        record.started = True
        record.started_at = time.monotonic()
        try:
            response = self._session.request(
                self._method,
                record.url,
                timeout=self._timeout,
                **self._request_kwargs,
            )
            if self.discard.is_set():
                record.response = response
                response.close()
                record.response_closed = True
                record.status = ABANDONED
                record.detail = "finished after the race had already ended"
                return
            if not self._status_validator(response):
                self._reject(
                    response,
                    f"status check failed: HTTP {response.status_code}",
                )
                return
            if self._content_validator is not None:
                try:
                    accepted = self._content_validator(response)
                except Exception as exc:
                    self._reject(
                        response,
                        f"content check raised {type(exc).__name__}: {exc}",
                    )
                    return
                if not accepted:
                    self._reject(response, "content check failed")
                    return
            record.response = response
            record.status = WON
            record.detail = f"HTTP {response.status_code}"
        except Exception as exc:  # network errors, timeouts, ...
            record.error = exc
            if self.discard.is_set():
                record.status = ABANDONED
                record.detail = f"abandoned while in flight ({type(exc).__name__})"
            else:
                record.status = FAILED
                record.detail = f"{type(exc).__name__}: {exc}"
        finally:
            with self._condition:
                record.finished = True
                record.finished_at = time.monotonic()
                if record.started_at is not None:
                    record.elapsed = record.finished_at - record.started_at
                self._condition.notify_all()

    def _reject(self, response: Response, detail: str) -> None:
        self.record.response = response
        response.close()
        self.record.response_closed = True
        self.record.status = REJECTED
        self.record.detail = detail


def _mirror_name(index: int) -> str:
    if index == 0:
        return "primary"
    if index == 1:
        return "backup"
    return f"mirror-{index}"


def race_read(
    method: str,
    mirror_urls: Sequence[str],
    *,
    idempotent: bool,
    delay: float = 0.25,
    timeout: float | tuple[float, float] | None = None,
    budget: float | None = None,
    status_validator: StatusValidator | None = None,
    content_validator: ContentValidator | None = None,
    session: Session | None = None,
    **kwargs: Any,
) -> RaceResult:
    """Race a safe-to-repeat read request across mirror URLs.

    :param method: HTTP method; only read methods (``GET``, ``HEAD``,
        ``OPTIONS``, ``TRACE``) are accepted.
    :param mirror_urls: Mirror URLs of the same data, ordered by preference;
        the first entry is the primary and is contacted immediately.
    :param idempotent: Must be passed as ``True``: the caller asserts the
        request is safe to issue more than once. The race is never started
        without this explicit declaration.
    :param delay: Short wait between starting consecutive mirror attempts.
    :param timeout: Per-request timeout forwarded to every attempt; kept
        independent of ``budget``.
    :param budget: Total wall-clock budget for the whole race. Attempts still
        unscheduled when the budget expires are never sent; in-flight attempts
        are abandoned and close their own responses as soon as they finish.
    :param status_validator: Predicate deciding whether a response's status is
        acceptable; defaults to :attr:`Response.ok`.
    :param content_validator: Optional predicate deciding whether the response
        body is acceptable.
    :param session: Optional :class:`Session` to reuse; a temporary one is
        created and closed when omitted.
    :param kwargs: Extra keyword arguments forwarded to every
        :meth:`Session.request` call.
    :raises RaceError: If no mirror produces a response that passes every
        check. The attempt history is attached as ``RaceError.attempts``.
    """
    if not idempotent:
        raise ValueError(
            "race_read requires idempotent=True: the caller must explicitly "
            "declare the read request safe to repeat"
        )
    method = method.upper()
    if method not in READ_METHODS:
        raise ValueError(f"race_read only accepts safe read methods, got {method!r}")
    urls = list(mirror_urls)
    if not urls:
        raise ValueError("race_read requires at least one mirror URL")
    if delay < 0:
        raise ValueError("delay must be non-negative")
    if budget is not None and budget <= 0:
        raise ValueError("budget must be positive")

    status_check = status_validator or (lambda response: response.ok)
    owns_session = session is None
    sess = session or Session()
    condition = threading.Condition()

    attempts: list[_MirrorAttempt] = [
        _MirrorAttempt(
            index=index,
            name=_mirror_name(index),
            url=url,
            session=sess,
            method=method,
            timeout=timeout,
            request_kwargs=kwargs,
            status_validator=status_check,
            content_validator=content_validator,
            condition=condition,
        )
        for index, url in enumerate(urls)
    ]
    pending = list(attempts)
    active: list[_MirrorAttempt] = []
    winner: _MirrorAttempt | None = None
    start = time.monotonic()
    deadline = start + budget if budget is not None else None

    try:
        with condition:
            while True:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    break

                while pending and now >= start + pending[0].index * delay:
                    attempt = pending.pop(0)
                    active.append(attempt)
                    logger.debug(
                        "starting mirror attempt %s (%s)",
                        attempt.record.name,
                        attempt.record.url,
                    )
                    attempt.start()

                for attempt in [item for item in active if item.record.finished]:
                    active.remove(attempt)
                    if attempt.record.status == WON and winner is None:
                        winner = attempt

                if winner is not None:
                    break
                if not active and not pending:
                    break

                now = time.monotonic()
                wait_timeout: float | None = None
                if pending:
                    wait_timeout = start + pending[0].index * delay - now
                if deadline is not None:
                    budget_left = deadline - now
                    wait_timeout = (
                        budget_left
                        if wait_timeout is None
                        else min(wait_timeout, budget_left)
                    )
                condition.wait_for(
                    lambda: any(item.record.finished for item in active),
                    timeout=wait_timeout,
                )

            records = [attempt.record for attempt in attempts]

            if winner is not None:
                _mark_not_started(
                    pending, "race finished before this attempt was scheduled"
                )
                _abandon_active(
                    active, "still in flight when the winning response arrived"
                )
                _drain(condition, active)
                _close_losing_responses(attempts, winner)
                winning_response = winner.record.response
                assert winning_response is not None
                return RaceResult(
                    response=winning_response,
                    winner=winner.record.name,
                    attempts=records,
                )

            budget_exceeded = deadline is not None and time.monotonic() >= deadline
            _mark_not_started(
                pending,
                "total budget exhausted before this attempt was scheduled"
                if budget_exceeded
                else "race ended before this attempt was scheduled",
            )
            _abandon_active(
                active,
                "total budget exhausted while the request was in flight"
                if budget_exceeded
                else "still in flight when the race ended",
            )
            _drain(condition, active)
            _close_losing_responses(attempts, None)

        error = RaceError(
            "no mirror returned a response satisfying all win conditions",
            attempts=records,
            budget_timeout=budget_exceeded,
        )
        for record in records:
            logger.info(
                "mirror attempt %s: %s (%s)",
                record.name,
                record.status,
                record.detail,
            )
        raise error
    finally:
        if owns_session:
            sess.close()


def _mark_not_started(attempts: list[_MirrorAttempt], detail: str) -> None:
    for attempt in attempts:
        record = attempt.record
        record.status = NOT_STARTED
        record.detail = detail


def _abandon_active(active: list[_MirrorAttempt], detail: str) -> None:
    for attempt in active:
        attempt.discard.set()
        if not attempt.record.finished:
            attempt.record.status = ABANDONED
            attempt.record.detail = detail


def _close_losing_responses(
    attempts: list[_MirrorAttempt], winner: _MirrorAttempt | None
) -> None:
    """Close every open response that did not win the race."""
    for attempt in attempts:
        if attempt is winner:
            continue
        record = attempt.record
        if record.response is None or record.response_closed:
            continue
        record.response.close()
        record.response_closed = True
        if record.status == WON:
            record.status = REJECTED
            record.detail = (
                "superseded by an earlier valid mirror"
                if winner is not None
                else "closed because the race ended without a winner"
            )


def _drain(condition: threading.Condition, active: list[_MirrorAttempt]) -> None:
    """Wait briefly for in-flight attempts so their responses get closed."""
    if not active:
        return
    with condition:
        finished = condition.wait_for(
            lambda: all(attempt.record.finished for attempt in active),
            timeout=_CLEANUP_GRACE,
        )
    if not finished:
        for attempt in active:
            if not attempt.record.finished:
                logger.debug(
                    "mirror attempt %s did not finish during cleanup; "
                    "the daemon thread releases its connection once the "
                    "per-request timeout expires",
                    attempt.record.name,
                )
