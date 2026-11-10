# pylint:disable-msg=E0611,I1101
"""
Offline comparison of HTML snapshots for the same page.

A page that is crawled repeatedly may have its publication date corrected and
its "last modified" value rolled back. Comparing single snapshots against each
other therefore requires keeping the full observation history instead of
overwriting it with the newest value.

The entry point :func:`compare_snapshots` takes several HTML snapshots together
with the time at which each one was observed, orders them chronologically
(including out-of-order input), and reports when each date declaration
appeared, changed or disappeared. Dates explicitly declared on the page are
kept separate from the trusted dates inferred across observations, and an
article update is only claimed when the body text is known to have changed.
"""

import logging

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256

from lxml.etree import XPath
from lxml.html import HtmlElement

from .core import (
    DATE_ATTRIBUTES,
    ITEMPROP_ATTRS,
    NAME_MODIFIED,
    PROPERTY_MODIFIED,
    find_date,
    logstring,
)
from .extractors import JSON_MODIFIED, JSON_PUBLISHED, try_date_expr_opts
from .utils import Extractor, load_html, trim_text
from .validators import get_max_date, get_min_date, is_valid_format

LOGGER = logging.getLogger(__name__)

# declaration roles
ORIGINAL = "original"
UPDATED = "updated"
UNKNOWN = "unknown"

# change events
EVENT_APPEARED = "appeared"
EVENT_CHANGED = "changed"
EVENT_DISAPPEARED = "disappeared"

# inferred trusted-date statuses
TRUSTED_CONFIRMED = "confirmed"
TRUSTED_CONFLICT = "conflict"
TRUSTED_CORRECTED = "corrected"
TRUSTED_ROLLED_BACK = "rolled_back"
TRUSTED_UNDETERMINED = "undetermined"

# update verdicts
UPDATE_YES = "yes"
UPDATE_NO = "no"
UPDATE_UNDETERMINED = "undetermined"

_JSON_DATE_XPATH = XPath(
    './/script[@type="application/ld+json" or @type="application/settings+json"]'
)
_UPDATED_META_NAMES = frozenset(NAME_MODIFIED)
_ORIGINAL_META_NAMES = frozenset(DATE_ATTRIBUTES)
_UPDATED_META_PROPS = frozenset(PROPERTY_MODIFIED)
_ORIGINAL_META_PROPS = frozenset(DATE_ATTRIBUTES)
_UPDATED_ITEMPROPS = frozenset({"datemodified", "dateupdate"})
_ORIGINAL_ITEMPROPS = frozenset(ITEMPROP_ATTRS - _UPDATED_ITEMPROPS)
_TIME_ORIGINAL_CLASSES = ("entry-date", "entry-time")


@dataclass(slots=True)
class DateDeclaration:
    """A single date statement found in one snapshot.

    :param role: ``original`` for publication-oriented statements,
        ``updated`` for modification-oriented ones, ``unknown`` otherwise
    :param value: normalized date string in the requested output format,
        or ``None`` when the statement could not be parsed as a date
    :param source: kind of markup carrying the statement (``meta``,
        ``json-ld``, ``time`` or ``heuristic``)
    :param locator: element path or attribute name, so the statement can
        be traced back to the markup
    :param raw: raw date expression as written in the snapshot
    :param observed_at: observation time provided for the snapshot
    :param snapshot_id: identifier of the snapshot carrying the statement
    """

    role: str
    value: str | None
    source: str
    locator: str
    raw: str
    observed_at: datetime
    snapshot_id: str

    def as_dict(self) -> dict[str, str | None]:
        "Serialize to a JSON-friendly dictionary."
        return {
            "role": self.role,
            "value": self.value,
            "source": self.source,
            "locator": self.locator,
            "raw": self.raw,
            "observed_at": self.observed_at.isoformat(),
            "snapshot_id": self.snapshot_id,
        }


@dataclass(slots=True)
class DateEvent:
    """A point in the timeline where a declaration changed.

    :param event: one of ``appeared``, ``changed`` or ``disappeared``
    :param observed_at: observation time at which the change is visible
    :param snapshot_id: snapshot documenting the event
    :param role: declaration role the event applies to
    :param source: markup kind of the tracked declaration
    :param locator: markup location of the tracked declaration
    :param previous_value: value carried before the event, if any
    :param value: value carried at the event, ``None`` for disappearances
    :param raw: raw expression carried at the event
    """

    event: str
    observed_at: datetime
    snapshot_id: str
    role: str
    source: str
    locator: str
    previous_value: str | None
    value: str | None
    raw: str | None

    def as_dict(self) -> dict[str, str | None]:
        "Serialize to a JSON-friendly dictionary."
        return {
            "event": self.event,
            "observed_at": self.observed_at.isoformat(),
            "snapshot_id": self.snapshot_id,
            "role": self.role,
            "source": self.source,
            "locator": self.locator,
            "previous_value": self.previous_value,
            "value": self.value,
            "raw": self.raw,
        }


@dataclass(slots=True)
class TrustedDate:
    """Inferred date for one role, kept apart from the raw declarations.

    :param role: declaration role this inference is based on
    :param value: inferred date value, or ``None`` when nothing parseable
        has ever been declared
    :param status: one of the ``TRUSTED_*`` constants
    :param first_seen: earliest observation carrying a value for the role
    :param last_seen: latest observation carrying a value for the role
    :param evidence: snapshot identifiers supporting the inference
    """

    role: str
    value: str | None
    status: str
    first_seen: datetime | None
    last_seen: datetime | None
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        "Serialize to a JSON-friendly dictionary."
        return {
            "role": self.role,
            "value": self.value,
            "status": self.status,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "evidence": list(self.evidence),
        }


@dataclass(slots=True)
class SnapshotAnalysis:
    """What was observed in a single snapshot."""

    snapshot_id: str
    observed_at: datetime
    declarations: list[DateDeclaration]
    content_fingerprint: str | None
    parseable: bool

    def as_dict(self) -> dict[str, object]:
        "Serialize to a JSON-friendly dictionary."
        return {
            "snapshot_id": self.snapshot_id,
            "observed_at": self.observed_at.isoformat(),
            "declarations": [decl.as_dict() for decl in self.declarations],
            "content_fingerprint": self.content_fingerprint,
            "parseable": self.parseable,
        }


@dataclass(slots=True)
class SnapshotHistory:
    """Full, traceable comparison result for one page's snapshots.

    :param snapshots: per-snapshot analyses, ordered by observation time;
        snapshots sharing an observation time stay in input order
    :param events: chronological declaration timeline
    :param trusted_dates: inferred trusted dates, one per observed role
    :param content_changed: ``True`` when the body text differs between
        snapshots, ``False`` when it is identical everywhere, ``None``
        when unparseable snapshots make the comparison inconclusive
    :param article_updated: verdict on whether the article itself was
        updated; never ``yes`` without an observed content change
    """

    snapshots: list[SnapshotAnalysis]
    events: list[DateEvent]
    trusted_dates: dict[str, TrustedDate]
    content_changed: bool | None
    article_updated: str

    def as_dict(self) -> dict[str, object]:
        "Serialize to a JSON-friendly dictionary."
        return {
            "snapshots": [snapshot.as_dict() for snapshot in self.snapshots],
            "events": [event.as_dict() for event in self.events],
            "trusted_dates": {
                role: trusted.as_dict()
                for role, trusted in self.trusted_dates.items()
            },
            "content_changed": self.content_changed,
            "article_updated": self.article_updated,
        }


def _tree_path(elem: HtmlElement) -> str:
    "Build a stable locator for an element, including an attribute hint."
    parts = [str(elem.tag)]
    for attr in ("name", "property", "itemprop", "class", "id"):
        value = elem.get(attr)
        if value:
            parts.append(f"@{attr}={value}")
            break
    return "/".join(parts)


def _make_declaration(
    role: str,
    value: str | None,
    source: str,
    locator: str,
    raw: str | None,
    observed_at: datetime,
    snapshot_id: str,
) -> DateDeclaration:
    return DateDeclaration(
        role=role,
        value=value,
        source=source,
        locator=locator,
        raw=raw or "",
        observed_at=observed_at,
        snapshot_id=snapshot_id,
    )


def _meta_role(elem: HtmlElement) -> str | None:
    "Classify a meta element's date statement by the same cues as find_date()."
    if "name" in elem.attrib:
        attribute = elem.get("name", "").lower()
        if attribute in _UPDATED_META_NAMES:
            return UPDATED
        if attribute in _ORIGINAL_META_NAMES:
            return ORIGINAL
    elif "property" in elem.attrib:
        attribute = elem.get("property", "").lower()
        # modified-oriented properties win when an attribute belongs to both sets
        if attribute in _UPDATED_META_PROPS:
            return UPDATED
        if attribute in _ORIGINAL_META_PROPS:
            return ORIGINAL
    elif "itemprop" in elem.attrib:
        attribute = elem.get("itemprop", "").lower()
        if attribute in _UPDATED_ITEMPROPS:
            return UPDATED
        if attribute in _ORIGINAL_ITEMPROPS:
            return ORIGINAL
    elif "pubdate" in elem.attrib:
        if elem.get("pubdate", "").lower() == "pubdate":
            return ORIGINAL
    elif "http-equiv" in elem.attrib:
        attribute = elem.get("http-equiv", "").lower()
        if attribute == "last-modified":
            return UPDATED
        if attribute == "date":
            return ORIGINAL
    return None


def _meta_declarations(
    tree: HtmlElement,
    options: Extractor,
    observed_at: datetime,
    snapshot_id: str,
) -> list[DateDeclaration]:
    "Collect declarations carried by meta elements in the header."
    declarations: list[DateDeclaration] = []
    for elem in tree.iterfind(".//meta"):
        if "content" not in elem.attrib and "datetime" not in elem.attrib:
            continue
        role = _meta_role(elem)
        if role is None:
            continue
        raw = elem.get("content") or elem.get("datetime")
        LOGGER.debug("snapshot meta declaration: %s", logstring(elem))
        declarations.append(
            _make_declaration(
                role,
                try_date_expr_opts(raw, options),
                "meta",
                _tree_path(elem),
                raw,
                observed_at,
                snapshot_id,
            )
        )
    return declarations


def _json_declarations(
    tree: HtmlElement,
    options: Extractor,
    observed_at: datetime,
    snapshot_id: str,
) -> list[DateDeclaration]:
    "Collect datePublished/dateModified statements embedded in JSON blocks."
    declarations: list[DateDeclaration] = []
    for elem in _JSON_DATE_XPATH(tree):
        text = elem.text
        if not text or '"date' not in text:
            continue
        for pattern, role in (
            (JSON_PUBLISHED, ORIGINAL),
            (JSON_MODIFIED, UPDATED),
        ):
            for match in pattern.finditer(text):
                raw = match.group(1) + (match.group(2) or "")
                declarations.append(
                    _make_declaration(
                        role,
                        try_date_expr_opts(match.group(1), options),
                        "json-ld",
                        _tree_path(elem),
                        raw,
                        observed_at,
                        snapshot_id,
                    )
                )
    return declarations


def _time_declarations(
    tree: HtmlElement,
    options: Extractor,
    observed_at: datetime,
    snapshot_id: str,
) -> list[DateDeclaration]:
    "Collect statements carried by time elements."
    declarations: list[DateDeclaration] = []
    for elem in tree.findall(".//time"):
        datetime_attr = elem.get("datetime", "")
        class_attr = elem.get("class", "")
        if len(datetime_attr) > 6:
            if class_attr == "updated":
                role = UPDATED
            elif class_attr.startswith(_TIME_ORIGINAL_CLASSES) or elem.get(
                "pubdate"
            ) == "pubdate":
                role = ORIGINAL
            else:
                role = UNKNOWN
            raw = datetime_attr
        elif elem.text is not None and len(elem.text) > 6:
            role = UNKNOWN
            raw = elem.text
        else:
            continue
        declarations.append(
            _make_declaration(
                role,
                try_date_expr_opts(raw, options),
                "time",
                _tree_path(elem),
                raw,
                observed_at,
                snapshot_id,
            )
        )
    return declarations


def _heuristic_declarations(
    tree: HtmlElement,
    options: Extractor,
    observed_at: datetime,
    snapshot_id: str,
) -> list[DateDeclaration]:
    "Fall back on the full find_date() pipeline when no markup declaration exists."
    declarations: list[DateDeclaration] = []
    seen_values: set[str] = set()
    for role, original_flag in ((ORIGINAL, True), (UPDATED, False)):
        value = find_date(
            tree,
            extensive_search=options.extensive,
            original_date=original_flag,
            outputformat=options.format,
            min_date=options.min,
            max_date=options.max,
        )
        if value is not None and value not in seen_values:
            seen_values.add(value)
            declarations.append(
                _make_declaration(
                    role,
                    value,
                    "heuristic",
                    "full-document-search",
                    value,
                    observed_at,
                    snapshot_id,
                )
            )
    return declarations


def _body_fingerprint(tree: HtmlElement) -> str | None:
    """Hash the visible text of the article/main/body for change detection.

    Returns ``None`` when the snapshot has no comparable content container
    (e.g. the response was not an HTML document): an empty fingerprint would
    otherwise look like a content change against every real page.
    """
    articles = ([tree] if tree.tag == "article" else []) + tree.findall(
        ".//article"
    )
    if articles:
        elements = articles
    else:
        mains = ([tree] if tree.tag == "main" else []) + tree.findall(".//main")
        if mains:
            elements = mains
        else:
            elements = ([tree] if tree.tag == "body" else []) + tree.findall(
                ".//body"
            )
    if not elements:
        return None
    text = trim_text(" ".join(element.text_content() for element in elements))
    if not text:
        return None
    return sha256(text.encode("utf-8")).hexdigest()


def analyze_snapshot(
    htmlobject: bytes | str | HtmlElement,
    observed_at: datetime,
    snapshot_id: str,
    options: Extractor,
) -> SnapshotAnalysis:
    """Parse one snapshot and extract every traceable date declaration.

    Unparseable input does not raise: it is recorded with an empty
    declaration list and a ``None`` fingerprint so the caller can tell
    missing evidence from a page without dates.
    """
    tree = load_html(htmlobject)
    if tree is None:
        return SnapshotAnalysis(snapshot_id, observed_at, [], None, False)

    fingerprint = _body_fingerprint(tree)
    # a fragment without a content container is not evidence of what the
    # page declared: keep it out of the event diff and the content verdict
    parseable = fingerprint is not None

    declarations = (
        _meta_declarations(tree, options, observed_at, snapshot_id)
        + _json_declarations(tree, options, observed_at, snapshot_id)
        + _time_declarations(tree, options, observed_at, snapshot_id)
    )
    # structured markup exists but yielded nothing parseable: give the
    # full-document pipeline a chance instead of reporting an undated page
    if not any(declaration.value is not None for declaration in declarations):
        declarations.extend(
            _heuristic_declarations(tree, options, observed_at, snapshot_id)
        )
    return SnapshotAnalysis(
        snapshot_id=snapshot_id,
        observed_at=observed_at,
        declarations=declarations,
        content_fingerprint=fingerprint,
        parseable=parseable,
    )


def _declaration_key(declaration: DateDeclaration) -> tuple[str, str, str]:
    "Identity of a declaration: same role, markup kind and location."
    return declaration.role, declaration.source, declaration.locator


def _build_events(snapshots: list[SnapshotAnalysis]) -> list[DateEvent]:
    """Diff declaration locations across the ordered observations.

    Several declarations may share a location at one observation time;
    they are all kept so that same-time conflicts remain visible.
    """
    seen: dict[tuple[str, str, str], list[DateDeclaration]] = {}
    events: list[DateEvent] = []
    # an unparseable snapshot is missing evidence, not a page without dates:
    # it must not generate disappearance events for declarations it cannot show
    for snapshot in (item for item in snapshots if item.parseable):
        current: dict[tuple[str, str, str], list[DateDeclaration]] = {}
        for declaration in snapshot.declarations:
            key = _declaration_key(declaration)
            current.setdefault(key, []).append(declaration)
            previous = seen.get(key)
            if previous is None:
                events.append(
                    DateEvent(
                        EVENT_APPEARED,
                        snapshot.observed_at,
                        snapshot.snapshot_id,
                        declaration.role,
                        declaration.source,
                        declaration.locator,
                        None,
                        declaration.value,
                        declaration.raw,
                    )
                )
            elif declaration.value not in {item.value for item in previous}:
                events.append(
                    DateEvent(
                        EVENT_CHANGED,
                        snapshot.observed_at,
                        snapshot.snapshot_id,
                        declaration.role,
                        declaration.source,
                        declaration.locator,
                        previous[0].value,
                        declaration.value,
                        declaration.raw,
                    )
                )
        for key, previous in seen.items():
            if key not in current:
                events.append(
                    DateEvent(
                        EVENT_DISAPPEARED,
                        snapshot.observed_at,
                        snapshot.snapshot_id,
                        previous[0].role,
                        previous[0].source,
                        previous[0].locator,
                        previous[0].value,
                        None,
                        None,
                    )
                )
        # locations seen at this observation become the baseline
        seen = current
    return events


def _as_datetime(value: str, outputformat: str) -> datetime | None:
    "Parse a declaration value back for chronological comparison."
    try:
        return datetime.strptime(value, outputformat)
    except ValueError:
        return None


def _infer_trusted_dates(
    snapshots: list[SnapshotAnalysis],
    outputformat: str,
) -> dict[str, TrustedDate]:
    """Infer a trusted value per role from the raw declaration history.

    The newest declaration is never taken at face value: corrections and
    rollbacks are detected by comparing each change against the values seen
    before, and unparseable or conflicting statements are flagged instead
    of being silently overwritten.
    """
    # role -> parseable declarations in observation order; snapshots that
    # could not be compared still expose their raw declarations above, but
    # they cannot support or contradict an inferred trusted date
    histories: dict[str, list[DateDeclaration]] = {}
    for snapshot in (item for item in snapshots if item.parseable):
        for declaration in snapshot.declarations:
            if declaration.value is not None:
                histories.setdefault(declaration.role, []).append(declaration)

    result: dict[str, TrustedDate] = {}
    for role, declarations in histories.items():
        distinct = list(dict.fromkeys(item.value for item in declarations))
        first = declarations[0]
        last = declarations[-1]

        # conflicting values at one and the same observation time stay unresolved
        observed_groups: dict[datetime, set[str]] = {}
        for declaration in declarations:
            observed_groups.setdefault(declaration.observed_at, set()).add(
                declaration.value
            )
        if any(len(group) > 1 for group in observed_groups.values()):
            status = TRUSTED_CONFLICT
        elif len(distinct) == 1:
            status = TRUSTED_CONFIRMED
        elif last.value in distinct[:-1]:
            # a value seen earlier comes back: the update was rolled back
            status = TRUSTED_ROLLED_BACK
        else:
            # order chronologically, not lexicographically: a custom output
            # format such as %d.%m.%Y would otherwise misorder the values
            first_dt = _as_datetime(first.value, outputformat)
            last_dt = _as_datetime(last.value, outputformat)
            if first_dt is not None and last_dt is not None:
                status = (
                    TRUSTED_CORRECTED if last_dt < first_dt else TRUSTED_UNDETERMINED
                )
            elif last.value < first.value:
                # defensive fallback if the format cannot be reversed
                status = TRUSTED_CORRECTED
            else:
                status = TRUSTED_UNDETERMINED

        evidence = list(
            dict.fromkeys(
                declaration.snapshot_id for declaration in declarations
            )
        )
        result[role] = TrustedDate(
            role=role,
            value=last.value,
            status=status,
            first_seen=first.observed_at,
            last_seen=last.observed_at,
            evidence=evidence,
        )
    return result


def _content_verdict(
    snapshots: list[SnapshotAnalysis],
) -> tuple[bool | None, str]:
    """Compare body fingerprints and decide on an article update.

    An unparseable snapshot makes the comparison inconclusive, and an
    update is only asserted when the body text is known to differ.
    """
    if any(snapshot.content_fingerprint is None for snapshot in snapshots):
        content_changed: bool | None = None
    else:
        fingerprints = {
            snapshot.content_fingerprint for snapshot in snapshots
        }
        content_changed = len(fingerprints) > 1

    if content_changed is None:
        verdict = UPDATE_UNDETERMINED
    elif content_changed:
        verdict = UPDATE_YES
    else:
        verdict = UPDATE_NO
    return content_changed, verdict


def compare_snapshots(
    snapshots: list[tuple[str, datetime, bytes | str | HtmlElement]],
    *,
    extensive_search: bool = True,
    outputformat: str = "%Y-%m-%d",
    min_date: datetime | str | None = None,
    max_date: datetime | str | None = None,
) -> SnapshotHistory:
    """Compare several offline HTML snapshots of the same page.

    :param snapshots: list of ``(snapshot_id, observed_at, htmlobject)``
        triples; the observation time is when the snapshot was collected.
        Input may be out of order: it is sorted by observation time while
        snapshots sharing a time keep their input order.
    :param extensive_search: forward the corresponding ``find_date`` option
        to date expression parsing
    :param outputformat: date format used for every declaration value
    :param min_date: earliest acceptable date (ISO 8601 YMD or datetime)
    :param max_date: latest acceptable date (ISO 8601 YMD or datetime)
    :return: a :class:`SnapshotHistory` whose every event and inferred date
        references the snapshot(s) it comes from
    :raises TypeError: if an observation time is not a datetime
    :raises ValueError: if identifiers are duplicated or the list is empty
    """
    if not snapshots:
        raise ValueError("at least one snapshot is required")

    ids = [item[0] for item in snapshots]
    if len(set(ids)) != len(ids):
        raise ValueError("snapshot identifiers must be unique")
    for _, observed_at, _ in snapshots:
        if not isinstance(observed_at, datetime):
            raise TypeError("observed_at must be a datetime instance")
    if not is_valid_format(outputformat):
        raise ValueError(f"invalid output format: {outputformat}")

    # stable sort: same-time conflicts preserve input order
    ordered = sorted(snapshots, key=lambda item: item[1])

    options = Extractor(
        extensive=extensive_search,
        max=get_max_date(max_date),
        min=get_min_date(min_date),
        original=True,
        format=outputformat,
    )

    analyses = [
        analyze_snapshot(htmlobject, observed_at, snapshot_id, options)
        for snapshot_id, observed_at, htmlobject in ordered
    ]

    events = _build_events(analyses)
    trusted_dates = _infer_trusted_dates(analyses, outputformat)
    content_changed, verdict = _content_verdict(analyses)

    return SnapshotHistory(
        snapshots=analyses,
        events=events,
        trusted_dates=trusted_dates,
        content_changed=content_changed,
        article_updated=verdict,
    )
