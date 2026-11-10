"""
Optional crawl-trap monitoring.

Calendar widgets, layered filter combinations and soft-404 endpoints can make
a site expose an unbounded number of distinct URLs while serving a handful of
distinct responses. Ordinary URL deduplication cannot catch this because the
URLs really are different. The monitors in this module combine three kinds of
caller-supplied knowledge to spot the resulting address inflation:

- discovery relationships (which URLs link to which others),
- response fingerprints (status code, body hash, text length),
- and access results (what was actually fetched).

Detection never blocks anything on its own. It produces evidence-backed
:class:`TrapSuggestion` objects that the caller may turn into an enabled
:class:`TrapRule`. A large but finite directory with real pagination and
distinct pages is never flagged solely for having many page numbers, and a
rule can be disabled or removed at any time, after which the candidate URLs
become eligible again.
"""

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from threading import RLock
from urllib.parse import parse_qsl, urlsplit

LOGGER = logging.getLogger(__name__)

# query keys that usually drive a calendar or date navigation widget
_CALENDAR_KEY_RE = re.compile(
    r"^(?:year|yr|yyyy|y|month|mon|mm|m|day|dd|d|date|dt|cal|calendar|yymm|yyyymm)$",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
# page numbers carried by common pagination markers
_PAGE_MARKER_RE = re.compile(
    r"(?:[?&](?:page|pagenum|p|paged|seite|start)=|/(?:page|p|paged|seite)/)(\d+)",
    re.IGNORECASE,
)
# generic numeric segment, e.g. /news/42 or /2024/05/
_NUMERIC_RE = re.compile(r"^\d{1,4}$")


class TrapMode(Enum):
    "Operational mode of a monitor."

    OBSERVE = "observe"
    ENFORCE = "enforce"


class TrapSignal(Enum):
    "Kind of evidence collected for a candidate trap."

    CALENDAR_CYCLE = "calendar-cycle"
    CARTESIAN_EXPLOSION = "cartesian-explosion"
    DUPLICATE_FINGERPRINTS = "duplicate-fingerprints"
    SOFT_404_PATTERN = "soft-404-pattern"
    GROWING_PAGE_NUMBERS = "growing-page-numbers"
    # informational, protective signals: they explain why a group is not a trap
    FINITE_PAGINATION = "finite-pagination"
    DISTINCT_FINGERPRINTS = "distinct-fingerprints"
    INSUFFICIENT_DATA = "insufficient-data"


@dataclass(frozen=True)
class ResponseFingerprint:
    """Fingerprint of a fetched response.

    Args:
        url: URL the response was fetched from.
        status: HTTP status code.
        body_hash: stable digest of the response body (see :func:`fingerprint_url`).
        text_length: response size in characters or bytes, if known.
        content_type: response content type, if known.
    """

    url: str
    status: int
    body_hash: str
    text_length: int | None = None
    content_type: str | None = None


@dataclass(frozen=True)
class FetchResult:
    """Access result recorded by the caller after fetching a URL.

    Discovery links are the URLs found on the page, i.e. the caller's view of
    the discovery relationships between addresses.
    """

    url: str
    status: int
    fingerprint: str | None = None
    text_length: int | None = None
    content_type: str | None = None
    discovery_links: tuple[str, ...] = ()

    @classmethod
    def from_fingerprint(
        cls,
        response_fingerprint: ResponseFingerprint,
        discovery_links: tuple[str, ...] = (),
    ) -> "FetchResult":
        "Build a fetch result from a full response fingerprint."
        return cls(
            url=response_fingerprint.url,
            status=response_fingerprint.status,
            fingerprint=response_fingerprint.body_hash,
            text_length=response_fingerprint.text_length,
            content_type=response_fingerprint.content_type,
            discovery_links=tuple(discovery_links),
        )


@dataclass(frozen=True)
class TrapEvidence:
    """One piece of evidence backing a trap decision.

    Args:
        signal: nature of the observation.
        detail: human-readable explanation of the measurement.
        sample_urls: concrete URLs the measurement is based on.
        metrics: machine-readable measurements (ratios, counts, spans).
    """

    signal: TrapSignal
    detail: str
    sample_urls: tuple[str, ...]
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TrapRule:
    """Optional restriction rule, derived from a suggestion or supplied directly.

    A rule only matches while it is enabled, so disabling it restores every
    candidate URL without deleting the accumulated evidence.
    """

    pattern: str
    reason: str
    enabled: bool = True

    def matches(self, url: str) -> bool:
        "Tell whether the rule currently applies to a URL."
        return self.enabled and re.search(self.pattern, url) is not None


@dataclass(frozen=True)
class TrapSuggestion:
    """Evidence-backed restriction proposal.

    It is a recommendation only: nothing is limited until the caller accepts
    it (or runs the monitor in enforcing mode with automatic rules).
    """

    prefix: str
    rule: TrapRule
    evidence: tuple[TrapEvidence, ...]
    duplicate_ratio: float
    observed_urls: int
    created_at: datetime

    def matches(self, url: str) -> bool:
        "Tell whether an accepted suggestion would currently cover a URL."
        return (
            self.rule.enabled and url.startswith(self.prefix) and self.rule.matches(url)
        )


@dataclass
class _Entry:
    "Everything known about a single canonical URL within a prefix group."

    url: str
    fetch: FetchResult | None = None
    discoveries: tuple[str, ...] = ()


def fingerprint_url(
    url: str, status: int, body: bytes | str, content_type: str | None = None
) -> ResponseFingerprint:
    "Compute a response fingerprint from a URL, status and body."
    if isinstance(body, str):
        body = body.encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    return ResponseFingerprint(
        url=url,
        status=status,
        body_hash=digest,
        text_length=len(body),
        content_type=content_type,
    )


def is_duplicate_fingerprint(
    first: ResponseFingerprint,
    second: ResponseFingerprint,
    length_tolerance: float = 0.02,
) -> bool:
    """Tell whether two fingerprints plausibly describe the same response.

    Status codes must agree. Equal body hashes then always match; otherwise the
    text lengths, when both are known, must stay within ``length_tolerance`` of
    each other, which covers boilerplate-only differences.
    """
    if first.status != second.status:
        return False
    if first.body_hash == second.body_hash:
        return True
    if first.text_length is None or second.text_length is None:
        return False
    if first.text_length == 0 or second.text_length == 0:
        return first.text_length == second.text_length
    difference = abs(first.text_length - second.text_length) / max(
        first.text_length, second.text_length
    )
    return difference <= length_tolerance


def _canonical_key(url: str) -> str | None:
    "Normalize a URL for grouping: fragments never create distinct content."
    try:
        return urlsplit(url)._replace(fragment="").geturl()
    except ValueError:
        LOGGER.debug("trap monitor: unparseable URL discarded: %s", url)
        return None


def trap_prefix(url: str) -> str | None:
    """Return the address-space prefix a URL belongs to.

    Trailing numeric segments (page, article or date numbers) are folded into
    the prefix, so ``/calendar/2024/05/page/8`` and ``/calendar/2024/06/page/9``
    are evaluated as one group instead of looking like separate directories.
    """
    key = _canonical_key(url)
    if key is None:
        return None
    parsed = urlsplit(key)
    if not parsed.scheme or not parsed.netloc:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    # fold a trailing run of at most three numeric segments (day/month/year,
    # page or article numbers), never a non-numeric section name
    folded = 0
    while folded < 3 and segments and segments[-1].isdigit() and len(segments[-1]) <= 4:
        segments.pop()
        folded += 1
    prefix_path = "/" + "/".join(segments)
    if not prefix_path.endswith("/"):
        prefix_path += "/"
    return f"{parsed.scheme}://{parsed.netloc}{prefix_path}"


def _page_numbers(url: str) -> list[int]:
    "Extract pagination numbers carried by markers or a trailing numeric segment."
    numbers = [int(match) for match in _PAGE_MARKER_RE.findall(url)]
    if not numbers:
        segments = urlsplit(url).path.rstrip("/").split("/")
        if segments and _NUMERIC_RE.match(segments[-1]):
            numbers = [int(segments[-1])]
    return numbers


def _calendar_path(url: str) -> list[tuple[str, str]]:
    "Find year/month/day-like path segments, in order."
    found: list[tuple[str, str]] = []
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    for segment in segments:
        if _YEAR_RE.match(segment):
            found.append(("year", segment))
        elif (
            found
            and found[-1][0] == "year"
            and segment.isdigit()
            and 1 <= int(segment) <= 12
        ):
            found.append(("month", segment))
        elif (
            found
            and found[-1][0] == "month"
            and segment.isdigit()
            and 1 <= int(segment) <= 31
        ):
            found.append(("day", segment))
    return found


class CrawlTrapMonitor:
    """Collect crawl observations and emit evidence-backed trap suggestions.

    Args:
        mode: ``OBSERVE`` (default) only records evidence and suggestions;
            ``ENFORCE`` makes accepted rules actually limit URLs.
        min_sample_size: minimum number of fetched URLs in a group before any
            suggestion can be produced.
        min_observations: minimum number of known URLs (fetched or merely
            discovered) before discovery-only signals are considered.
        duplicate_threshold: duplicate-fingerprint ratio above which the
            duplicated content is itself treated as strong evidence.
        similarity_threshold: distinct-content ratio at or above which a
            paginated group is considered finite and therefore safe.
        length_tolerance: relative length difference tolerated by
            :func:`is_duplicate_fingerprint`.
        auto_rules: in enforcing mode, accept suggestions as enabled rules
            during :meth:`evaluate` without waiting for :meth:`accept_suggestion`.
        max_suggestions: safety cap on retained suggestions, oldest first.

    Thread-safety: public methods are atomic under one internal lock;
    sequences of calls are not, mirroring :class:`~courlan.urlstore.UrlStore`.
    """

    def __init__(
        self,
        mode: TrapMode = TrapMode.OBSERVE,
        *,
        min_sample_size: int = 8,
        min_observations: int = 8,
        duplicate_threshold: float = 0.8,
        similarity_threshold: float = 0.9,
        length_tolerance: float = 0.02,
        auto_rules: bool = False,
        max_suggestions: int = 128,
    ) -> None:
        self.mode = mode
        self.min_sample_size = min_sample_size
        self.min_observations = min_observations
        self.duplicate_threshold = duplicate_threshold
        self.similarity_threshold = similarity_threshold
        self.length_tolerance = length_tolerance
        self.auto_rules = auto_rules
        self.max_suggestions = max_suggestions
        # prefix -> canonical URL key -> entry
        self._groups: dict[str, dict[str, _Entry]] = {}
        self._rules: dict[str, TrapRule] = {}
        self._suggestions: dict[str, TrapSuggestion] = {}
        self._lock = RLock()

    # ----- recording -------------------------------------------------------

    def _group_for(self, url: str) -> tuple[str, str] | None:
        prefix = trap_prefix(url)
        if prefix is None:
            return None
        key = _canonical_key(url)
        if key is None:
            return None
        return prefix, key

    def _record(self, url: str) -> tuple[str, str, _Entry] | None:
        grouped = self._group_for(url)
        if grouped is None:
            return None
        prefix, key = grouped
        entries = self._groups.setdefault(prefix, {})
        entry = entries.get(key)
        if entry is None:
            entry = _Entry(url=key)
            entries[key] = entry
        return prefix, key, entry

    def register_discovery(
        self, source_url: str, discovered_urls: list[str] | tuple[str, ...]
    ) -> None:
        """Record a discovery relationship: ``source_url`` links to each
        discovered URL. The discovered addresses are tracked even before they
        are fetched, which is how an expanding calendar surface is noticed."""
        with self._lock:
            parent = self._record(source_url)
            if parent is not None:
                parent[2].discoveries = tuple(
                    dict.fromkeys((*parent[2].discoveries, *discovered_urls))
                )
            for discovered in discovered_urls:
                self._record(discovered)

    def observe_fingerprint(
        self,
        response_fingerprint: ResponseFingerprint,
        discovery_links: tuple[str, ...] = (),
    ) -> None:
        "Record a fetched response, optionally with the links it exposed."
        with self._lock:
            recorded = self._record(response_fingerprint.url)
            if recorded is None:
                return
            entry = recorded[2]
            entry.fetch = FetchResult.from_fingerprint(
                response_fingerprint, discovery_links
            )
            if discovery_links:
                entry.discoveries = tuple(
                    dict.fromkeys((*entry.discoveries, *discovery_links))
                )
                for discovered in discovery_links:
                    self._record(discovered)

    def observe(self, result: FetchResult) -> None:
        "Record a fetch result (status, fingerprint and discovery links)."
        with self._lock:
            recorded = self._record(result.url)
            if recorded is None:
                return
            entry = recorded[2]
            entry.fetch = result
            if result.discovery_links:
                entry.discoveries = tuple(
                    dict.fromkeys((*entry.discoveries, *result.discovery_links))
                )
                for discovered in result.discovery_links:
                    self._record(discovered)

    # ----- evidence collection --------------------------------------------

    def _discovery_signals(
        self, entries: dict[str, _Entry]
    ) -> tuple[list[TrapEvidence], dict[str, float]]:
        urls = sorted(entries)
        metrics: dict[str, float] = {}
        signals: list[TrapEvidence] = []

        # calendar parameters in query strings and calendar-like path segments
        calendar_values: dict[str, set[str]] = defaultdict(set)
        path_dates = 0
        for url in urls:
            for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
                if _CALENDAR_KEY_RE.match(key) and value:
                    calendar_values[key].add(value)
            path_dates = max(path_dates, len(_calendar_path(url)))
        cycling_keys = {
            key: values for key, values in calendar_values.items() if len(values) >= 2
        }
        if (
            path_dates >= 2
            or len(cycling_keys) >= 2
            or any(len(values) >= 4 for values in calendar_values.values())
        ):
            sample = urls[: min(6, len(urls))]
            metrics["calendar_param_keys"] = float(len(cycling_keys))
            metrics["calendar_path_depth"] = float(path_dates)
            detail = (
                f"{len(cycling_keys)} cycling calendar parameter(s) and "
                f"{path_dates} date-like path segment(s) generate recombined URLs"
            )
            signals.append(
                TrapEvidence(
                    TrapSignal.CALENDAR_CYCLE, detail, tuple(sample), dict(metrics)
                )
            )

        # cartesian combination of non-calendar filter parameters
        varying: dict[str, set[str]] = defaultdict(set)
        combinations: set[tuple[tuple[str, str], ...]] = set()
        for url in urls:
            pairs = tuple(
                sorted(
                    (key, value)
                    for key, value in parse_qsl(urlsplit(url).query)
                    if value and not _CALENDAR_KEY_RE.match(key)
                )
            )
            combinations.add(pairs)
            for key, value in pairs:
                varying[key].add(value)
        varying = {key: values for key, values in varying.items() if len(values) >= 2}
        if len(varying) >= 2 and len(combinations) >= max(4, round(0.75 * len(urls))):
            metrics["filter_keys"] = float(len(varying))
            metrics["parameter_combinations"] = float(len(combinations))
            detail = (
                f"{len(varying)} filter parameter(s) combine into "
                f"{len(combinations)} distinct address combinations"
            )
            signals.append(
                TrapEvidence(
                    TrapSignal.CARTESIAN_EXPLOSION,
                    detail,
                    tuple(urls[:6]),
                    dict(metrics),
                )
            )
        return signals, metrics

    def _fingerprint_signals(
        self, entries: dict[str, _Entry]
    ) -> tuple[list[TrapEvidence], list[TrapEvidence], dict[str, float], float]:
        fetched = [entry for entry in entries.values() if entry.fetch is not None]
        metrics: dict[str, float] = {}
        trap_signals: list[TrapEvidence] = []
        info_signals: list[TrapEvidence] = []
        if not fetched:
            return trap_signals, info_signals, metrics, 0.0

        # exact-hash clusters, status code included
        clusters: dict[tuple[int, str], list[str]] = defaultdict(list)
        for entry in fetched:
            fetch = entry.fetch
            assert fetch is not None
            if fetch.fingerprint is not None:
                clusters[(fetch.status, fetch.fingerprint)].append(entry.url)
        largest = max(clusters.values(), key=len) if clusters else []
        # share of fetches whose body repeats at least once
        repeated = sum(len(urls) for urls in clusters.values() if len(urls) >= 2)
        duplicate_ratio = round(repeated / len(fetched), 4)
        unique_ratio = len(clusters) / len(fetched)
        metrics["fetched_urls"] = float(len(fetched))
        metrics["unique_fingerprints"] = float(len(clusters))
        metrics["duplicate_ratio"] = round(duplicate_ratio, 4)
        sample = tuple(sorted(largest)[:6])

        if len(largest) >= 3 and duplicate_ratio >= 0.5:
            statuses = {
                entry.fetch.status for entry in fetched if entry.fetch is not None
            }
            detail = (
                f"{len(largest)} of {len(fetched)} fetched URLs "
                f"({duplicate_ratio:.0%}) return the same response body"
            )
            # repeated error pages are a soft-404; uniform 200 bodies are duplication
            signal = (
                TrapSignal.SOFT_404_PATTERN
                if statuses and all(status >= 400 for status in statuses)
                else TrapSignal.DUPLICATE_FINGERPRINTS
            )
            trap_signals.append(TrapEvidence(signal, detail, sample, dict(metrics)))
        elif duplicate_ratio >= self.duplicate_threshold:
            detail = (
                f"duplicate body ratio {duplicate_ratio:.0%} reaches the trap threshold"
            )
            trap_signals.append(
                TrapEvidence(
                    TrapSignal.DUPLICATE_FINGERPRINTS, detail, sample, dict(metrics)
                )
            )
        elif unique_ratio >= self.similarity_threshold:
            info_signals.append(
                TrapEvidence(
                    TrapSignal.DISTINCT_FINGERPRINTS,
                    f"{len(clusters)} distinct bodies across {len(fetched)} fetches",
                    tuple(sorted(entry.url for entry in fetched)[:6]),
                    dict(metrics),
                )
            )

        # near-duplicate lengths catch templated boilerplate the hash misses
        lengths = [
            entry.fetch.text_length
            for entry in fetched
            if entry.fetch is not None and entry.fetch.text_length is not None
        ]
        if lengths and max(lengths) - min(lengths) > 0:
            spread = (max(lengths) - min(lengths)) / max(lengths)
            metrics["length_spread"] = round(spread, 4)
        return trap_signals, info_signals, metrics, duplicate_ratio

    def _pagination_signal(
        self, entries: dict[str, _Entry], metrics: dict[str, float]
    ) -> TrapEvidence | None:
        "Return protective finite-pagination evidence, or a runaway-page signal."
        fetched = [entry for entry in entries.values() if entry.fetch is not None]
        numbered = [
            (entry.url, numbers)
            for entry in fetched
            if (numbers := _page_numbers(entry.url))
        ]
        if len(numbered) < self.min_sample_size:
            return None
        page_values = [number for _, numbers in numbered for number in numbers]
        lowest, highest = min(page_values), max(page_values)
        metrics["page_number_min"] = float(lowest)
        metrics["page_number_max"] = float(highest)
        metrics["page_number_span"] = float(highest - lowest + 1)
        distinct_ratio = metrics.get("unique_fingerprints", 0.0) / max(
            metrics.get("fetched_urls", 1.0), 1.0
        )
        sample = tuple(url for url, _ in numbered[:6])
        # finite directory: nearly one distinct body per page and no runaway numbers
        if (
            distinct_ratio >= self.similarity_threshold
            and (highest - lowest + 1) <= len(fetched) * 2
        ):
            return TrapEvidence(
                TrapSignal.FINITE_PAGINATION,
                (
                    f"pages {lowest}-{highest} each serve distinct content "
                    f"({distinct_ratio:.0%} unique fingerprints), a finite directory"
                ),
                sample,
                dict(metrics),
            )
        if (
            highest - lowest + 1 > len(fetched) * 2
            and distinct_ratio < self.similarity_threshold
        ):
            return TrapEvidence(
                TrapSignal.GROWING_PAGE_NUMBERS,
                (
                    f"page numbers span {highest - lowest + 1} positions for "
                    f"{len(fetched)} fetches but only {distinct_ratio:.0%} distinct bodies"
                ),
                sample,
                dict(metrics),
            )
        return None

    def analyze(
        self, prefix: str
    ) -> tuple[list[TrapEvidence], list[TrapEvidence], dict[str, float]]:
        """Recompute all evidence for one prefix.

        Returns trap-pointing evidence, protective (finite/distinct) evidence
        and the raw metrics, without creating or changing any suggestion."""
        with self._lock:
            entries = self._groups.get(prefix)
            if not entries:
                return [], [], {}
            discovery_signals, metrics = self._discovery_signals(entries)
            fingerprint_signals, info_signals, fingerprint_metrics, _ = (
                self._fingerprint_signals(entries)
            )
            metrics.update(fingerprint_metrics)
            pagination = self._pagination_signal(entries, metrics)
            if pagination is not None:
                if pagination.signal is TrapSignal.FINITE_PAGINATION:
                    info_signals.append(pagination)
                else:
                    fingerprint_signals.append(pagination)
            fetched = sum(1 for entry in entries.values() if entry.fetch is not None)
            if fetched < self.min_sample_size:
                info_signals.append(
                    TrapEvidence(
                        TrapSignal.INSUFFICIENT_DATA,
                        f"only {fetched} of {self.min_sample_size} required fetches available",
                        tuple(sorted(entries)[:6]),
                        {"fetched_urls": float(fetched)},
                    )
                )
            return (
                discovery_signals + fingerprint_signals,
                info_signals,
                metrics,
            )

    # ----- evaluation and rules -------------------------------------------

    def evaluate(self, prefix: str | None = None) -> list[TrapSuggestion]:
        """Evaluate one prefix or every known prefix and refresh suggestions.

        Suggestions are recommendations only. Nothing is limited unless a
        suggestion is accepted (or ``auto_rules`` is set in enforcing mode)."""
        with self._lock:
            targets = [prefix] if prefix is not None else sorted(self._groups)
            results: list[TrapSuggestion] = []
            for target in targets:
                suggestion = self._evaluate_one(target)
                if suggestion is not None:
                    self._suggestions[target] = suggestion
                    results.append(suggestion)
                else:
                    self._suggestions.pop(target, None)
            if len(self._suggestions) > self.max_suggestions:
                for stale in sorted(self._suggestions)[
                    : len(self._suggestions) - self.max_suggestions
                ]:
                    self._suggestions.pop(stale, None)
            return sorted(results, key=lambda item: item.duplicate_ratio, reverse=True)

    def _evaluate_one(self, prefix: str) -> TrapSuggestion | None:
        entries = self._groups.get(prefix)
        if not entries:
            return None
        trap_evidence, info_evidence, metrics = self.analyze(prefix)
        fetched = sum(1 for entry in entries.values() if entry.fetch is not None)
        if fetched < self.min_sample_size:
            return None
        # finite pagination and genuinely distinct directories are never traps
        if any(item.signal is TrapSignal.FINITE_PAGINATION for item in info_evidence):
            return None
        signals = {item.signal for item in trap_evidence}
        discovery_signals = signals & {
            TrapSignal.CALENDAR_CYCLE,
            TrapSignal.CARTESIAN_EXPLOSION,
        }
        # growing page numbers are only corroborating evidence: many page
        # numbers by themselves never justify restricting a normal directory
        response_signals = signals & {
            TrapSignal.DUPLICATE_FINGERPRINTS,
            TrapSignal.SOFT_404_PATTERN,
            TrapSignal.GROWING_PAGE_NUMBERS,
        }
        content_signals = response_signals - {TrapSignal.GROWING_PAGE_NUMBERS}
        duplicate_ratio = metrics.get("duplicate_ratio", 0.0)
        strong_duplication = (
            bool(content_signals) and duplicate_ratio >= self.duplicate_threshold
        )
        # a suggestion needs corroboration across knowledge sources:
        # discovery + response evidence, or near-total content duplication
        if not (discovery_signals and response_signals) and not strong_duplication:
            return None
        if not discovery_signals and len(entries) < self.min_observations:
            return None
        # anchor at the start; cover both "/events/..." and "/events?..." so
        # a query string does not let a limited URL slip through
        pattern = "^" + re.escape(prefix.rstrip("/")) + r"(?:/|$|[?#])"
        reason = "; ".join(item.detail for item in trap_evidence) or (
            f"duplicate response ratio {duplicate_ratio:.0%}"
        )
        existing = self._rules.get(pattern)
        if existing is not None:
            rule = existing
        else:
            rule = TrapRule(pattern=pattern, reason=reason)
            # observation mode never installs rules; enforcement only does so
            # automatically when the caller opted into auto_rules
            if self.mode is TrapMode.ENFORCE and self.auto_rules:
                self._rules[pattern] = rule
        return TrapSuggestion(
            prefix=prefix,
            rule=rule,
            evidence=tuple(trap_evidence),
            duplicate_ratio=duplicate_ratio,
            observed_urls=len(entries),
            created_at=datetime.now(),
        )

    def suggestions(self) -> list[TrapSuggestion]:
        "Return the current suggestions, most duplicated content first."
        with self._lock:
            return sorted(
                self._suggestions.values(),
                key=lambda item: item.duplicate_ratio,
                reverse=True,
            )

    def evidence_for(self, prefix: str) -> list[TrapEvidence]:
        "Return both trap-pointing and protective evidence collected for a prefix."
        trap_evidence, info_evidence, _ = self.analyze(prefix)
        return trap_evidence + info_evidence

    def accept_suggestion(self, suggestion: TrapSuggestion) -> TrapRule:
        """Turn a suggestion into an enabled restriction rule and return it."""
        with self._lock:
            rule = TrapRule(
                pattern=suggestion.rule.pattern,
                reason=suggestion.rule.reason,
                enabled=True,
            )
            self._rules[rule.pattern] = rule
            stored = self._suggestions.get(suggestion.prefix)
            if stored is not None:
                self._suggestions[suggestion.prefix] = TrapSuggestion(
                    prefix=stored.prefix,
                    rule=rule,
                    evidence=stored.evidence,
                    duplicate_ratio=stored.duplicate_ratio,
                    observed_urls=stored.observed_urls,
                    created_at=stored.created_at,
                )
            return rule

    def dismiss_suggestion(self, prefix: str) -> None:
        "Drop a suggestion without touching any rule."
        with self._lock:
            self._suggestions.pop(prefix, None)

    def enable_rule(self, pattern: str, reason: str = "manually enabled") -> TrapRule:
        "Add or re-enable a rule, e.g. after a previous false-positive review."
        with self._lock:
            rule = TrapRule(pattern=pattern, reason=reason, enabled=True)
            self._rules[pattern] = rule
            return rule

    def disable_rule(self, pattern: str) -> None:
        """Disable a rule while keeping it and its evidence.

        Candidate URLs immediately become eligible again: the restriction is
        lifted, the observations are not deleted."""
        with self._lock:
            rule = self._rules.get(pattern)
            if rule is not None:
                disabled = TrapRule(
                    pattern=rule.pattern, reason=rule.reason, enabled=False
                )
            else:
                disabled = TrapRule(
                    pattern=pattern, reason="manually disabled", enabled=False
                )
            self._rules[disabled.pattern] = disabled
            for target, suggestion in list(self._suggestions.items()):
                if suggestion.rule.pattern == pattern and suggestion.rule.enabled:
                    self._suggestions[target] = TrapSuggestion(
                        prefix=suggestion.prefix,
                        rule=disabled,
                        evidence=suggestion.evidence,
                        duplicate_ratio=suggestion.duplicate_ratio,
                        observed_urls=suggestion.observed_urls,
                        created_at=suggestion.created_at,
                    )

    def remove_rule(self, pattern: str) -> None:
        "Delete a rule altogether; its candidates recover and can be re-evaluated."
        with self._lock:
            self._rules.pop(pattern, None)

    def rules(self, include_disabled: bool = False) -> list[TrapRule]:
        "Return the known rules, enabled ones first."
        with self._lock:
            known = list(self._rules.values())
            if not include_disabled:
                known = [rule for rule in known if rule.enabled]
            return sorted(known, key=lambda rule: (not rule.enabled, rule.pattern))

    def is_limited(self, url: str) -> bool:
        """Tell whether a URL is currently covered by an enabled rule.

        Observation mode never limits anything, and disabled or removed rules
        let every candidate URL through."""
        if self.mode is not TrapMode.ENFORCE:
            return False
        with self._lock:
            return any(
                rule.matches(url) for rule in self._rules.values() if rule.enabled
            )

    def filter_limited(self, urls: list[str]) -> tuple[list[str], list[str]]:
        "Split URLs into eligible ones and ones currently limited, in input order."
        eligible: list[str] = []
        limited: list[str] = []
        for url in urls:
            (limited if self.is_limited(url) else eligible).append(url)
        return eligible, limited

    def stats(self, prefix: str) -> dict[str, float]:
        "Return the raw measurements behind a prefix, useful for dashboards."
        _, _, metrics = self.analyze(prefix)
        return dict(metrics)

    def known_prefixes(self) -> list[str]:
        "Return the address-space prefixes currently monitored."
        with self._lock:
            return sorted(self._groups)

    def reset(self) -> None:
        "Drop every observation, suggestion and rule."
        with self._lock:
            self._groups.clear()
            self._rules.clear()
            self._suggestions.clear()


def detect_traps(
    fetch_results: list[FetchResult],
    *,
    discoveries: list[tuple[str, list[str]]] | None = None,
    mode: TrapMode = TrapMode.OBSERVE,
    **kwargs: object,
) -> list[TrapSuggestion]:
    """One-shot helper: feed a finite sample of fetch results and get suggestions.

    Args:
        fetch_results: finite sample of access results (both groups needed for
            a controlled comparison).
        discoveries: optional ``(source_url, discovered_urls)`` relationships.
        mode: monitor mode, observation by default.
        **kwargs: extra :class:`CrawlTrapMonitor` tuning arguments.
    """
    monitor = CrawlTrapMonitor(mode=mode, **kwargs)  # type: ignore[arg-type]
    for source_url, discovered in discoveries or []:
        monitor.register_discovery(source_url, discovered)
    for result in fetch_results:
        monitor.observe(result)
    return monitor.evaluate()
