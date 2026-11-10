# pylint:disable-msg=E0611
"""
Relative date mode: resolve expressions like "yesterday 23:30" or "vor 2 Stunden"
against a caller-supplied observation time and site time zone.

Design principles:
- The observation moment, the site time zone and the allowed expression range
  are explicit inputs, the local system clock and time zone are never consulted.
- Day-level expressions ("yesterday", "gestern") are resolved as calendar days
  in the site time zone, sub-day expressions ("2 hours ago") as absolute
  elapsed time computed in UTC and converted back to the site zone.
- Wall times that are ambiguous or nonexistent due to DST transitions are not
  silently resolved: the ambiguity is logged and the expression is only used
  when all interpretations agree on the same calendar day.
"""

import logging
import re

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from lxml.html import HtmlElement

# own
from .extractors import FREE_TEXT_EXPRESSIONS
from .settings import MAX_SEGMENT_LEN
from .utils import Extractor, trim_text
from .validators import is_valid_date

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RelativeContext:
    "Caller-supplied context for resolving relative date expressions."

    base: datetime  # aware, normalized to the site time zone
    timezone: tzinfo  # site time zone
    max_days: int  # furthest allowed distance between base and resolved day


def build_relative_context(
    base: datetime | str,
    site_timezone: str | tzinfo,
    max_days: int = 7,
) -> RelativeContext:
    """Validate the caller inputs and normalize the observation moment to the
    site time zone. A naive base is read as local time in the site zone, an
    aware base is converted to it. No system defaults are involved."""
    if isinstance(site_timezone, str):
        # raises ZoneInfoNotFoundError on unknown names, never falls back
        zone: tzinfo = ZoneInfo(site_timezone)
    elif isinstance(site_timezone, tzinfo):
        zone = site_timezone
    else:
        raise TypeError(f"incompatible time zone type: {type(site_timezone)}")
    if max_days < 0:
        raise ValueError(f"max_relative_days must be >= 0: {max_days}")
    if isinstance(base, str):
        base = datetime.fromisoformat(base)
    if not isinstance(base, datetime):
        raise TypeError(f"incompatible base type: {type(base)}")
    if base.tzinfo is None:
        base = base.replace(tzinfo=zone)
    else:
        base = base.astimezone(zone)
    return RelativeContext(base=base, timezone=zone, max_days=max_days)


# day words mapped to their distance in calendar days (EN, DE, FR)
DAY_WORDS = {
    "today": 0,
    "yesterday": 1,
    "heute": 0,
    "gestern": 1,
    "vorgestern": 2,
    "aujourd'hui": 0,
    "hier": 1,
    "avant-hier": 2,
}

DAY_WORD_PATTERN = re.compile(
    r"(?<![\w'])("
    + "|".join(
        sorted((re.escape(w) for w in DAY_WORDS), key=len, reverse=True)
    ).replace("'", r"['’]")
    + r")(?![\w'])"
    # optional clock time: "yesterday 23:30", "gestern, 23:30 Uhr", "hier à 23h30"
    r"(?:\s*(?:,|–|-|um|at|à)?\s*([0-2]?[0-9])[:h]([0-5][0-9])(?:\s*Uhr)?)?",
    re.I,
)

# "N units ago" in English, German and French
AGO_PATTERN = re.compile(
    r"(?<![\w'])(?:"
    r"(\d+|an?)\s+(minutes?|hours?|days?|weeks?)\s+ago"
    r"|vor\s+(\d+|eine[rmn]?)\s+(minuten?|stunden?|tage?n?|wochen?)"
    r"|il\s+y\s+a\s+(\d+|une?)\s+(minutes?|heures?|jours?|semaines?)"
    r")(?![\w'])",
    re.I,
)


def _unit_key(unit: str) -> str:
    "Map a localized unit word to a timedelta keyword."
    unit = unit.lower()
    if unit.startswith("min"):
        return "minutes"
    if unit.startswith(("hour", "stund", "heur")):
        return "hours"
    if unit.startswith(("day", "tag", "jour")):
        return "days"
    return "weeks"


def _valid_instants(naive: datetime, zone: tzinfo) -> list[datetime]:
    "All existing UTC interpretations of a local wall time (0, 1 or 2)."
    instants, seen = [], set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        # round-trip through UTC: only existing wall times come back unchanged
        if utc not in seen and utc.astimezone(zone).replace(tzinfo=None) == naive:
            seen.add(utc)
            instants.append(aware)
    return instants


def _within_range(
    instant: datetime, context: RelativeContext, expression: str
) -> datetime | None:
    "Accept the resolved moment if its calendar day lies within the allowed range."
    age = (context.base.date() - instant.date()).days
    if 0 <= age <= context.max_days:
        LOGGER.info(
            "relative expression %r resolved to %s (base %s, zone %s, %s day(s) back)",
            expression,
            instant.isoformat(),
            context.base.isoformat(),
            context.timezone,
            age,
        )
        return instant
    LOGGER.debug(
        "relative expression %r out of range: %s day(s) back, allowed %s",
        expression,
        age,
        context.max_days,
    )
    return None


def _localize_checked(
    naive: datetime, context: RelativeContext, expression: str
) -> datetime | None:
    "Attach the site time zone to a wall time without hiding DST ambiguity."
    instants = _valid_instants(naive, context.timezone)
    if not instants:
        LOGGER.warning(
            "relative expression %r denotes %s, which does not exist in zone %s "
            "(DST gap): leaving it unresolved",
            expression,
            naive,
            context.timezone,
        )
        return None
    if len(instants) == 2:
        if instants[0].date() != instants[1].date():
            LOGGER.warning(
                "relative expression %r denotes %s, ambiguous in zone %s "
                "(DST fold: %s or %s) with different calendar days: leaving it unresolved",
                expression,
                naive,
                context.timezone,
                instants[0].isoformat(),
                instants[1].isoformat(),
            )
            return None
        LOGGER.warning(
            "relative expression %r denotes %s, ambiguous in zone %s "
            "(DST fold: %s or %s): both interpretations share the calendar day %s",
            expression,
            naive,
            context.timezone,
            instants[0].isoformat(),
            instants[1].isoformat(),
            instants[0].date(),
        )
    return _within_range(instants[0], context, expression)


def resolve_relative(expression: str, context: RelativeContext) -> datetime | None:
    """Resolve a relative date expression against the given context.

    Returns an aware datetime in the site time zone, or None if the text holds
    no supported expression, if the result lies outside the allowed range, or
    if DST ambiguity prevents a definite calendar day.
    """
    # calendar-day expressions: day shift in the site time zone
    day_match = DAY_WORD_PATTERN.search(expression)
    if day_match:
        word = day_match[1].lower().replace("’", "'")
        days = DAY_WORDS[word]
        target = context.base.date() - timedelta(days=days)
        if day_match[2] is not None:
            hour = int(day_match[2])
            if hour > 23:
                return None
            wall = time(hour, int(day_match[3]))
        else:
            wall = context.base.time()
        return _localize_checked(datetime.combine(target, wall), context, expression)

    # elapsed-time expressions: absolute arithmetic in UTC, then back to the zone
    ago_match = AGO_PATTERN.search(expression)
    if ago_match:
        groups = [g for g in ago_match.groups() if g is not None]
        amount = int(groups[0]) if groups[0].isdigit() else 1
        delta = timedelta(**{_unit_key(groups[1]): amount})
        instant = (context.base.astimezone(timezone.utc) - delta).astimezone(
            context.timezone
        )
        return _within_range(instant, context, expression)

    return None


def relative_search(tree: HtmlElement, options: Extractor) -> str | None:
    """Scan the page text for relative date expressions and resolve them
    against the caller-supplied context. Inactive unless the relative date
    mode was enabled in find_date()."""
    context = options.relative
    if context is None:
        return None
    for segment in FREE_TEXT_EXPRESSIONS(tree):
        segment = trim_text(segment)
        if not segment or len(segment) > MAX_SEGMENT_LEN:
            continue
        attempt = resolve_relative(segment, context)
        if attempt is not None and is_valid_date(
            attempt, options.format, earliest=options.min, latest=options.max
        ):
            return attempt.strftime(options.format)
    return None
