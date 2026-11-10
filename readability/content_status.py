"""Content status detection for extracted article content.

Paywall pages often let readability extract a couple of text paragraphs even
though the full article is not accessible.  Downstream consumers must not
mistake that partial extraction for the complete article.  This module only
describes the content that was actually obtained; it never attempts to bypass
an access restriction.

The status is derived exclusively from explicit structural markers.  In
particular:

* a short body is never treated as evidence of truncation on its own;
* subscription promotional copy does not prove that a page is restricted;
* when structured markers contradict each other, every marker is preserved on
  the result and the status falls back to ``unknown``.

Markers carry a strength.  Standardized machine markers (JSON-LD, microdata,
content-tier meta tags, ``data-*`` access attributes) and explicit access
vocabulary such as ``paywall`` are *strong* and decide the state on their own.
Ambiguous class/id tokens (``preview`` cards, ``login`` navigation, video
caption widgets) and password inputs are *weak*: they are always recorded, but
only decide the state when no substantial article body was extracted.
"""
import json
import re

import lxml.html


class ContentStatus:
    """Supported content states.

    ``UNKNOWN``
        No explicit marker allows a completeness claim.  This state never
        guarantees that the content is complete.
    ``PREVIEW``
        The page explicitly marks the extracted text as a preview or teaser of
        content that continues elsewhere.
    ``RESTRICTED``
        The page explicitly states that access is limited by a paywall,
        registration wall, or similar restriction.
    ``NO_CONTENT``
        No article body text could be extracted.
    """

    UNKNOWN = "unknown"
    PREVIEW = "preview"
    RESTRICTED = "restricted"
    NO_CONTENT = "no_content"


CONTENT_STATES = frozenset(
    [
        ContentStatus.UNKNOWN,
        ContentStatus.PREVIEW,
        ContentStatus.RESTRICTED,
        ContentStatus.NO_CONTENT,
    ]
)

RESTRICTED = "restricted"
FREE = "free"
PREVIEW = "preview"
REGISTRATION = "registration"

STRONG = "strong"
WEAK = "weak"

# Explicit access-control vocabulary: reliable even on otherwise full pages.
STRONG_RESTRICTED_TOKENS = frozenset(
    ["paywall", "paywalled", "metered", "gated"]
)
# Ambiguous tokens that also appear in ordinary site chrome; they may only
# decide the state when no substantial article body was extracted.
WEAK_RESTRICTED_TOKENS = frozenset(
    ["premium", "paid", "subscriber", "subscribers", "locked"]
)
PREVIEW_TOKENS = frozenset(
    ["preview", "teaser", "truncated", "partial", "freemium"]
)
REGISTRATION_TOKENS = frozenset(
    ["login", "signin", "register", "registration"]
)

BOOLEAN_RESTRICTED_ATTRIBUTES = frozenset(
    [
        "data-paywall",
        "data-metered",
        "data-gated",
        "data-locked",
        "data-restricted",
    ]
)
TIER_ATTRIBUTES = frozenset(
    [
        "data-access",
        "data-content-tier",
        "data-contenttier",
        "data-membership",
        "data-tier",
        "data-portal",
    ]
)

META_TIER_RE = re.compile(r"^(?:article:)?content[-_]?tier$", re.I)
META_RIGHTS_NAMES = frozenset(
    ["dcterms.accessrights", "dc.rights", "accessrights", "access"]
)
RIGHTS_RESTRICTED_RE = re.compile(
    r"subscri|paid|premium|metered|restricted|registered|members", re.I
)
RIGHTS_FREE_RE = re.compile(r"free|open\s*access", re.I)
CAMEL_CASE_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+"
)
MAX_SIGNALS = 200


def _tokens(value):
    if not value:
        return []
    tokens = []
    for part in re.split(r"[^A-Za-z0-9]+", value):
        tokens.extend(
            match.group(0) for match in CAMEL_CASE_RE.finditer(part)
        )
    return [token.lower() for token in tokens if token]


def _describe_element(elem):
    if not isinstance(elem.tag, str):
        return str(elem.tag)
    name = elem.tag
    element_id = elem.get("id")
    if element_id:
        name += "#" + element_id
    classes = (elem.get("class") or "").split()
    if classes:
        name += "." + ".".join(classes[:3])
    return name[:120]


def _restricted_boolean_indicator(value):
    """Marker where a truthy value (or bare presence) means restricted."""
    if isinstance(value, bool):
        return RESTRICTED if value else FREE
    text = str(value).strip().lower()
    if text == "":
        return RESTRICTED
    if text in ("true", "1", "yes", "y"):
        return RESTRICTED
    if text in ("false", "0", "no", "n"):
        return FREE
    return None


def _free_boolean_indicator(value):
    """Boolean mapping where ``true`` means freely accessible."""
    if isinstance(value, bool):
        return FREE if value else RESTRICTED
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y"):
        return FREE
    if text in ("false", "0", "no", "n"):
        return RESTRICTED
    return None


def _tier_indicator(value):
    tokens = set(_tokens(str(value).lower()))
    if (
        tokens & STRONG_RESTRICTED_TOKENS
        or tokens & WEAK_RESTRICTED_TOKENS
        or tokens & {"subscription", "members", "registered"}
    ):
        return RESTRICTED
    if tokens & PREVIEW_TOKENS:
        return PREVIEW
    if tokens & REGISTRATION_TOKENS:
        return REGISTRATION
    if tokens & {"free", "open"}:
        return FREE
    return None


class ContentStatusResult:
    """Outcome of :meth:`readability.Document.content_status`.

    :ivar state: one of the :class:`ContentStatus` constants.
    :ivar signals: structured markers observed on the page, each a dictionary
        with ``source``, ``signal``, ``value``, ``indicates`` and
        ``strength`` keys.  Markers are retained even when they contradict
        each other or are too weak to decide the state.
    :ivar contradictory_signals: ``True`` when both restricted and free
        access markers are present.
    :ivar body_text_length: plain-text length of the extracted summary.  The
        value is descriptive and is never used as truncation evidence.
    """

    def __init__(self, state, signals, contradictory_signals, body_text_length):
        self.state = state
        self.signals = signals
        self.contradictory_signals = contradictory_signals
        self.body_text_length = body_text_length

    def as_dict(self):
        return {
            "state": self.state,
            "signals": list(self.signals),
            "contradictory_signals": self.contradictory_signals,
            "body_text_length": self.body_text_length,
        }

    def __repr__(self):
        return (
            "ContentStatusResult(state={!r}, signals={}, "
            "contradictory_signals={!r}, body_text_length={})".format(
                self.state,
                len(self.signals),
                self.contradictory_signals,
                self.body_text_length,
            )
        )

    def __eq__(self, other):
        if not isinstance(other, ContentStatusResult):
            return NotImplemented
        return self.as_dict() == other.as_dict()


def _make_recorder():
    seen = set()
    signals = []

    def add(source, signal, value, indicates, strength=STRONG):
        key = (source, signal, value, indicates, strength)
        if key in seen or len(signals) >= MAX_SIGNALS:
            return
        seen.add(key)
        signals.append(
            {
                "source": source,
                "signal": signal,
                "value": value,
                "indicates": indicates,
                "strength": strength,
            }
        )

    return add, signals


def collect_structural_signals(doc):
    """Collect paywall-related markers from a parsed, unmodified document."""
    add, signals = _make_recorder()

    for meta in doc.findall(".//meta"):
        key = (
            meta.get("name")
            or meta.get("property")
            or meta.get("itemprop")
            or ""
        ).strip().lower()
        content = (meta.get("content") or "").strip()
        if not key or not content:
            continue
        if META_TIER_RE.match(key):
            indicator = _tier_indicator(content)
            if indicator:
                add("meta", key, content, indicator)
        elif key in META_RIGHTS_NAMES:
            if RIGHTS_RESTRICTED_RE.search(content):
                add("meta", key, content, RESTRICTED)
            elif RIGHTS_FREE_RE.search(content):
                add("meta", key, content, FREE)

    for elem in doc.findall(".//*[@itemprop]"):
        item_props = (elem.get("itemprop") or "").lower().split()
        text = " ".join((elem.text_content() or "").split())
        if not text:
            text = (elem.get("content") or "").strip()
        for item_prop in item_props:
            if item_prop == "isaccessibleforfree":
                indicator = _free_boolean_indicator(text)
                if indicator:
                    add(
                        "microdata",
                        "isAccessibleForFree",
                        text,
                        indicator,
                    )
            elif item_prop == "paywall":
                indicator = _restricted_boolean_indicator(text)
                if indicator:
                    add("microdata", "paywall", text, indicator)

    for elem in doc.iter():
        if not isinstance(elem.tag, str):
            continue
        descriptor = _describe_element(elem)

        for attribute, value in elem.attrib.items():
            attribute = attribute.lower()
            if attribute in BOOLEAN_RESTRICTED_ATTRIBUTES:
                indicator = _restricted_boolean_indicator(value)
                if indicator:
                    add("data-attribute", attribute, value, indicator)
            elif attribute in TIER_ATTRIBUTES:
                indicator = _tier_indicator(value)
                if indicator:
                    add("data-attribute", attribute, value, indicator)

        for attribute_name in ("class", "id"):
            attribute_value = elem.get(attribute_name)
            if not attribute_value:
                continue
            for token in _tokens(attribute_value):
                label = "{}:{}".format(attribute_name, token)
                if token in STRONG_RESTRICTED_TOKENS:
                    add(
                        "element", label, descriptor, RESTRICTED, STRONG
                    )
                elif token in WEAK_RESTRICTED_TOKENS:
                    add(
                        "element", label, descriptor, RESTRICTED, WEAK
                    )
                elif token in PREVIEW_TOKENS:
                    add("element", label, descriptor, PREVIEW, WEAK)
                elif token in REGISTRATION_TOKENS:
                    add(
                        "element", label, descriptor, REGISTRATION, WEAK
                    )

    for input_elem in doc.findall(".//input[@type]"):
        if (input_elem.get("type") or "").strip().lower() == "password":
            add(
                "form",
                "password-input",
                _describe_element(input_elem),
                REGISTRATION,
                WEAK,
            )

    return signals


def collect_jsonld_signals(blocks):
    """Collect markers from ``application/ld+json`` script blocks."""
    add, signals = _make_recorder()

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                next_path = "{}.{}".format(path, key)
                indicator = None
                if key == "isAccessibleForFree":
                    indicator = _free_boolean_indicator(value)
                elif key == "paywall":
                    indicator = _restricted_boolean_indicator(value)
                elif key in ("contentTier", "content_tier"):
                    indicator = _tier_indicator(value)
                if indicator is not None and not isinstance(
                    value, (dict, list)
                ):
                    signal_name = (
                        next_path[2:]
                        if next_path.startswith("$.")
                        else next_path
                    )
                    add(
                        "json-ld",
                        signal_name,
                        str(value).lower(),
                        indicator,
                    )
                walk(value, next_path)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, "{}[{}]".format(path, index))

    for raw in blocks:
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        walk(data, "$")

    return signals


def summary_text_length(summary_html):
    """Plain-text length of a readability summary HTML string."""
    if not summary_html:
        return 0
    fragment = lxml.html.fromstring(summary_html)
    return len(" ".join((fragment.text_content() or "").split()))


def determine_content_status(
    signals, body_text_length, min_text_length, retry_length
):
    """Map extracted markers and body length to a content state.

    Length is only used to tell whether any body text was obtained, never as
    truncation evidence: short, marker-free content stays ``unknown`` instead
    of being called truncated, and wall chrome on a login page is not mistaken
    for an article body.  Weak markers only decide the state below
    ``retry_length``; strong markers decide it at any length.  A page is
    considered empty below ``min_text_length``.
    """
    strong = [signal for signal in signals if signal["strength"] == STRONG]
    weak = [signal for signal in signals if signal["strength"] == WEAK]

    def has(collection, indication):
        return any(signal["indicates"] == indication for signal in collection)

    strong_restricted = has(strong, RESTRICTED)
    strong_free = has(strong, FREE)
    weak_restricted = has(weak, RESTRICTED)
    weak_preview = has(weak, PREVIEW)
    weak_registration = has(weak, REGISTRATION)

    contradictory = (strong_restricted or weak_restricted) and (
        strong_free or has(weak, FREE)
    )
    has_any_text = body_text_length >= min_text_length
    has_substantial_text = body_text_length >= retry_length

    if not has_any_text:
        if strong_restricted or weak_restricted or weak_registration:
            state = ContentStatus.RESTRICTED
        else:
            state = ContentStatus.NO_CONTENT
        return state, contradictory

    if contradictory:
        return ContentStatus.UNKNOWN, True

    if strong_restricted:
        return ContentStatus.RESTRICTED, contradictory

    if has(strong, PREVIEW):
        return ContentStatus.PREVIEW, contradictory

    if has(strong, REGISTRATION):
        return ContentStatus.RESTRICTED, contradictory

    if has_substantial_text:
        # Weak class/id markers cannot override a substantial extracted body:
        # teaser cards, login links and video-caption widgets are common on
        # freely accessible pages.
        return ContentStatus.UNKNOWN, contradictory

    if weak_restricted or weak_registration:
        state = ContentStatus.RESTRICTED
    elif weak_preview:
        state = ContentStatus.PREVIEW
    else:
        state = ContentStatus.UNKNOWN
    return state, contradictory


def build_content_status(
    structural_signals,
    json_ld_blocks,
    summary_html,
    min_text_length,
    retry_length,
):
    """Combine structural and JSON-LD markers into a status result."""
    jsonld_signals = collect_jsonld_signals(json_ld_blocks)

    add, signals = _make_recorder()
    for signal in structural_signals + jsonld_signals:
        add(
            signal["source"],
            signal["signal"],
            signal["value"],
            signal["indicates"],
            signal["strength"],
        )

    body_text_length = summary_text_length(summary_html)
    state, contradictory = determine_content_status(
        signals,
        body_text_length,
        min_text_length,
        retry_length,
    )
    return ContentStatusResult(
        state, signals, contradictory, body_text_length
    )


def analyze_content_status(
    doc,
    json_ld_blocks,
    summary_html,
    min_text_length,
    retry_length,
):
    """Build the full content status result for one parsed document.

    The document must be parsed before cleaning, otherwise lxml removes the
    script blocks and part of the attributes that carry the markers.
    """
    return build_content_status(
        collect_structural_signals(doc),
        json_ld_blocks,
        summary_html,
        min_text_length,
        retry_length,
    )
