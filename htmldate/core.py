# pylint:disable-msg=E0611,I1101
"""Module bundling all functions needed to determine the date of HTML strings
or LXML trees.
"""

import logging
import re

from collections import Counter
from collections.abc import Callable, Mapping, Sized
from copy import deepcopy
from datetime import datetime, timezone
from functools import partial
from typing import Any

from lxml.etree import Element
from lxml.html import HtmlElement, tostring

# own
from .extractors import (
    discard_unwanted,
    extract_url_date,
    idiosyncrasies_search,
    img_search,
    json_search,
    regex_parse,
    pattern_search,
    try_date_expr,
    try_date_expr_opts,
    FAST_TAGS,
    FREE_TEXT_EXPRESSIONS,
    YMD_PATTERN,
    DAY_RE,
    MONTH_RE,
    YEAR_RE,
    TIME_TZ_RE,
)
from .settings import (
    CLEANING_LIST,
    MAX_POSSIBLE_CANDIDATES,
    MAX_SEGMENT_LEN,
    MIN_SEGMENT_LEN,
)
from .sources import Source, SourceEvidence, SourcePolicy
from .utils import Extractor, clean_html, load_html, trim_text
from .validators import (
    REFERENCE_FORMAT,
    check_extracted_reference,
    compare_values,
    correct_year,
    filter_ymd_candidate,
    get_min_date,
    get_max_date,
    is_valid_date,
    is_valid_format,
    plausible_year_filter,
    update_reference,
    validate_and_convert,
)

LOGGER = logging.getLogger(__name__)


def logstring(element: HtmlElement) -> str:
    """Format the element to be logged to a string."""
    return tostring(element, pretty_print=False, encoding="unicode").strip()


def serialize(tree: HtmlElement) -> str:
    "Robust conversion to string."
    try:
        return tostring(tree, pretty_print=False, encoding="unicode")
    except UnicodeDecodeError:
        return tostring(tree, pretty_print=False).decode("utf-8", "ignore")


DATE_ATTRIBUTES = {
    "analyticsattributes.articledate",
    "article.created",
    "article_date_original",
    "article:post_date",
    "article.published",
    "article:published",
    "article:published_date",
    "article:published_time",
    "article:publicationdate",
    "bt:pubdate",
    "citation_date",
    "citation_publication_date",
    "content_create_date",
    "created",
    "cxenseparse:recs:publishtime",
    "date",
    "date_created",
    "date_published",
    "datecreated",
    "dateposted",
    "datepublished",
    # Dublin Core: https://wiki.whatwg.org/wiki/MetaExtensions
    "dc.date",
    "dc.created",
    "dc.date.created",
    "dc.date.issued",
    "dc.date.publication",
    "dcsext.articlefirstpublished",
    "dcterms.created",
    "dcterms.date",
    "dcterms.issued",
    "dc:created",
    "dc:date",
    "displaydate",
    "doc_date",
    "field-name-post-date",
    "gentime",
    "mediator_published_time",
    "meta",  # too loose?
    # Open Graph: https://opengraphprotocol.org/
    "og:article:published",
    "og:article:published_time",
    "og:datepublished",
    "og:pubdate",
    "og:publish_date",
    "og:published_time",
    "og:question:published_time",
    "og:regdate",
    "originalpublicationdate",
    "parsely-pub-date",
    "pdate",
    "ptime",
    "pubdate",
    "publishdate",
    "publish_date",
    "publish_time",
    "publish-date",
    "published-date",
    "published_date",
    "published_time",
    "publisheddate",
    "publication_date",
    "rbpubdate",
    "release_date",
    "rnews:datepublished",
    "sailthru.date",
    "shareaholic:article_published_time",
    "timestamp",
    "twt-published-at",
    "video:release_date",
    "vr:published_time",
}


NAME_MODIFIED = {
    "lastdate",
    "lastmod",
    "lastmodified",
    "last-modified",
    "modified",
    "utime",
}


PROPERTY_MODIFIED = {
    "article:modified",
    "article:modified_date",
    "article:modified_time",
    "article:post_modified",
    "bt:moddate",
    "datemodified",
    "dc.modified",
    "dcterms.modified",
    "lastmodified",
    "modified_time",
    "modificationdate",
    "og:article:modified_time",
    "og:modified_time",
    "og:updated_time",
    "release_date",
    "revision_date",
    "updated_time",
}


ITEMPROP_ATTRS_ORIGINAL = {"datecreated", "datepublished", "pubyear"}
ITEMPROP_ATTRS_MODIFIED = {"datemodified", "dateupdate"}
ITEMPROP_ATTRS = ITEMPROP_ATTRS_ORIGINAL.union(ITEMPROP_ATTRS_MODIFIED)
CLASS_ATTRS = {"date-published", "published", "time published"}

NON_DIGITS_REGEX = re.compile(r"\D+$")

TIMESTAMP_PATTERN = re.compile(rf"({YEAR_RE}-{MONTH_RE}-{DAY_RE})({TIME_TZ_RE})")
# same without the mandatory time: fast-mode last resort only, see find_date
TIMESTAMP_LOOSE_PATTERN = re.compile(rf"({YEAR_RE}-{MONTH_RE}-{DAY_RE})({TIME_TZ_RE})?")

# component patterns
THREE_COMP_REGEX_A = re.compile(rf"({DAY_RE})[/.-]({MONTH_RE})[/.-]({YEAR_RE})")
THREE_COMP_REGEX_B = re.compile(
    rf"({DAY_RE})/({MONTH_RE})/([0-9]{{2}})|({DAY_RE})[.-]({MONTH_RE})[.-]([0-9]{{2}})"
)
TWO_COMP_REGEX = re.compile(rf"({MONTH_RE})[/.-]({YEAR_RE})")

# extensive search patterns
YEAR_PATTERN = re.compile(rf"^\D?({YEAR_RE})")
# bounded gap \D{0,99} (not \D*) avoids ReDoS
COPYRIGHT_PATTERN = re.compile(
    rf"(?:©|\&copy;|Copyright|\(c\))\D{{0,99}}(?:{YEAR_RE})?-?({YEAR_RE})\D"
)
THREE_PATTERN = re.compile(r"/([0-9]{4}/[0-9]{2}/[0-9]{2})[01/]")
THREE_LOOSE_PATTERN = re.compile(r"\D([0-9]{4}[/.-][0-9]{2}[/.-][0-9]{2})\D")
THREE_LOOSE_CATCH = re.compile(r"([0-9]{4})[/.-]([0-9]{2})[/.-]([0-9]{2})")
SELECT_YMD_PATTERN = re.compile(rf"\D({DAY_RE}[/.-]{MONTH_RE}[/.-][0-9]{{4}})\D")
SELECT_YMD_YEAR = re.compile(rf"({YEAR_RE})\D?$")
DATESTRINGS_PATTERN = re.compile(
    r"(\D19[0-9]{2}[01][0-9][0-3][0-9]\D|\D20[0-9]{2}[01][0-9][0-3][0-9]\D)"
)
DATESTRINGS_CATCH = re.compile(rf"({YEAR_RE})([01][0-9])([0-3][0-9])")
SLASHES_PATTERN = re.compile(
    rf"\D({DAY_RE}/{MONTH_RE}/[0129][0-9]|[0-3][0-9]\.[01][0-9]\.[0129][0-9])\D"
)
SLASHES_YEAR = re.compile(r"([0-9]{2})$")
YYYYMM_PATTERN = re.compile(r"\D([12][0-9]{3}[/.-](?:1[0-2]|0[1-9]))\D")
YYYYMM_CATCH = re.compile(rf"({YEAR_RE})[/.-](1[0-2]|0[1-9])")
MMYYYY_PATTERN = re.compile(rf"\D({MONTH_RE}[/.-][12][0-9]{{3}})\D")
SIMPLE_PATTERN = re.compile(rf"(?<!w3.org)\D({YEAR_RE})\D")

THREE_COMP_PATTERNS = (THREE_PATTERN, THREE_LOOSE_PATTERN)


def examine_text(
    text: str,
    options: Extractor,
) -> str | None:
    "Prepare text and try to extract a date."
    text = trim_text(text)
    if len(text) <= MIN_SEGMENT_LEN:
        return None
    text = NON_DIGITS_REGEX.sub("", text[:MAX_SEGMENT_LEN])
    return try_date_expr_opts(text, options)


def has_plausible_candidates(candidates: Sized) -> bool:
    "Check that the number of candidates is neither zero nor excessive."
    return 0 < len(candidates) <= MAX_POSSIBLE_CANDIDATES


ALWAYS_TAGS = frozenset({"footer", "small"})
ID_CLASS_CUES = re.compile("[Mm]eta|time|publish|footer")
CLASS_CUES = re.compile(
    "info|post_detail|block-content|byline|subline|posted|submitted|created-post|"
    "publication|author|autor|field-content|fa-clock-o|fa-calendar|fecha|parution"
)


def is_date_candidate(elem: HtmlElement) -> bool:
    "Mirrors the former XPath: of id and class, only the first in source order counts."
    first = itemprop = None
    for key, value in elem.attrib.items():
        if key == "itemprop":
            itemprop = value
        elif first is None and key in ("id", "class"):
            first = value
    for cue in (first, itemprop):
        if cue is not None:
            folded = cue.replace("D", "d")
            if "date" in folded or "datum" in folded:
                return True
    if first is None:
        return False
    if ID_CLASS_CUES.search(first):
        return True
    cls = elem.get("class")
    if cls is not None and CLASS_CUES.search(cls):
        return True
    idval = elem.get("id")
    return idval is not None and "footer-info-lastmod" in idval


def date_candidates(tree: HtmlElement, extensive_search: bool) -> list[HtmlElement]:
    "Collect date-bearing elements."
    elements = (
        tree.iterdescendants(Element)
        if extensive_search
        else tree.iterdescendants(*FAST_TAGS, *ALWAYS_TAGS)
    )
    return [e for e in elements if e.tag in ALWAYS_TAGS or is_date_candidate(e)]


def examine_elements(elements: list[HtmlElement], options: Extractor) -> str | None:
    "Check candidate elements for date strings."
    if not has_plausible_candidates(elements):
        return None
    for elem in elements:
        # try element text and link title (Blogspot)
        for text in [elem.text_content(), elem.get("title", "")]:
            attempt = examine_text(text, options)
            if attempt:
                return attempt
    return None


def examine_header(
    tree: HtmlElement,
    options: Extractor,
) -> str | None:
    """
    Parse header elements to find date cues

    :param tree:
        LXML parsed tree object
    :type tree: LXML tree
    :param options:
        Options for extraction
    :type options: Extractor
    :return: Returns a valid date expression as a string, or None

    """
    headerdate, reserve = examine_header_dates(tree, options)
    # if nothing was found, look for lower granularity (so far: "copyright year")
    if headerdate is None and reserve is not None:
        LOGGER.debug("opting for reserve date with less granularity")
        headerdate = reserve
    return headerdate


def examine_header_dates(
    tree: HtmlElement,
    options: Extractor,
) -> tuple[str | None, str | None]:
    """
    Parse header elements to find date cues, keeping the matching date and the
    less reliable fallback ("reserve") date apart

    :param tree:
        LXML parsed tree object
    :type tree: LXML tree
    :param options:
        Options for extraction
    :type options: Extractor
    :return: Returns a tuple of the matching date and the reserve date, as
        valid date expressions or None

    """
    headerdate, reserve = None, None
    if not options.policy.permits(Source.META):
        if options.evidence is not None:
            options.evidence.note_excluded(
                Source.META, options.policy.block_reason(Source.META)
            )
        return headerdate, reserve
    tryfunc = partial(try_date_expr_opts, options=options)
    # loop through all meta elements
    for elem in tree.iterfind(".//meta"):
        # safeguard
        if "content" not in elem.attrib and "datetime" not in elem.attrib:
            continue
        content = elem.get("content")
        # name attribute, most frequent
        if "name" in elem.attrib:
            attribute = elem.get("name", "").lower()
            # url
            if attribute == "og:url":
                # the date comes from the URL channel, so honor its policy
                if options.policy.permits(Source.URL):
                    url_candidate = extract_url_date(content, options)
                    if url_candidate is not None:
                        reserve = url_candidate
                        _record(options, Source.URL, url_candidate)
            # date
            elif attribute in DATE_ATTRIBUTES:
                LOGGER.debug("examining meta name: %s", logstring(elem))
                headerdate = tryfunc(content)
            # modified
            elif attribute in NAME_MODIFIED:
                LOGGER.debug("examining meta name: %s", logstring(elem))
                if not options.original:
                    headerdate = tryfunc(content)
                else:
                    reserve = tryfunc(content) or reserve
        # property attribute
        elif "property" in elem.attrib:
            attribute = elem.get("property", "").lower()
            if attribute in DATE_ATTRIBUTES or attribute in PROPERTY_MODIFIED:
                LOGGER.debug("examining meta property: %s", logstring(elem))
                attempt = tryfunc(content)
                if attempt is not None:
                    if attribute in (
                        DATE_ATTRIBUTES if options.original else PROPERTY_MODIFIED
                    ):
                        headerdate = attempt
                    # hurts precision
                    else:
                        reserve = attempt
        # itemprop
        elif "itemprop" in elem.attrib:
            attribute = elem.get("itemprop", "").lower()
            # original: store / updated: override date
            if attribute in ITEMPROP_ATTRS:
                LOGGER.debug("examining meta itemprop: %s", logstring(elem))
                attempt = tryfunc(elem.get("datetime") or content)
                # store value
                if attempt is not None:
                    if attribute in (
                        ITEMPROP_ATTRS_ORIGINAL
                        if options.original
                        else ITEMPROP_ATTRS_MODIFIED
                    ):
                        headerdate = attempt
                    # put on hold: hurts precision
                    # else:
                    #    reserve = attempt
            # reserve with copyrightyear
            elif attribute == "copyrightyear":
                LOGGER.debug("examining meta itemprop: %s", logstring(elem))
                if content is not None:
                    attempt = "-".join([content, "01", "01"])
                    if is_valid_date(
                        attempt, "%Y-%m-%d", earliest=options.min, latest=options.max
                    ):
                        reserve = datetime(int(attempt[:4]), 1, 1).strftime(
                            options.format
                        )
        # pubdate, relatively rare
        elif "pubdate" in elem.attrib:
            if elem.get("pubdate", "").lower() == "pubdate":
                LOGGER.debug("examining meta pubdate: %s", logstring(elem))
                headerdate = tryfunc(content)
        # http-equiv, rare
        elif "http-equiv" in elem.attrib:
            attribute = elem.get("http-equiv", "").lower()
            if attribute in ("date", "last-modified"):
                LOGGER.debug("examining meta http-equiv: %s", logstring(elem))
                attempt = tryfunc(content)
                # "date" is original, "last-modified" is updated
                if (attribute == "date") == options.original:
                    headerdate = attempt
                else:
                    reserve = attempt or reserve
        # exit loop
        if headerdate is not None:
            break
    # return values
    return headerdate, reserve


def select_candidate(
    occurrences: Counter[str],
    catch: re.Pattern[str],
    yearpat: re.Pattern[str],
    options: Extractor,
) -> re.Match[str] | None:
    """Select a candidate among the most frequent matches"""
    if not has_plausible_candidates(occurrences):
        return None

    if len(occurrences) == 1:
        return catch.search(next(iter(occurrences)))

    # select among most frequent: more than 10? more than 2 candidates?
    firstselect = occurrences.most_common(10)
    LOGGER.debug("firstselect: %s", firstselect)
    # sort and find probable candidates
    bestones = sorted(firstselect, reverse=not options.original)[:2]
    LOGGER.debug("bestones: %s", bestones)

    # plausibility heuristics
    patterns, counts = zip(*bestones)

    years = []
    for pattern in patterns:
        year_match = yearpat.search(pattern)
        if year_match:
            years.append(year_match[1])

    min_year, max_year = options.min.year, options.max.year
    validation = [min_year <= int(year) <= max_year for year in years]

    # safety net: plausibility
    if all(validation):
        # the runner-up (older, or newer if original) wins when from another
        # year and more than half as frequent, except on ties
        if (
            counts[0] != counts[1]
            and years[1] != years[0]
            and counts[1] / counts[0] > 0.5
        ):
            match = catch.search(patterns[1])
        else:
            match = catch.search(patterns[0])
    elif any(validation):
        match = catch.search(patterns[validation.index(True)])
    else:
        LOGGER.debug("no suitable candidate: %s %s", years[0], years[1])
        match = None
    return match


def search_pattern(
    htmlstring: str,
    pattern: re.Pattern[str],
    catch: re.Pattern[str],
    yearpat: re.Pattern[str],
    options: Extractor,
) -> re.Match[str] | None:
    """Chained candidate filtering and selection"""
    candidates = plausible_year_filter(
        htmlstring,
        pattern=pattern,
        yearpat=yearpat,
        earliest=options.min,
        latest=options.max,
    )
    return select_candidate(candidates, catch, yearpat, options)


def compare_reference(
    reference: datetime | None,
    expression: str,
    options: Extractor,
) -> datetime | None:
    """Compare candidate to current date reference (includes date validation and older/newer test)"""
    attempt = try_date_expr(
        expression, REFERENCE_FORMAT, options.extensive, options.min, options.max
    )
    if attempt is not None:
        return compare_values(reference, attempt, options)
    return reference


def examine_abbr_elements(
    tree: HtmlElement,
    options: Extractor,
) -> str | None:
    """Scan the page for abbr elements and check if their content contains an eligible date"""
    if not options.policy.permits(Source.CONTENT):
        if options.evidence is not None:
            options.evidence.note_excluded(
                Source.CONTENT, options.policy.block_reason(Source.CONTENT)
            )
        return None
    elements = tree.findall(".//abbr")
    if has_plausible_candidates(elements):
        reference: datetime | None = None
        for elem in elements:
            # data-utime (mostly Facebook)
            if "data-utime" in elem.attrib:
                # untrusted: may be outside the platform timestamp range
                try:
                    candidate = datetime.fromtimestamp(
                        int(elem.get("data-utime", "")), tz=timezone.utc
                    )
                except (OSError, OverflowError, ValueError):
                    continue
                LOGGER.debug("data-utime found: %s", candidate)
                reference = update_reference(reference, candidate, options.original)
            # class
            elif elem.get("class") in CLASS_ATTRS:
                # other attributes
                trytext = elem.get("title")
                if trytext is not None:
                    LOGGER.debug("abbr published-title found: %s", trytext)
                    # shortcut
                    if options.original:
                        attempt = try_date_expr_opts(trytext, options)
                        if attempt is not None:
                            return attempt
                    else:
                        reference = compare_reference(reference, trytext, options)
                        # faster execution
                        if reference is not None:
                            break
                # dates, not times of the day
                elif elem.text and len(elem.text) > 10:
                    LOGGER.debug("abbr published found: %s", elem.text)
                    reference = compare_reference(reference, elem.text, options)
        # return or try rescue in abbr content
        return check_extracted_reference(reference, options) or examine_elements(
            elements, options
        )
    return None


def examine_time_elements(
    tree: HtmlElement,
    options: Extractor,
) -> str | None:
    """Scan the page for time elements and check if their content contains an eligible date"""
    if not options.policy.permits(Source.TIME):
        if options.evidence is not None:
            options.evidence.note_excluded(
                Source.TIME, options.policy.block_reason(Source.TIME)
            )
        return None
    elements = tree.findall(".//time")
    if has_plausible_candidates(elements):
        # scan all the tags and look for the newest one
        reference: datetime | None = None
        for elem in elements:
            datetime_attr = elem.get("datetime", "")
            # go for datetime
            if len(datetime_attr) > 6:
                class_attr = elem.get("class", "")
                # mode-specific shortcut attributes
                if options.original:
                    shortcut_flag = elem.get(
                        "pubdate"
                    ) == "pubdate" or class_attr.startswith(
                        ("entry-date", "entry-time")
                    )
                else:
                    shortcut_flag = class_attr == "updated"
                # analyze attribute
                if shortcut_flag:
                    LOGGER.debug("shortcut for time/datetime found: %s", datetime_attr)
                    attempt = try_date_expr_opts(datetime_attr, options)
                    if attempt is not None:
                        return attempt
                else:
                    LOGGER.debug("time/datetime found: %s", datetime_attr)
                    reference = compare_reference(reference, datetime_attr, options)
            # bare text in element
            elif elem.text is not None and len(elem.text) > 6:
                LOGGER.debug("time/datetime found in text: %s", elem.text)
                reference = compare_reference(reference, elem.text, options)
            # else...?
        # return
        return check_extracted_reference(reference, options)
    return None


def normalize_match(pattern: re.Pattern[str], item: str) -> str:
    "Zero-pad the matched components and expand a 2-digit year."
    match = pattern.match(item)
    day, month, year = (g.zfill(2) for g in match.groups() if g)  # type: ignore[union-attr]
    if len(year) == 2:
        year = str(correct_year(int(year)))
    return f"{year}-{month}-{day}"


def normalize_two_comp(item: str) -> str:
    """Normalize a MM-YYYY style match into a YYYY-MM-01 string."""
    match = TWO_COMP_REGEX.match(item)
    month = match[1].zfill(2)  # type: ignore[index]
    return "-".join([match[2], month, "01"])  # type: ignore[index]


def search_normalized(
    htmlstring: str,
    pattern: re.Pattern[str],
    yearpat: re.Pattern[str],
    normalizer: Callable[[str], str],
    copyear: int,
    options: Extractor,
) -> str | None:
    """Filter plausible years, normalize each candidate to the YMD format, then
    select the best match and validate it (shared candidate-selection pipeline)."""
    candidates = plausible_year_filter(
        htmlstring,
        pattern=pattern,
        yearpat=yearpat,
        earliest=options.min,
        latest=options.max,
    )
    # revert DD-MM-YYYY patterns before sorting
    normalized: Counter[str] = Counter()
    for item, count in candidates.items():
        normalized[normalizer(item)] += count
    bestmatch = select_candidate(normalized, YMD_PATTERN, YEAR_PATTERN, options)
    return _filter_ymd(bestmatch, copyear, options)


def _filter_ymd(
    bestmatch: re.Match[str] | None, copyear: int, options: Extractor
) -> str | None:
    "Convert the match to YMD groups (kept outside the lru_cache) and validate."
    return filter_ymd_candidate(
        bestmatch.groups() if bestmatch else None,
        copyear,
        options.format,
        options.min,
        options.max,
    )


def _search_and_filter(
    htmlstring: str,
    pattern: re.Pattern[str],
    catch: re.Pattern[str],
    copyear: int,
    options: Extractor,
) -> str | None:
    "Search for a date pattern, then validate the best match in the YMD format."
    bestmatch = search_pattern(htmlstring, pattern, catch, YEAR_PATTERN, options)
    return _filter_ymd(bestmatch, copyear, options)


def _finalize_candidate(
    dateobject: datetime | None, copyear: int, options: Extractor
) -> str | None:
    "Apply the copyright-year floor, then validate and convert a candidate date."
    if dateobject is not None and (copyear == 0 or dateobject.year >= copyear):
        return validate_and_convert(
            dateobject, options.format, earliest=options.min, latest=options.max
        )
    return None


def search_page(htmlstring: str, options: Extractor) -> str | None:
    """
    Opportunistically search the HTML text for common text patterns

    :param htmlstring:
        The HTML document in string format, potentially cleaned and stripped to
        the core (much faster)
    :type htmlstring: string
    :param options:
        Define extraction options
    :type options: Extractor
    :return: Returns a valid date expression as a string, or None

    """

    # copyright symbol
    LOGGER.debug("looking for copyright/footer information")
    copyear = 0
    bestmatch = search_pattern(
        htmlstring,
        COPYRIGHT_PATTERN,
        YEAR_PATTERN,
        YEAR_PATTERN,
        options,
    )
    if bestmatch is not None:
        year = int(bestmatch[1])
        if is_valid_date(
            datetime(year, 1, 1), "%Y", earliest=options.min, latest=options.max
        ):
            LOGGER.debug("copyright year/footer pattern found: %s", year)
            copyear = year

    # 3 components
    LOGGER.debug("3 components")
    # target URL characteristics
    # then more loosely structured data
    for pattern in THREE_COMP_PATTERNS:
        result = _search_and_filter(
            htmlstring, pattern, THREE_LOOSE_CATCH, copyear, options
        )
        if result is not None:
            return result

    # YYYY-MM-DD/DD-MM-YYYY
    result = search_normalized(
        htmlstring,
        SELECT_YMD_PATTERN,
        SELECT_YMD_YEAR,
        partial(normalize_match, THREE_COMP_REGEX_A),
        copyear,
        options,
    )
    if result is not None:
        return result

    # valid dates strings
    result = _search_and_filter(
        htmlstring, DATESTRINGS_PATTERN, DATESTRINGS_CATCH, copyear, options
    )
    if result is not None:
        return result

    # DD?/MM?/YY
    result = search_normalized(
        htmlstring,
        SLASHES_PATTERN,
        SLASHES_YEAR,
        partial(normalize_match, THREE_COMP_REGEX_B),
        copyear,
        options,
    )
    if result is not None:
        return result

    # 2 components
    LOGGER.debug("switching to two components")
    # first option
    bestmatch = search_pattern(
        htmlstring,
        YYYYMM_PATTERN,
        YYYYMM_CATCH,
        YEAR_PATTERN,
        options,
    )
    if bestmatch is not None:
        result = _finalize_candidate(
            datetime(int(bestmatch[1]), int(bestmatch[2]), 1), copyear, options
        )
        if result is not None:
            return result

    # 2 components, second option
    result = search_normalized(
        htmlstring,
        MMYYYY_PATTERN,
        SELECT_YMD_YEAR,
        normalize_two_comp,
        copyear,
        options,
    )
    if result is not None:
        return result

    # try full-blown text regex on all HTML?
    # todo: find all candidates and disambiguate?
    result = _finalize_candidate(regex_parse(htmlstring), copyear, options)
    if result is not None:
        return result

    # catchall: copyright mention
    if copyear != 0:
        LOGGER.debug("using copyright year as default")
        dateobject = datetime(copyear, 1, 1)
        return dateobject.strftime(options.format)

    # last resort: 1 component
    LOGGER.debug("switching to one component")
    bestmatch = search_pattern(
        htmlstring,
        SIMPLE_PATTERN,
        YEAR_PATTERN,
        YEAR_PATTERN,
        options,
    )
    if bestmatch is not None:
        return _finalize_candidate(datetime(int(bestmatch[1]), 1, 1), copyear, options)

    return None


def _record(options: Extractor, source: Source, value: str | None) -> None:
    "Register a value produced by a permitted channel for the audit trail."
    if value is not None and options.evidence is not None:
        options.evidence.record(source, value)


def _coerce_policy(policy: SourcePolicy | Mapping[str, Any] | str | None) -> SourcePolicy:
    "Normalize the source_policy argument into a SourcePolicy instance."
    if policy is None:
        return SourcePolicy()
    if isinstance(policy, SourcePolicy):
        return policy
    if isinstance(policy, Mapping):
        return SourcePolicy.from_mapping(policy)
    if isinstance(policy, str):
        return SourcePolicy.from_json(policy)
    raise TypeError(
        "source_policy must be a SourcePolicy, a mapping of source lists or a JSON string"
    )


# markup tied to a dedicated channel, removed from the content search tree when
# that channel is disabled so generic text regexes cannot resurrect its date
_POLICY_XPATHS = {
    # og:url/og:image belong to the URL channel, so keep them when only meta is off
    Source.META: (
        './/meta[not(@property="og:url" or @property="og:image")]',
    ),
    Source.STRUCTURED: (
        './/script[@type="application/ld+json"]',
        './/script[@type="application/settings+json"]',
    ),
    Source.TIME: (".//time",),
    Source.URL: (
        './/link[@rel="canonical"]',
        './/meta[@property="og:url"]',
        './/meta[@property="og:image"]',
    ),
}


def strip_disabled_sources(tree: HtmlElement, policy: SourcePolicy) -> None:
    """Remove markup belonging to disabled channels from the content search
    tree. The dedicated extractors already ran (or were gated) on the original
    tree; this only stops generic body regexes from reading the same literals."""
    for source, expressions in _POLICY_XPATHS.items():
        if policy.permits(source):
            continue
        for expression in expressions:
            for element in tree.xpath(expression):
                element.drop_tree()


def find_date(
    htmlobject: bytes | str | HtmlElement,
    extensive_search: bool = True,
    original_date: bool = False,
    outputformat: str = "%Y-%m-%d",
    url: str | None = None,
    verbose: bool = False,
    min_date: datetime | str | None = None,
    max_date: datetime | str | None = None,
    deferred_url_extractor: bool = False,
    source_policy: SourcePolicy | Mapping[str, Any] | str | None = None,
    explain: bool = False,
) -> str | None | dict[str, Any]:
    """
    Extract dates from HTML documents using markup analysis and text patterns

    :param htmlobject:
        Two possibilities: 1. HTML document (e.g. body of HTTP request or .html-file) in text string
        form or LXML parsed tree or 2. URL string (gets detected automatically)
    :type htmlobject: string or lxml tree
    :param extensive_search:
        Activate pattern-based opportunistic text search
    :type extensive_search: boolean
    :param original_date:
        Look for original date (e.g. publication date) instead of most recent
        one (e.g. last modified, updated time)
    :type original_date: boolean
    :param outputformat:
        Provide a valid datetime format for the returned string
        (see datetime.strftime())
    :type outputformat: string
    :param url:
        Provide an URL manually for pattern-searching in URL
        (in some cases much faster)
    :type url: string
    :param verbose:
        Set verbosity level for debugging
    :type verbose: boolean
    :param min_date:
        Set the earliest acceptable date manually (ISO 8601 YMD format)
    :type min_date: datetime, string
    :param max_date:
        Set the latest acceptable date manually (ISO 8601 YMD format)
    :type max_date: datetime, string
    :param deferred_url_extractor:
        Use url extractor as backup only to prioritize full expressions,
        e.g. of the type `%Y-%m-%d %H:%M:%S`
    :type deferred_url_extractor: boolean
    :param source_policy:
        Call-level policy deciding which evidence channels (``structured``,
        ``meta``, ``time``, ``content`` and ``url``) may, must not or must
        provide the date. Accepts a :class:`~htmldate.sources.SourcePolicy`,
        a mapping of source lists (``allow``/``disable``/``require``) or a
        JSON string. Disabled channels are never queried, and required
        channels must corroborate the result; otherwise ``None`` is returned.
    :type source_policy: SourcePolicy, dict or string
    :param explain:
        Return an audit dictionary instead of a plain date, listing the
        adopted channels, the values considered, the excluded channels and
        any requirement that could not be satisfied.
    :type explain: boolean
    :return: Returns a valid date expression as a string, or None (or an
        explanation dictionary when ``explain`` is set)
    """

    # init
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    tree = load_html(htmlobject)

    # safeguards
    if tree is None:
        return None
    if outputformat != "%Y-%m-%d" and not is_valid_format(outputformat):
        return None

    # per-call policy and evidence bookkeeping: no module-level state is touched
    policy = _coerce_policy(source_policy)
    evidence = SourceEvidence()

    # define options and time boundaries
    options = Extractor(
        extensive_search,
        get_max_date(max_date),
        get_min_date(min_date),
        original_date,
        outputformat,
        policy,
        evidence,
    )

    # when corroboration is required, every channel is queried and its value
    # adopted; otherwise the historical first-match short-circuit is preserved
    corroborating = bool(policy.require)

    def _short_circuit(
        source: Source, value: str | None
    ) -> str | dict[str, Any] | None:
        "Return early in default mode, or merely gather evidence when requiring."
        if value is None:
            return None
        if corroborating:
            evidence.adopt(source, value)
            return None
        return _finish(options, source, value, explain)

    def _finalize_corroboration() -> str | dict[str, Any] | None:
        "All required channels must be present and agree on one date."
        selected = evidence.selected
        missing = [source for source in policy.require if source not in selected]
        if missing:
            LOGGER.debug(
                "insufficient evidence, missing required source(s): %s",
                ", ".join(source.value for source in missing),
            )
            return _unknown(options, explain)
        required = {source: selected[source] for source in policy.require}
        distinct = set(required.values())
        if len(distinct) > 1:
            disagreement = [
                {"source": source.value, "date": date}
                for source, date in required.items()
            ]
            LOGGER.debug("required sources disagree: %s", disagreement)
            if explain:
                return evidence.explanation(policy, None, disagreement)
            return None
        value = next(iter(distinct))
        if explain:
            return evidence.explanation(policy, value)
        return value

    # URL
    if url is None:
        # probe for canonical links
        urlelem = tree.find('.//link[@rel="canonical"]')
        if urlelem is not None:
            url = urlelem.get("href")

    # direct processing of URL info (skipped entirely when the channel is off)
    url_permitted = policy.permits(Source.URL)
    url_result = extract_url_date(url, options) if url_permitted else None
    _record(options, Source.URL, url_result)
    if not url_permitted:
        evidence.note_excluded(Source.URL, policy.block_reason(Source.URL))
    if url_result is not None and not deferred_url_extractor:
        outcome = _short_circuit(Source.URL, url_result)
        if outcome is not None:
            return outcome

    # first try header
    # then try to use JSON data
    # a header "reserve" date is a low-confidence fallback (e.g. a modification
    # date when the original one was requested): only use it if JSON-LD, which
    # can carry the requested date explicitly, has nothing to offer
    header_result, header_reserve = examine_header_dates(tree, options)
    _record(options, Source.META, header_result)
    _record(options, Source.META, header_reserve)
    if policy.permits(Source.STRUCTURED):
        json_result = json_search(tree, options)
        _record(options, Source.STRUCTURED, json_result)
    else:
        json_result = None
        evidence.note_excluded(
            Source.STRUCTURED, policy.block_reason(Source.STRUCTURED)
        )
    outcome = (
        _short_circuit(Source.META, header_result)
        or _short_circuit(Source.STRUCTURED, json_result)
        or _short_circuit(Source.META, header_reserve)
    )
    if outcome is not None:
        return outcome

    # deferred processing of URL info (may be moved even further down if necessary)
    if deferred_url_extractor and url_result is not None:
        outcome = _short_circuit(Source.URL, url_result)
        if outcome is not None:
            return outcome

    # try abbr elements
    abbr_result = examine_abbr_elements(
        tree,
        options,
    )
    _record(options, Source.CONTENT, abbr_result)
    outcome = _short_circuit(Source.CONTENT, abbr_result)
    if outcome is not None:
        return outcome

    # first, prune tree
    # only copy the tree if the caller passed one in: when we parsed it ourselves
    # (string/bytes/URL input) we own it and can clean it in place, avoiding a
    # costly deepcopy of the whole document
    pruning_tree = deepcopy(tree) if isinstance(htmlobject, HtmlElement) else tree
    try:
        search_tree = discard_unwanted(clean_html(pruning_tree, CLEANING_LIST))
    # rare LXML error: no NULL bytes or control characters
    except ValueError:  # pragma: no cover
        # pruning_tree, not tree: it is ours in both cases, and the fast-mode
        # fallback below strips this tree in place
        search_tree = pruning_tree
        LOGGER.error("lxml cleaner error")

    # purge disabled-channel markup so generic content scans cannot read dates
    # a disabled dedicated channel would have provided (no-op with the open policy)
    strip_disabled_sources(search_tree, policy)

    content_permitted = policy.permits(Source.CONTENT)
    if content_permitted:
        content_result = (
            examine_elements(date_candidates(search_tree, extensive_search), options)
            or examine_elements(search_tree.xpath(".//title|.//h1"), options)
        )
        _record(options, Source.CONTENT, content_result)
    else:
        content_result = None
        evidence.note_excluded(
            Source.CONTENT, policy.block_reason(Source.CONTENT)
        )
    time_result = examine_time_elements(search_tree, options)
    _record(options, Source.TIME, time_result)
    outcome = (
        _short_circuit(Source.CONTENT, content_result)
        or _short_circuit(Source.TIME, time_result)
    )
    if outcome is not None:
        return outcome

    htmlstring = serialize(search_tree)

    # date regex timestamp rescue
    # try image elements (their date is read from an og:image URL)
    # precise patterns and idiosyncrasies
    image_result = img_search(search_tree, options) if url_permitted else None
    _record(options, Source.URL, image_result)
    if content_permitted:
        pattern_result = pattern_search(htmlstring, TIMESTAMP_PATTERN, options)
        idio_result = idiosyncrasies_search(htmlstring, options)
    else:
        pattern_result = idio_result = None
    _record(options, Source.CONTENT, pattern_result)
    _record(options, Source.CONTENT, idio_result)
    outcome = (
        _short_circuit(Source.CONTENT, pattern_result)
        or _short_circuit(Source.URL, image_result)
        or _short_circuit(Source.CONTENT, idio_result)
    )
    if outcome is not None:
        return outcome

    # fast mode has no search_page fallback: accept a bare date, but from text
    # only — itertext() skips comments and attributes yet keeps tail text
    if not extensive_search:
        if content_permitted:
            clean_html(search_tree, ["script", "style"])
            text = " ".join(search_tree.itertext())
            fast_result = pattern_search(text, TIMESTAMP_LOOSE_PATTERN, options)
            _record(options, Source.CONTENT, fast_result)
        else:
            fast_result = None
        outcome = _short_circuit(Source.CONTENT, fast_result)
        if outcome is not None:
            return outcome
        if corroborating:
            return _finalize_corroboration()
        return _unknown(options, explain)

    LOGGER.debug("extensive search started")
    if content_permitted:
        # TODO: further tests & decide according to original_date
        reference: datetime | None = None
        for segment in FREE_TEXT_EXPRESSIONS(search_tree):
            segment = segment.strip()
            if not MIN_SEGMENT_LEN < len(segment) < MAX_SEGMENT_LEN:
                continue
            reference = compare_reference(reference, segment, options)
        converted = check_extracted_reference(reference, options)
        _record(options, Source.CONTENT, converted)
        # return or search page HTML
        page_result = search_page(htmlstring, options)
        _record(options, Source.CONTENT, page_result)
    else:
        converted = page_result = None
    outcome = (
        _short_circuit(Source.CONTENT, converted)
        or _short_circuit(Source.CONTENT, page_result)
    )
    if outcome is not None:
        return outcome
    if corroborating:
        return _finalize_corroboration()
    return _unknown(options, explain)


def _finish(
    options: Extractor,
    source: Source,
    value: str,
    explain: bool,
) -> str | dict[str, Any]:
    "Adopt a channel result and format the return value (default, no-require mode)."
    options.evidence.adopt(source, value)  # type: ignore[union-attr]
    if explain:
        return options.evidence.explanation(options.policy, value)  # type: ignore[union-attr]
    return value


def _unknown(
    options: Extractor,
    explain: bool,
) -> str | None | dict[str, Any]:
    "Return the unknown-date sentinel, optionally with the audit trail."
    if explain:
        return options.evidence.explanation(options.policy, None)  # type: ignore[union-attr]
    return None
