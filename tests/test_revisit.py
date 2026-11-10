"""
Unit tests for the configurable revisit policy (courlan.revisit).
"""

import pickle
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from courlan import (
    FetchOutcome,
    OutcomeCategory,
    RetryAfterError,
    RevisitPolicy,
    RevisitSchedule,
    classify_exception,
    format_timeline,
)
from courlan.revisit import _ScheduleEntry

T0 = datetime(2026, 11, 10, 8, 0, 0)


def _outcome(
    status: int | None = None,
    category: OutcomeCategory | None = None,
    at: datetime = T0,
    **kwargs: Any,
) -> FetchOutcome:
    return FetchOutcome(observed_at=at, status_code=status, category=category, **kwargs)


# --- classification -----------------------------------------------------------


def test_status_code_classification():
    "HTTP status codes map to distinct, meaningful categories."
    policy = RevisitPolicy()
    cases = {
        200: OutcomeCategory.SUCCESS,
        204: OutcomeCategory.SUCCESS,
        304: OutcomeCategory.SUCCESS,
        404: OutcomeCategory.NOT_FOUND,
        429: OutcomeCategory.RATE_LIMITED,
        410: OutcomeCategory.PERMANENT,
        403: OutcomeCategory.PERMANENT,
        408: OutcomeCategory.TEMPORARY,
        500: OutcomeCategory.TEMPORARY,
        503: OutcomeCategory.TEMPORARY,
        418: OutcomeCategory.UNKNOWN,
    }
    for code, expected in cases.items():
        decision = policy.evaluate("https://x.org/a", _outcome(code))
        assert decision.category is expected, code


def test_exception_classification():
    "Timeouts and connection errors are temporary; other errors are unknown."
    assert classify_exception(TimeoutError()) is OutcomeCategory.TEMPORARY
    assert classify_exception(ConnectionError()) is OutcomeCategory.TEMPORARY
    assert classify_exception(OSError()) is OutcomeCategory.TEMPORARY
    assert classify_exception(ValueError()) is OutcomeCategory.UNKNOWN
    outcome = FetchOutcome.from_exception(T0, TimeoutError("slow"))
    assert outcome.category is OutcomeCategory.TEMPORARY
    assert isinstance(outcome.error, TimeoutError)


def test_fetch_outcome_requires_signal():
    "An observation without status code or category is rejected."
    with pytest.raises(ValueError):
        FetchOutcome(observed_at=T0)


# --- distinct outcomes, distinct plans ----------------------------------------


def test_404_timeout_and_429_get_distinct_plans():
    "The headline case: one 404, one timeout and one 429 never share a state."
    policy = RevisitPolicy()
    not_found = policy.evaluate("https://x.org/a", _outcome(404))
    timeout = policy.evaluate(
        "https://x.org/a", FetchOutcome.from_exception(T0, TimeoutError())
    )
    rate_limited = policy.evaluate("https://x.org/a", _outcome(429))

    assert not_found.category is OutcomeCategory.NOT_FOUND
    assert timeout.category is OutcomeCategory.TEMPORARY
    assert rate_limited.category is OutcomeCategory.RATE_LIMITED

    assert not_found.delay == timedelta(days=1)
    assert timeout.delay == timedelta(seconds=30)
    assert rate_limited.delay == timedelta(minutes=1)
    assert len({not_found.retry_at, timeout.retry_at, rate_limited.retry_at}) == 3
    assert all(d.retryable and not d.paused for d in (not_found, timeout, rate_limited))


# --- backoff and server Retry-After -------------------------------------------


def test_consecutive_failures_use_exponential_backoff():
    "Repeated rate limiting grows the delay and is capped by max_delay."
    policy = RevisitPolicy(
        delays={OutcomeCategory.RATE_LIMITED: 10},
        backoff_factor=2,
        max_delay=100,
        max_attempts=4,
    )
    schedule = RevisitSchedule(policy)
    url = "https://x.org/a"
    expected = [10, 20, 40]
    for attempt, seconds in enumerate(expected, 1):
        decision = schedule.record(url, _outcome(429, at=T0 + timedelta(hours=attempt)))
        assert decision.failure_count == attempt
        assert decision.delay == timedelta(seconds=seconds)
        assert schedule.failure_count(url) == attempt
    # the next one crosses the attempt cap and pauses instead of retrying
    paused = schedule.record(url, _outcome(429, at=T0 + timedelta(hours=4)))
    assert paused.paused is True and paused.retry_at is None


def test_retry_after_seconds_takes_precedence():
    "An explicit Retry-After hint overrides the computed backoff."
    policy = RevisitPolicy()
    decision = policy.evaluate("https://x.org/a", _outcome(429, retry_after=120))
    assert decision.delay == timedelta(seconds=120)
    assert decision.server_wait is True
    assert decision.retry_at == T0 + timedelta(seconds=120)
    # string form works too
    decision = policy.evaluate("https://x.org/a", _outcome(429, retry_after="45"))
    assert decision.delay == timedelta(seconds=45)


def test_retry_after_http_date():
    "An absolute HTTP-date Retry-After is measured from the observation time."
    policy = RevisitPolicy()
    decision = policy.evaluate(
        "https://x.org/a",
        _outcome(503, retry_after="Tue, 10 Nov 2026 08:02:00 GMT"),
    )
    assert decision.server_wait is True
    assert decision.delay == timedelta(minutes=2)


def test_retry_after_datetime_and_validation():
    "datetime hints are supported; bad values raise instead of being guessed."
    policy = RevisitPolicy()
    target = T0 + timedelta(seconds=90)
    decision = policy.evaluate("https://x.org/a", _outcome(429, retry_after=target))
    assert decision.delay == timedelta(seconds=90)
    with pytest.raises(RetryAfterError):
        policy.evaluate("https://x.org/a", _outcome(429, retry_after="not a date"))
    with pytest.raises(RetryAfterError):
        policy.evaluate("https://x.org/a", _outcome(429, retry_after=-5))
    with pytest.raises(RetryAfterError):
        policy.evaluate("https://x.org/a", _outcome(429, retry_after=10**9))
    # a date in the past clamps to a zero-length wait
    decision = policy.evaluate(
        "https://x.org/a", _outcome(429, retry_after=T0 - timedelta(hours=1))
    )
    assert decision.delay == timedelta(0)


def test_backoff_never_runs_before_the_previous_plan():
    "A late observation cannot schedule a retry earlier than the last plan."
    policy = RevisitPolicy(delays={OutcomeCategory.TEMPORARY: 30}, backoff_factor=2)
    first = policy.evaluate("https://x.org/a", _outcome(500, at=T0))
    # next failure happens immediately, while the first plan still points ahead
    second = policy.evaluate(
        "https://x.org/a",
        _outcome(500, at=T0 + timedelta(seconds=5)),
        failure_count=1,
        last_retry_at=first.retry_at,
    )
    assert second.retry_at is not None and first.retry_at is not None
    assert second.retry_at >= first.retry_at


# --- pausing and manual recovery ----------------------------------------------


def test_repeated_failures_eventually_pause():
    "Past the attempt cap the URL is paused with an explicit reason."
    policy = RevisitPolicy(max_attempts=3)
    schedule = RevisitSchedule(policy)
    url = "https://x.org/a"
    for attempt in range(1, 4):
        decision = schedule.record(
            url, _outcome(500, at=T0 + timedelta(minutes=attempt))
        )
        assert decision.failure_count == attempt
    assert decision.retryable is False and decision.paused is True
    assert decision.paused_reason is not None and "temporary" in decision.paused_reason
    assert schedule.is_paused(url)
    assert schedule.pause_reason(url) == decision.paused_reason
    assert schedule.is_available(url, T0 + timedelta(days=1)) is False
    assert schedule.get_due_urls(T0 + timedelta(days=1)) == []


def test_permanent_failure_pauses_immediately():
    "A 410/403 is not retried: it is paused on the first observation."
    schedule = RevisitSchedule()
    decision = schedule.record("https://x.org/gone", _outcome(410))
    assert decision.paused is True and decision.retry_at is None
    assert decision.paused_reason is not None
    assert "permanent" in decision.paused_reason
    assert schedule.failure_count("https://x.org/gone") == 1
    assert schedule.next_visit_at("https://x.org/gone") is None


def test_unknown_outcomes_are_failures_and_have_their_own_cap():
    "Unknown results never count as success and pause after unknown_attempts."
    policy = RevisitPolicy(unknown_attempts=2, max_attempts=5)
    schedule = RevisitSchedule(policy)
    url = "https://x.org/odd"
    first = schedule.record(url, _outcome(418))
    second = schedule.record(url, _outcome(418, at=T0 + timedelta(minutes=10)))
    assert first.category is OutcomeCategory.UNKNOWN and first.retryable
    assert second.paused is True
    assert schedule.failure_count(url) == 2


def test_manual_resume_clears_pause_and_cooldown():
    "A human can reopen a paused or cooling-down URL without touching history rules."
    schedule = RevisitSchedule(RevisitPolicy(max_attempts=1))
    url = "https://x.org/a"
    schedule.record(url, _outcome(500))
    assert schedule.is_paused(url)
    # immediate availability
    assert schedule.resume(url) is None
    assert not schedule.is_paused(url)
    assert schedule.is_available(url, T0)
    # scheduled availability
    later = T0 + timedelta(hours=6)
    assert schedule.resume(url, at=later) == later
    assert not schedule.is_available(url, T0)
    assert schedule.is_available(url, later)
    # resuming an unknown URL creates an entry
    schedule.resume("https://new.org/x")
    assert "https://new.org/x" in schedule


def test_resume_keeps_the_failure_streak():
    "Manual recovery reopens the URL but does not erase its failure history."
    schedule = RevisitSchedule(RevisitPolicy(max_attempts=2))
    url = "https://x.org/a"
    schedule.record(url, _outcome(500, at=T0))
    schedule.record(url, _outcome(500, at=T0 + timedelta(minutes=1)))
    assert schedule.is_paused(url)
    assert schedule.failure_count(url) == 2
    schedule.resume(url)
    # the streak survives the resume, so the next failure pauses again at once
    assert schedule.failure_count(url) == 2
    again = schedule.record(url, _outcome(500, at=T0 + timedelta(minutes=2)))
    assert again.paused is True and again.failure_count == 3
    # reset() is the operation that actually wipes the history
    schedule.reset(url)
    assert url not in schedule
    fresh = schedule.record(url, _outcome(500, at=T0))
    assert fresh.failure_count == 1 and fresh.retryable


# --- success reset -------------------------------------------------------------


def test_success_resets_the_failure_streak():
    "A confirmed success clears the streak, the cooldown and the pause."
    schedule = RevisitSchedule(RevisitPolicy(max_attempts=2))
    url = "https://x.org/a"
    schedule.record(url, _outcome(500, at=T0))
    schedule.record(url, _outcome(500, at=T0 + timedelta(minutes=1)))
    assert schedule.is_paused(url)
    decision = schedule.record(url, _outcome(200, at=T0 + timedelta(minutes=2)))
    assert decision.category is OutcomeCategory.SUCCESS
    assert decision.failure_count == 0 and decision.retry_at is None
    assert schedule.failure_count(url) == 0
    assert not schedule.is_paused(url)
    assert schedule.is_available(url, T0)
    # the next failure starts the backoff from the base delay again
    again = schedule.record(url, _outcome(500, at=T0 + timedelta(minutes=3)))
    assert again.failure_count == 1
    assert again.delay == timedelta(seconds=30)


def test_non_success_never_resets():
    "404s and unknown results keep the streak running."
    schedule = RevisitSchedule()
    url = "https://x.org/a"
    schedule.record(url, _outcome(500, at=T0))
    schedule.record(url, _outcome(404, at=T0 + timedelta(minutes=1)))
    assert schedule.failure_count(url) == 2
    schedule.record(url, _outcome(418, at=T0 + timedelta(minutes=2)))
    assert schedule.failure_count(url) == 3


# --- cross-site independence ---------------------------------------------------


def test_schedules_are_independent_across_sites():
    "One host's failures never alter another host's plan."
    schedule = RevisitSchedule()
    schedule.record("https://a.com/1", _outcome(429, at=T0))
    schedule.record("https://a.com/1", _outcome(429, at=T0 + timedelta(minutes=2)))
    assert schedule.failure_count("https://a.com/1") == 2
    assert schedule.failure_count("https://b.org/1") == 0
    assert schedule.is_available("https://b.org/1", T0) is True
    due = schedule.get_due_urls(T0 + timedelta(seconds=1))
    assert "https://b.org/1" not in due  # never recorded, not part of the frontier
    # b.org starts its own streak at the base delay
    decision = schedule.record("https://b.org/1", _outcome(500, at=T0))
    assert decision.failure_count == 1
    assert decision.delay == timedelta(seconds=30)
    assert schedule.failure_count("https://a.com/1") == 2


def test_due_and_paused_listings():
    "Due and paused URLs are reported separately at a given point in time."
    schedule = RevisitSchedule(RevisitPolicy(max_attempts=2))
    schedule.record("https://a.com/1", _outcome(429, at=T0))  # cooldown
    schedule.record("https://b.org/1", _outcome(200, at=T0))  # available
    schedule.record("https://c.org/1", _outcome(500, at=T0))
    schedule.record(
        "https://c.org/1", _outcome(500, at=T0 + timedelta(minutes=1))
    )  # paused on the 2nd failure
    at = T0 + timedelta(seconds=1)
    assert schedule.get_due_urls(at) == ["https://b.org/1"]
    assert schedule.get_paused_urls() == {
        "https://c.org/1": schedule.pause_reason("https://c.org/1")
    }
    # after the rate-limit window elapses, a.com becomes due again
    assert schedule.get_due_urls(T0 + timedelta(minutes=2)) == [
        "https://a.com/1",
        "https://b.org/1",
    ]


# --- timeline ------------------------------------------------------------------


def test_timeline_tracks_how_the_plan_evolves():
    "The same URL's plan changes step by step as outcomes differ."
    schedule = RevisitSchedule()
    url = "https://example.org/page"
    observations = [
        _outcome(404, at=T0),
        FetchOutcome.from_exception(
            T0 + timedelta(hours=2), TimeoutError("temporary timeout")
        ),
        _outcome(429, at=T0 + timedelta(hours=5), retry_after=120),
        _outcome(429, at=T0 + timedelta(hours=6), retry_after=120),
        _outcome(200, at=T0 + timedelta(hours=7)),
    ]
    decisions = [schedule.record(url, outcome) for outcome in observations]

    assert [d.category for d in decisions] == [
        OutcomeCategory.NOT_FOUND,
        OutcomeCategory.TEMPORARY,
        OutcomeCategory.RATE_LIMITED,
        OutcomeCategory.RATE_LIMITED,
        OutcomeCategory.SUCCESS,
    ]
    assert [d.failure_count for d in decisions] == [1, 2, 3, 4, 0]
    # 404: one day; timeout: 30s backoff; 429: server hint twice
    assert decisions[0].retry_at == T0 + timedelta(days=1)
    assert decisions[1].delay == timedelta(seconds=60)
    assert decisions[2].delay == timedelta(seconds=120)
    assert decisions[2].server_wait is True
    assert decisions[4].retry_at is None

    stored = schedule.timeline(url)
    assert stored == decisions
    text = format_timeline(url, stored)
    assert url in text
    assert "not_found" in text and "rate_limited" in text and "success" in text
    assert "server Retry-After" in text and "backoff" in text
    assert "reset" in text


def test_replay_matches_recorded_timeline_without_mutation():
    "replay() previews a timeline on fresh state and leaves the store untouched."
    policy = RevisitPolicy(max_attempts=3)
    schedule = RevisitSchedule(policy)
    url = "https://x.org/a"
    outcomes = [_outcome(500, at=T0 + timedelta(minutes=i)) for i in range(4)]
    replayed = schedule.replay(url, outcomes)
    assert len(schedule) == 0  # nothing stored
    recorded = [schedule.record(url, outcome) for outcome in outcomes]
    assert replayed == recorded
    assert recorded[-1].paused is True


def test_timeline_renders_pause_reason():
    "A paused step appears with its reason in the rendered timeline."
    schedule = RevisitSchedule(RevisitPolicy(max_attempts=1))
    schedule.record("https://x.org/a", _outcome(410, at=T0))
    text = format_timeline("https://x.org/a", schedule.timeline("https://x.org/a"))
    assert "paused:" in text and "permanent" in text


# --- storage details -----------------------------------------------------------


def test_url_normalization_groups_variants():
    "Host case and trailing-slash variants of one address share a plan."
    schedule = RevisitSchedule()
    schedule.record("https://Example.org/Path/", _outcome(429, at=T0))
    assert schedule.failure_count("https://example.org/Path") == 1
    assert schedule.next_visit_at("https://example.org/Path/") is not None
    # the URL path itself stays case-sensitive
    assert schedule.failure_count("https://example.org/path") == 0


def test_schedule_reset_and_snapshot():
    "reset() drops one URL or everything; snapshot copies the current state."
    schedule = RevisitSchedule()
    schedule.record("https://a.com/1", _outcome(500, at=T0))
    schedule.record("https://b.org/1", _outcome(404, at=T0))
    assert len(schedule) == 2
    schedule.reset("https://a.com/1")
    assert "https://a.com/1" not in schedule and len(schedule) == 1
    snap = schedule.snapshot()
    assert snap["https://b.org/1"]["last_category"] is OutcomeCategory.NOT_FOUND
    schedule.reset()
    assert len(schedule) == 0


def test_schedule_pickles_round_trip():
    "A schedule with cooldowns and pauses survives a pickle round trip."
    schedule = RevisitSchedule(RevisitPolicy(max_attempts=2))
    schedule.record("https://a.com/1", _outcome(500, at=T0))
    schedule.record("https://b.org/1", _outcome(410, at=T0))
    restored = pickle.loads(pickle.dumps(schedule))
    assert restored.failure_count("https://a.com/1") == 1
    assert restored.is_paused("https://b.org/1")
    assert restored.timeline("https://a.com/1") == schedule.timeline("https://a.com/1")
    # the restored schedule keeps evaluating observations
    decision = restored.record(
        "https://a.com/1", _outcome(500, at=T0 + timedelta(minutes=1))
    )
    assert decision.paused is True


def test_schedule_entry_defaults():
    "A fresh entry is available and has no history."
    entry = _ScheduleEntry(url="https://x.org/a")
    assert entry.available and entry.history == []


# --- configuration validation --------------------------------------------------


def test_policy_configuration_validation():
    "Inconsistent configuration fails early."
    with pytest.raises(ValueError):
        RevisitPolicy(backoff_factor=0.5)
    with pytest.raises(ValueError):
        RevisitPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RevisitPolicy(unknown_attempts=0)
    with pytest.raises(ValueError):
        RevisitPolicy(delays={OutcomeCategory.SUCCESS: 10})
    with pytest.raises(ValueError):
        RevisitPolicy(delays={OutcomeCategory.PERMANENT: 10})
    with pytest.raises(ValueError):
        RevisitPolicy(delays={OutcomeCategory.TEMPORARY: -1})


def test_custom_delays_accept_timedelta():
    "Delays can be given as timedelta values and override the defaults."
    policy = RevisitPolicy(
        delays={OutcomeCategory.NOT_FOUND: timedelta(hours=12)},
        max_delay=timedelta(days=2),
    )
    decision = policy.evaluate("https://x.org/a", _outcome(404))
    assert decision.delay == timedelta(hours=12)


def test_module_never_sleeps():
    "The policy computes times only; it never sleeps and reads no clock."
    text = (Path(__file__).parent.parent / "courlan" / "revisit.py").read_text()
    assert "time.sleep" not in text
    assert "import time" not in text
    assert "datetime.now" not in text
    assert "datetime.utcnow" not in text
