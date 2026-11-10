"""
Unit tests for optional crawl-trap monitoring (courlan.traps).
"""

import pytest

from courlan import (
    CrawlTrapMonitor,
    FetchResult,
    TrapMode,
    TrapSignal,
    detect_traps,
    fingerprint_url,
    is_duplicate_fingerprint,
    trap_prefix,
)

# --------------------------------------------------------------------------- #
# helpers and fixtures
# --------------------------------------------------------------------------- #


def _result(
    url: str, status: int, body: str, links: tuple[str, ...] = ()
) -> FetchResult:
    response = fingerprint_url(url, status, body)
    return FetchResult.from_fingerprint(response, links)


def _calendar_sample() -> list[FetchResult]:
    """Finite sample of a calendar + filter loop: a few cycling date values
    recombine into many addresses that all serve the same placeholder page."""
    base = "https://calendar.example.org/events"
    results = []
    for year in (2023, 2024):
        for month in range(1, 7):
            url = f"{base}?year={year}&month={month:02d}"
            # the widget page is identical apart from the address
            results.append(_result(url, 200, "<html>calendar widget</html>"))
    return results


def _calendar_discoveries() -> list[tuple[str, list[str]]]:
    base = "https://calendar.example.org/events"
    links = [
        f"{base}?year={year}&month={month:02d}"
        for year in (2023, 2024)
        for month in range(1, 7)
    ]
    # the homepage exposes the calendar's recombined addresses
    return [("https://calendar.example.org/", links)]


def _finite_pagination_sample() -> list[FetchResult]:
    """Real, finite pagination: ten numbered pages, each with distinct content."""
    return [
        _result(
            f"https://blog.example.org/articles/page/{page}",
            200,
            f"<html>distinct article listing number {page}</html>",
        )
        for page in range(1, 11)
    ]


@pytest.fixture
def monitor() -> CrawlTrapMonitor:
    return CrawlTrapMonitor()


# --------------------------------------------------------------------------- #
# small building blocks
# --------------------------------------------------------------------------- #


def test_trap_prefix_folds_numbers():
    assert trap_prefix("https://example.org/calendar/2024/05/12") == (
        "https://example.org/calendar/"
    )
    assert trap_prefix("https://example.org/articles/page/10") == (
        "https://example.org/articles/page/"
    )
    # non-numeric sections are kept, fragments never split a group
    assert trap_prefix("https://example.org/events?year=2024#x") == (
        "https://example.org/events/"
    )
    assert trap_prefix("not a url") is None


def test_fingerprint_equality():
    first = fingerprint_url("https://a.org/x", 200, "same body")
    second = fingerprint_url("https://a.org/y", 200, "same body")
    third = fingerprint_url("https://a.org/z", 404, "same body")
    assert is_duplicate_fingerprint(first, second)
    assert not is_duplicate_fingerprint(first, third)
    near_a = fingerprint_url("https://a.org/a", 200, "x" * 1000)
    near_b = fingerprint_url("https://a.org/b", 200, "x" * 1010)
    assert is_duplicate_fingerprint(near_a, near_b, length_tolerance=0.02)
    far_b = fingerprint_url("https://a.org/c", 200, "x" * 2000)
    assert not is_duplicate_fingerprint(near_a, far_b, length_tolerance=0.02)


def test_discovery_tracks_unknown_urls(monitor):
    monitor.register_discovery(
        "https://example.org/", ["https://example.org/events?year=2024"]
    )
    # both the source page and the merely discovered URL are tracked
    assert "https://example.org/events/" in monitor.known_prefixes()
    assert "https://example.org/" in monitor.known_prefixes()
    # nothing fetched yet: no suggestion
    assert monitor.evaluate() == []
    evidence = monitor.evidence_for("https://example.org/events/")
    assert evidence and evidence[0].signal is TrapSignal.INSUFFICIENT_DATA


# --------------------------------------------------------------------------- #
# controlled comparison: calendar loop vs. finite pagination
# --------------------------------------------------------------------------- #


def test_calendar_loop_is_flagged_with_evidence(monitor):
    for source, links in _calendar_discoveries():
        monitor.register_discovery(source, links)
    for result in _calendar_sample():
        monitor.observe(result)

    suggestions = monitor.evaluate()
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    prefix = "https://calendar.example.org/events/"
    assert suggestion.prefix == prefix
    assert suggestion.observed_urls == 12
    assert suggestion.duplicate_ratio == 1.0

    signals = {item.signal for item in suggestion.evidence}
    assert TrapSignal.CALENDAR_CYCLE in signals
    assert TrapSignal.DUPLICATE_FINGERPRINTS in signals
    # every claim carries concrete sample URLs and metrics
    evidence = next(
        item for item in suggestion.evidence if item.signal is TrapSignal.CALENDAR_CYCLE
    )
    assert len(evidence.sample_urls) >= 2
    assert evidence.metrics["calendar_param_keys"] >= 1
    duplicate = next(
        item
        for item in suggestion.evidence
        if item.signal is TrapSignal.DUPLICATE_FINGERPRINTS
    )
    assert duplicate.metrics["duplicate_ratio"] == 1.0
    assert duplicate.metrics["unique_fingerprints"] == 1
    # observation mode suggests but never limits
    assert (
        monitor.is_limited("https://calendar.example.org/events?year=2024&month=01")
        is False
    )


def test_finite_pagination_is_not_flagged(monitor):
    for result in _finite_pagination_sample():
        monitor.observe(result)
    assert monitor.evaluate() == []

    prefix = "https://blog.example.org/articles/page/"
    signals = {item.signal for item in monitor.evidence_for(prefix)}
    # the monitor explains why the directory is safe
    assert TrapSignal.FINITE_PAGINATION in signals
    assert TrapSignal.DISTINCT_FINGERPRINTS in signals

    metrics = monitor.stats(prefix)
    assert metrics["unique_fingerprints"] == 10
    assert metrics["duplicate_ratio"] == 0.0
    assert metrics["page_number_min"] == 1
    assert metrics["page_number_max"] == 10


def test_many_page_numbers_alone_never_trigger():
    "A large directory with gaps in page numbering but distinct content stays eligible."
    monitor = CrawlTrapMonitor(min_sample_size=8)
    for page in [1, 2, 3, 4, 5, 6, 7, 8, 100, 250, 500, 999]:
        monitor.observe(
            _result(
                f"https://shop.example.org/category/page/{page}",
                200,
                f"<html>genuinely different listing {page}</html>",
            )
        )
    assert monitor.evaluate() == []
    prefix = "https://shop.example.org/category/page/"
    signals = {item.signal for item in monitor.evidence_for(prefix)}
    assert TrapSignal.GROWING_PAGE_NUMBERS not in signals
    assert TrapSignal.FINITE_PAGINATION not in signals
    assert TrapSignal.DISTINCT_FINGERPRINTS in signals


def test_runaway_pages_need_content_evidence():
    "Runaway page numbers corroborate duplication but cannot trigger alone."
    monitor = CrawlTrapMonitor()
    # distinct bodies, wide page span: no trap signal at all
    for page in [1, 2, 3, 4, 5, 6, 7, 8, 500, 900]:
        monitor.observe(
            _result(
                f"https://a.example.org/p/page/{page}",
                200,
                f"distinct body {page}",
            )
        )
    assert monitor.evaluate() == []


# --------------------------------------------------------------------------- #
# modes and rule lifecycle
# --------------------------------------------------------------------------- #


def test_observe_then_enforce_and_recovery():
    monitor = CrawlTrapMonitor()
    for result in _calendar_sample():
        monitor.observe(result)
    suggestion = monitor.evaluate()[0]

    # accepting a suggestion in observe mode still limits nothing
    rule = monitor.accept_suggestion(suggestion)
    assert rule.enabled
    assert monitor.is_limited(suggestion.evidence[0].sample_urls[0]) is False
    eligible, limited = monitor.filter_limited(
        ["https://calendar.example.org/events?year=2024&month=03"]
    )
    assert limited == [] and len(eligible) == 1

    # enforcement activates the accepted rule
    monitor.mode = TrapMode.ENFORCE
    target = "https://calendar.example.org/events?year=2024&month=03"
    assert monitor.is_limited(target)
    eligible, limited = monitor.filter_limited(
        [target, "https://calendar.example.org/other/page"]
    )
    assert eligible == ["https://calendar.example.org/other/page"]
    assert limited == [target]

    # disabling the rule restores candidates without deleting evidence
    monitor.disable_rule(rule.pattern)
    assert monitor.is_limited(target) is False
    assert monitor.rules(include_disabled=True)[0].enabled is False
    assert monitor.evaluate()[0].prefix == suggestion.prefix

    # re-enabling restricts again; removing lets candidates recover for good
    monitor.enable_rule(rule.pattern)
    assert monitor.is_limited(target)
    monitor.remove_rule(rule.pattern)
    assert monitor.is_limited(target) is False
    assert monitor.rules(include_disabled=True) == []
    # observations survive, so a fresh suggestion can be produced
    assert monitor.evaluate()[0].duplicate_ratio == 1.0


def test_enforce_auto_rules():
    monitor = CrawlTrapMonitor(mode=TrapMode.ENFORCE, auto_rules=True)
    for result in _calendar_sample():
        monitor.observe(result)
    suggestion = monitor.evaluate()[0]
    assert suggestion.rule.enabled
    assert monitor.is_limited("https://calendar.example.org/events?year=2023&month=02")
    # dismissal only hides the suggestion, the installed rule stays
    monitor.dismiss_suggestion(suggestion.prefix)
    assert monitor.suggestions() == []
    assert monitor.rules() and monitor.is_limited(
        "https://calendar.example.org/events?year=2023&month=02"
    )


def test_dismiss_does_not_touch_rules(monitor):
    for result in _calendar_sample():
        monitor.observe(result)
    suggestion = monitor.evaluate()[0]
    rule = monitor.accept_suggestion(suggestion)
    monitor.dismiss_suggestion(suggestion.prefix)
    assert monitor.rules(include_disabled=True) == [rule]


def test_disable_unknown_pattern_is_safe(monitor):
    # no rule exists yet for the pattern; disabling must not raise
    monitor.disable_rule("^https://example.org/events(?:/|$|[?#])")
    disabled = monitor.rules(include_disabled=True)
    assert len(disabled) == 1 and disabled[0].enabled is False


def test_progressive_evaluation_waits_for_enough_fetches(monitor):
    results = _calendar_sample()
    for result in results[:4]:
        monitor.observe(result)
    # below the minimum sample: no suggestion yet, but the reason is recorded
    assert monitor.evaluate() == []
    evidence = monitor.evidence_for("https://calendar.example.org/events/")
    assert any(item.signal is TrapSignal.INSUFFICIENT_DATA for item in evidence)
    for result in results[4:]:
        monitor.observe(result)
    assert len(monitor.evaluate()) == 1


def test_near_duplicate_lengths_are_evidence_not_hash_equality(monitor):
    short = fingerprint_url("https://example.org/a", 200, "x" * 1000)
    longer = fingerprint_url("https://example.org/b", 200, "x" * 1005)
    # different hashes, but within the length tolerance they are near-duplicates
    assert short.body_hash != longer.body_hash
    assert is_duplicate_fingerprint(short, longer, length_tolerance=0.02)


# --------------------------------------------------------------------------- #
# other trap shapes
# --------------------------------------------------------------------------- #


def test_cartesian_filter_explosion():
    monitor = CrawlTrapMonitor()
    urls = [
        f"https://shop.example.org/catalog?color={color}&size={size}&sort={sort}"
        for color in ("red", "blue")
        for size in ("s", "m", "l")
        for sort in ("asc", "desc")
    ]
    for url in urls:
        monitor.observe(_result(url, 200, "<html>same filtered listing</html>"))
    suggestion = monitor.evaluate()[0]
    signals = {item.signal for item in suggestion.evidence}
    assert TrapSignal.CARTESIAN_EXPLOSION in signals
    assert suggestion.observed_urls == 12


def test_soft_404_pattern():
    monitor = CrawlTrapMonitor()
    for tag in range(10):
        monitor.observe(
            _result(
                f"https://example.org/tags/{tag}",
                404,
                "<html>not found</html>",
            )
        )
    suggestion = monitor.evaluate()[0]
    assert TrapSignal.SOFT_404_PATTERN in {item.signal for item in suggestion.evidence}


def test_one_shot_detect_traps_helper():
    suggestions = detect_traps(_calendar_sample())
    assert len(suggestions) == 1
    assert suggestions[0].duplicate_ratio == 1.0
    # the finite control sample never produces a suggestion
    assert detect_traps(_finite_pagination_sample()) == []


def test_groups_stay_separate(monitor):
    for result in [*_calendar_sample(), *_finite_pagination_sample()]:
        monitor.observe(result)
    suggestions = monitor.evaluate()
    assert len(suggestions) == 1
    assert suggestions[0].prefix == "https://calendar.example.org/events/"
    assert len(monitor.known_prefixes()) == 2


def test_reset_clears_everything(monitor):
    for result in _calendar_sample():
        monitor.observe(result)
    monitor.evaluate()
    monitor.reset()
    assert monitor.known_prefixes() == []
    assert monitor.suggestions() == []
    assert monitor.rules(include_disabled=True) == []
