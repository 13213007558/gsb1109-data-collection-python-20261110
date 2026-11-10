# Revisit Policy — Distinct Plans for Distinct Outcomes

A 404, a one-off timeout and repeated 429 responses are different situations
and must not collapse into one generic "unvisited" bucket. The
`courlan.revisit` module turns each fetch observation into a decision: either
the earliest time the address may be fetched again, or a reason for which
crawling it is paused.

Everything is computed from times you supply. The module never calls
`sleep()` and never reads the clock, so the same sequence of observations
always produces the same plan — fast, deterministic tests included.


## Core objects

- `FetchOutcome` — one observation: an `observed_at` timestamp, an optional
  HTTP `status_code`, an optional `category` (for exceptions), and an optional
  `retry_after` server hint.
- `OutcomeCategory` — `SUCCESS`, `NOT_FOUND`, `RATE_LIMITED`, `TEMPORARY`,
  `PERMANENT`, `UNKNOWN`. A status code, when present, drives the category.
- `RevisitPolicy` — pure decision logic: base delays per category,
  exponential backoff, attempt limits and `Retry-After` handling.
- `RevisitSchedule` — per-URL state on top of a policy: failure streaks,
  cooldowns, pauses, manual resume and timelines. Schedules for different
  sites never interfere with each other.
- `RevisitDecision` — the result: a `retry_at` timestamp **or** a
  `paused_reason`, plus the current failure count.


## The headline case: 404, timeout and 429 differ

```python
from datetime import datetime, timedelta
from courlan import FetchOutcome, RevisitPolicy

t0 = datetime(2026, 11, 10, 8, 0, 0)
policy = RevisitPolicy()

not_found = policy.evaluate("https://example.org/a", FetchOutcome(t0, status_code=404))
timeout    = policy.evaluate(
    "https://example.org/a",
    FetchOutcome.from_exception(t0, TimeoutError("temporary timeout")),
)
rate_limited = policy.evaluate(
    "https://example.org/a", FetchOutcome(t0, status_code=429)
)

not_found.delay     # 1 day   — the page may have disappeared
timeout.delay       # 30 secs — a transient network issue
rate_limited.delay  # 1 minute — the host asked us to slow down
```

Each outcome gets its own category, delay and next-visit time; none of them is
silently marked as visited or as success.


## Observations and exception categories

Use a status code when you got a response, or a category (and optionally the
exception) when you did not:

```python
from courlan import FetchOutcome, OutcomeCategory, classify_exception

FetchOutcome(t0, status_code=503, retry_after=120)
FetchOutcome(t0, category=OutcomeCategory.TEMPORARY)
FetchOutcome.from_exception(t0, TimeoutError())  # classified as TEMPORARY

classify_exception(ConnectionError())  # OutcomeCategory.TEMPORARY
classify_exception(ValueError())       # OutcomeCategory.UNKNOWN (never success)
```

An outcome without a status code **and** without a category is rejected with
`ValueError`, so an unknown result can never be recorded as a success.


## Server-side waiting hints

`Retry-After` on a 429 or 503 overrides the computed backoff. Seconds, numeric
strings, `datetime` objects and HTTP-date strings are all accepted:

```python
policy.evaluate(url, FetchOutcome(t0, status_code=429, retry_after=120))
policy.evaluate(url, FetchOutcome(t0, status_code=503,
                                  retry_after="Tue, 10 Nov 2026 08:02:00 GMT"))
```

Unparseable or out-of-range hints raise `RetryAfterError` instead of being
guessed; a date in the past clamps to a zero-length wait.


## Configurable policy

```python
from datetime import timedelta
from courlan import OutcomeCategory, RevisitPolicy, RevisitSchedule

policy = RevisitPolicy(
    delays={
        OutcomeCategory.NOT_FOUND: timedelta(hours=12),
        OutcomeCategory.RATE_LIMITED: 10,      # seconds also accepted
        OutcomeCategory.TEMPORARY: 30,
        OutcomeCategory.UNKNOWN: 300,
    },
    backoff_factor=2.0,    # delay doubles on each consecutive same-kind failure
    max_delay=timedelta(days=7),
    max_attempts=5,        # pause on the 5th consecutive failure
    unknown_attempts=3,    # unknown outcomes get their own, tighter limit
)
schedule = RevisitSchedule(policy)
```

Consecutive failures of the same kind use exponential backoff. The monotonic
guard only applies within one category: a long 404 cooldown cannot swallow a
later, shorter timeout backoff.


## A timeline for one address

`RevisitSchedule.record()` stores every observation and keeps the full
decision history, so `timeline()` shows how the plan evolves. The example
below runs entirely against fixed timestamps — no real waiting:

```python
from courlan import format_timeline

url = "https://example.org/page"
observations = [
    FetchOutcome(t0, status_code=404),
    FetchOutcome.from_exception(t0 + timedelta(hours=2), TimeoutError()),
    FetchOutcome(t0 + timedelta(hours=5), status_code=429, retry_after=120),
    FetchOutcome(t0 + timedelta(hours=6), status_code=429, retry_after=120),
    FetchOutcome(t0 + timedelta(hours=7), status_code=200),
]
for outcome in observations:
    schedule.record(url, outcome)

print(format_timeline(url, schedule.timeline(url)))
```

```text
revisit timeline for https://example.org/page
 1. 2026-11-10 08:00:00 not_found    failures=1 -> next visit at 2026-11-11 08:00:00 (1 day, 0:00:00, backoff)
 2. 2026-11-10 10:00:00 temporary    failures=2 -> next visit at 2026-11-10 10:01:00 (0:01:00, backoff)
 3. 2026-11-10 13:00:00 rate_limited failures=3 -> next visit at 2026-11-10 13:02:00 (0:02:00, server Retry-After)
 4. 2026-11-10 14:00:00 rate_limited failures=4 -> next visit at 2026-11-10 14:02:00 (0:02:00, server Retry-After)
 5. 2026-11-10 15:00:00 success      failures=0 -> available immediately (failure streak reset)
```

You can also preview a sequence without storing it:

```python
decisions = schedule.replay(url, observations)  # the schedule is not modified
```


## Pausing and manual recovery

Permanent failures (e.g. 410 Gone) pause on the first observation; transient
and unknown failures pause once their attempt limit is reached. A paused URL
is never returned as due, regardless of the time:

```python
schedule.record("https://example.org/gone", FetchOutcome(t0, status_code=410))
schedule.is_paused("https://example.org/gone")      # True
schedule.pause_reason("https://example.org/gone")   # 'permanent failure (status 410)'
schedule.get_paused_urls()                          # {'https://example.org/gone': '...'}
```

A human operator can reopen a paused or cooling-down URL. The failure streak
is left untouched; only the current block is cleared:

```python
schedule.resume("https://example.org/gone")                 # fetchable now
schedule.resume("https://example.org/gone", at=t0 + timedelta(hours=6))
```


## Success resets the history

Only a confirmed success (a 2xx status or 304) resets the failure streak,
clears the cooldown and lifts any pause. 404s, timeouts, rate limits and
unknown outcomes all keep the streak running:

```python
schedule.record(url, FetchOutcome(t0, status_code=500))
schedule.record(url, FetchOutcome(t0, status_code=200))
schedule.failure_count(url)   # 0
schedule.is_available(url, t0)  # True
```


## Cross-site plans are independent

State is keyed per normalized URL, so one host's failures never affect
another host's backoff or counters:

```python
schedule.record("https://a.com/1", FetchOutcome(t0, status_code=429))
schedule.record("https://a.com/1", FetchOutcome(t0, status_code=429))
schedule.failure_count("https://a.com/1")  # 2
schedule.failure_count("https://b.org/x")  # 0 — untouched

schedule.get_due_urls(t0 + timedelta(minutes=2))  # URLs fetchable at that time
```

URLs are normalized before keying (host case and trailing-slash variants of
the same address share a plan); pass a custom `url_normalizer` to change this.
A schedule is picklable, so it can be checkpointed alongside a `UrlStore`.
