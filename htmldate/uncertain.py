# pylint:disable-msg=E0611,I1101
"""
Uncertain date mode: candidate-based parsing of ambiguous numeric dates.

Cross-border suppliers write dates like 03/04/2025 where assuming a
month-first (or day-first) order silently shifts the data. Instead of
committing to one reading, this module derives the admissible day/month
orders from the caller-allowed regions, the page language and explicit
month names in the text, and keeps every candidate that survives. It
never resolves conflicting context by majority vote: when the evidence
does not pin down a single date, the candidate set is returned as-is.

Invalid dates, candidates excluded by the business date range, and
remaining ambiguity are reported separately. The existing find_date()
interface is untouched; this is an additional, opt-in entry point.
"""

import logging
import re

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .extractors import regex_parse
from .utils import trim_text
from .validators import correct_year, get_max_date, get_min_date, is_valid_date

LOGGER = logging.getLogger(__name__)

ALL_ORDERS = frozenset({"DMY", "MDY", "YMD"})

# region (ISO 3166-1 alpha-2) → admissible numeric date orders
# unlisted regions carry no constraint rather than a guess
REGION_ORDERS: dict[str, frozenset[str]] = {
    # month-first
    "US": frozenset({"MDY"}),
    "FM": frozenset({"MDY"}),
    "MH": frozenset({"MDY"}),
    "PW": frozenset({"MDY"}),
    "PH": frozenset({"MDY"}),
    # year-first
    "BT": frozenset({"YMD"}),
    "CN": frozenset({"YMD"}),
    "HU": frozenset({"YMD"}),
    "JP": frozenset({"YMD"}),
    "KP": frozenset({"YMD"}),
    "KR": frozenset({"YMD"}),
    "LT": frozenset({"YMD"}),
    "MN": frozenset({"YMD"}),
    "SE": frozenset({"YMD"}),
    "TW": frozenset({"YMD"}),
    # all three orders are in official use
    "CA": ALL_ORDERS,
    # day-first
    "AT": frozenset({"DMY"}),
    "AU": frozenset({"DMY"}),
    "BE": frozenset({"DMY"}),
    "BR": frozenset({"DMY"}),
    "CH": frozenset({"DMY"}),
    "CZ": frozenset({"DMY"}),
    "DE": frozenset({"DMY"}),
    "DK": frozenset({"DMY"}),
    "ES": frozenset({"DMY"}),
    "FI": frozenset({"DMY"}),
    "FR": frozenset({"DMY"}),
    "GB": frozenset({"DMY"}),
    "GR": frozenset({"DMY"}),
    "ID": frozenset({"DMY"}),
    "IE": frozenset({"DMY"}),
    "IL": frozenset({"DMY"}),
    "IN": frozenset({"DMY"}),
    "IT": frozenset({"DMY"}),
    "MX": frozenset({"DMY"}),
    "NL": frozenset({"DMY"}),
    "NO": frozenset({"DMY"}),
    "NZ": frozenset({"DMY"}),
    "PL": frozenset({"DMY"}),
    "PT": frozenset({"DMY"}),
    "RU": frozenset({"DMY"}),
    "SK": frozenset({"DMY"}),
    "TR": frozenset({"DMY"}),
    "UA": frozenset({"DMY"}),
    "ZA": frozenset({"DMY"}),
}

# page language → admissible numeric date orders
# "en" covers both en-US and en-GB usage, so it does not decide alone
LANGUAGE_ORDERS: dict[str, frozenset[str]] = {
    "en": frozenset({"MDY", "DMY"}),
    "zh": frozenset({"YMD"}),
    "ja": frozenset({"YMD"}),
    "ko": frozenset({"YMD"}),
    "hu": frozenset({"YMD"}),
    "sv": frozenset({"YMD"}),
    "de": frozenset({"DMY"}),
    "fr": frozenset({"DMY"}),
    "es": frozenset({"DMY"}),
    "it": frozenset({"DMY"}),
    "pt": frozenset({"DMY"}),
    "nl": frozenset({"DMY"}),
    "pl": frozenset({"DMY"}),
    "tr": frozenset({"DMY"}),
    "ru": frozenset({"DMY"}),
    "uk": frozenset({"DMY"}),
    "cs": frozenset({"DMY"}),
    "sk": frozenset({"DMY"}),
    "da": frozenset({"DMY"}),
    "fi": frozenset({"DMY"}),
    "no": frozenset({"DMY"}),
    "id": frozenset({"DMY"}),
}

# loose numeric patterns: validity is checked per interpretation, so the
# number ranges are intentionally wider than real days/months
NUMERIC_YMD = re.compile(
    r"(?:\D|^)(?P<year>(?:199[0-9]|20[0-3][0-9]))[\-/.]"
    r"(?P<month>[0-9]{1,2})[\-/.](?P<day>[0-9]{1,2})(?:\D|$)"
)
NUMERIC_AMBIGUOUS = re.compile(
    r"(?:\D|^)(?P<first>[0-9]{1,2})[\-/.](?P<second>[0-9]{1,2})[\-/.]"
    r"(?P<year>[0-9]{2,4})(?:\D|$)"
)


class UncertainStatus(Enum):
    "Outcome categories of an uncertain date parse."

    UNIQUE = "unique"  # exactly one candidate survives
    AMBIGUOUS = "ambiguous"  # several candidates survive, kept as a set
    OUT_OF_RANGE = "out_of_range"  # valid dates, all excluded by the business range
    INVALID = "invalid"  # every interpretation is an impossible date
    NO_DATE = "no_date"  # no date-like expression found


@dataclass
class DateCandidate:
    "One surviving interpretation, with the orders that produce it."

    date: datetime
    orders: tuple[str, ...]

    def isoformat(self) -> str:
        "Render the candidate date as ISO string."
        return self.date.strftime("%Y-%m-%d")


@dataclass
class UncertainDate:
    "Structured result of the uncertain date mode."

    status: UncertainStatus
    candidates: list[DateCandidate] = field(default_factory=list)
    excluded: list[DateCandidate] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    conflict: bool = False

    @property
    def resolved(self) -> datetime | None:
        "The single surviving date, or None when the parse stays undecided."
        if self.status is UncertainStatus.UNIQUE:
            return self.candidates[0].date
        return None

    def format(self, outputformat: str = "%Y-%m-%d") -> str | None:
        "Format the resolved date, or None when there is no unique answer."
        resolved = self.resolved
        return resolved.strftime(outputformat) if resolved else None


def _orders_from_regions(regions: list[str] | None) -> frozenset[str] | None:
    "Combine caller-allowed regions into a set of admissible orders."
    if not regions:
        return None
    orders: set[str] = set()
    for region in regions:
        known = REGION_ORDERS.get(region.upper())
        if known is None:
            LOGGER.debug("unknown region, no constraint added: %s", region)
            continue
        orders |= known
    return frozenset(orders) if orders else None


def _orders_from_language(language: str | None) -> frozenset[str] | None:
    "Map a page language (optionally with region subtag) to admissible orders."
    if not language:
        return None
    code = language.lower().replace("_", "-")
    lang, _, subtag = code.partition("-")
    # an explicit region subtag is stronger evidence than the language alone
    if subtag and subtag.upper() in REGION_ORDERS:
        return REGION_ORDERS[subtag.upper()]
    known = LANGUAGE_ORDERS.get(lang)
    if known is None:
        LOGGER.debug("unknown language, no constraint added: %s", language)
    return known


def _combine_orders(
    regions: list[str] | None, language: str | None
) -> tuple[frozenset[str], bool]:
    """Intersect the context sources. On contradiction, fall back to the
    union and flag the conflict instead of voting for a winner."""
    sources = [
        orders
        for orders in (
            _orders_from_regions(regions),
            _orders_from_language(language),
        )
        if orders
    ]
    if not sources:
        return ALL_ORDERS, False
    effective = frozenset.intersection(*sources)
    if effective:
        return effective, False
    LOGGER.debug("context conflict between %s, keeping all candidates", sources)
    return frozenset.union(*sources), True


def _build_candidate(year: int, month: int, day: int, order: str) -> DateCandidate | str:
    "Build a candidate or return a description of the invalid interpretation."
    try:
        return DateCandidate(datetime(correct_year(year), month, day), (order,))
    except ValueError:
        return f"{order}: {year:04d}-{month:02d}-{day:02d} is not a real date"


def _merge(candidates: list[DateCandidate], candidate: DateCandidate) -> None:
    "Add a candidate, merging orders when the same date is already present."
    for existing in candidates:
        if existing.date == candidate.date:
            existing.orders = tuple(sorted(set(existing.orders) | set(candidate.orders)))
            return
    candidates.append(candidate)


def _numeric_candidates(
    string: str, orders: frozenset[str]
) -> tuple[list[DateCandidate], list[str]]:
    "Generate candidates for a numeric date under each admissible order."
    candidates: list[DateCandidate] = []
    invalid: list[str] = []

    match = NUMERIC_YMD.search(string)
    if match:
        result = _build_candidate(
            int(match["year"]), int(match["month"]), int(match["day"]), "YMD"
        )
        if isinstance(result, DateCandidate):
            candidates.append(result)
        else:
            invalid.append(result)
        return candidates, invalid

    match = NUMERIC_AMBIGUOUS.search(string)
    if match:
        first, second, year = (
            int(match["first"]),
            int(match["second"]),
            int(match["year"]),
        )
        for order in sorted(orders & {"DMY", "MDY"}):
            day, month = (first, second) if order == "DMY" else (second, first)
            result = _build_candidate(year, month, day, order)
            if isinstance(result, DateCandidate):
                _merge(candidates, result)
            else:
                invalid.append(result)
    return candidates, invalid


def parse_uncertain_date(
    string: str,
    *,
    regions: list[str] | None = None,
    language: str | None = None,
    min_date: datetime | str | None = None,
    max_date: datetime | str | None = None,
) -> UncertainDate:
    """Parse a date expression without committing to a day/month order.

    Combines the caller-allowed regions, the page language and explicit
    month names in the text to generate candidates. Returns an UncertainDate
    reporting a unique date, the surviving candidate set, the candidates
    excluded by the business range, or the invalid interpretations.
    """
    earliest, latest = get_min_date(min_date), get_max_date(max_date)
    string = trim_text(string)
    if not string:
        return UncertainDate(UncertainStatus.NO_DATE)

    # explicit month text pins the month, no order ambiguity remains
    text_date = regex_parse(string)
    if text_date is not None:
        candidate = DateCandidate(text_date, ("TEXT",))
        if is_valid_date(text_date, "%Y-%m-%d", earliest=earliest, latest=latest):
            return UncertainDate(UncertainStatus.UNIQUE, candidates=[candidate])
        return UncertainDate(UncertainStatus.OUT_OF_RANGE, excluded=[candidate])

    orders, conflict = _combine_orders(regions, language)
    candidates, invalid = _numeric_candidates(string, orders)
    if not candidates and not invalid:
        return UncertainDate(UncertainStatus.NO_DATE, conflict=conflict)

    surviving = [
        candidate
        for candidate in candidates
        if is_valid_date(candidate.date, "%Y-%m-%d", earliest=earliest, latest=latest)
    ]
    excluded = [candidate for candidate in candidates if candidate not in surviving]

    if len(surviving) == 1:
        status = UncertainStatus.UNIQUE
    elif surviving:
        status = UncertainStatus.AMBIGUOUS
    elif excluded:
        status = UncertainStatus.OUT_OF_RANGE
    else:
        status = UncertainStatus.INVALID
    return UncertainDate(
        status,
        candidates=surviving,
        excluded=excluded,
        invalid=invalid,
        conflict=conflict,
    )
