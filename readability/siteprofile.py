"""Optional same-site template profiles for shared navigation noise.

A profile is learned from several reference pages of one site and records
block-level signatures (tag, id, class set, ARIA role) that are both
repeated across pages and independently evidenced as navigation. Merely
repeated text never qualifies.

Safety properties:

* repetition alone never qualifies a block: every occurrence must show
  navigation evidence (link-dominated text, a repeated list of link cards,
  or navigation vocabulary combined with link-heavy wording);
* a rule needs an identifiable signature (an id or at least one class), so
  generic containers such as a classless ``<ul>`` are never templated;
* a learned rule is re-validated against the target page before removal, so
  the same signature holding legitimate prose is kept;
* editorial containers (``article``, ``main``, content/post/body classes)
  and repeated article prose never produce rules;
* profiles carry the domain they were trained for and refuse cross-site use
  unless the caller opts in explicitly;
* training parses private copies of the reference pages and profiles are
  immutable data, so training material and extracted pages never rewrite
  each other.
"""
import copy
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from lxml.html import HtmlElement

from .htmls import build_doc

SCHEMA_VERSION = 1

# Block-level containers that can plausibly hold a site-wide template.
BLOCK_TAGS = frozenset(
    ["div", "section", "nav", "aside", "footer", "header", "ul", "ol", "form"]
)
# Editorial landmarks are never turned into removal rules.
PROTECTED_TAGS = frozenset(["html", "body", "main", "article"])

LINKED_ITEM_RATIO = 0.5

NAV_NOISE_RE = re.compile(
    r"related|recommend|recommendation|navigation|nav|menu|footer|sidebar|"
    r"sponsor|promo|popular|trending|suggest|you-might|also-read|outbrain|"
    r"taboola|widget|more-stories|more-from|recirc|read-next|most-read|"
    r"in-case-you-missed",
    re.I,
)
NAV_HEADING_RE = re.compile(
    r"related|recommend|you might like|more from|most read|most popular|"
    r"trending|explore more|read next|in case you missed|top stories|"
    r"suggested for you",
    re.I,
)
EDITORIAL_RE = re.compile(
    r"article|entry|hentry|main|content|post|body|blog|story|text|column",
    re.I,
)

DEFAULT_MIN_OCCURRENCES = 2
DEFAULT_MIN_LINK_DENSITY = 0.5
DEFAULT_MIN_LINK_RATIO = 0.5
DEFAULT_MIN_LINKS = 3
DEFAULT_MIN_TEXT_LENGTH = 25


def normalize_text(value):
    """Collapse whitespace the same way article text is normalized."""
    return " ".join((value or "").split())


def link_metrics(elem):
    """Return link character density, word ratio and anchor count.

    Character density is the share of visible text living inside anchors;
    word ratio is the same share counted in whitespace-separated tokens.
    Dense navigation blocks score close to one on both measures while
    recommendation cards with long teasers do not, which is why additional
    structural evidence is evaluated separately.
    """
    total_text = normalize_text(elem.text_content())
    anchors = elem.findall(".//a")
    link_text = " ".join(
        normalize_text(anchor.text_content()) for anchor in anchors
    )
    char_density = len(link_text) / max(len(total_text), 1)
    total_words = len(total_text.split())
    word_ratio = len(link_text.split()) / max(total_words, 1)
    return char_density, word_ratio, len(anchors)


def linked_child_ratio(elem):
    """Share of direct element children that contain an anchor.

    A row of recommendation cards or a navigation list is built from
    repeated child items, each wrapping its own link; prose sections do not
    have this structure even when they contain an occasional inline link.
    """
    children = [child for child in elem if isinstance(child.tag, str)]
    if not children:
        return 0.0, 0
    linked = sum(1 for child in children if child.findall(".//a"))
    return linked / len(children), len(children)


def _parse_reference(page):
    """Parse a reference page without touching the caller's document."""
    if isinstance(page, HtmlElement):
        raise TypeError(
            "SiteProfile.train() needs HTML strings or bytes so that "
            "reference pages are parsed into private copies; passing a live "
            "DOM would let training and extraction mutate each other."
        )
    doc, _ = build_doc(page)
    for tag in ("script", "style", "noscript"):
        for elem in doc.findall(".//{}".format(tag)):
            elem.drop_tree()
    for elem in doc.xpath("//*[@hidden]"):
        elem.drop_tree()
    return doc


def _features(elem):
    return "{} {}".format(elem.get("class", ""), elem.get("id", ""))


def _looks_editorial(elem):
    features = _features(elem)
    return bool(EDITORIAL_RE.search(features)) and not bool(
        NAV_NOISE_RE.search(features)
    )


def _has_navigation_vocabulary(elem):
    if NAV_NOISE_RE.search(_features(elem)) or NAV_NOISE_RE.search(
        elem.get("role") or ""
    ):
        return True
    for heading in elem.findall(".//h1|.//h2|.//h3|.//h4"):
        if NAV_HEADING_RE.search(normalize_text(heading.text_content())):
            return True
    return False


def _signature(elem):
    return (
        elem.tag,
        elem.get("id"),
        tuple(sorted((elem.get("class") or "").split())),
        elem.get("role"),
    )


class SiteProfile:
    """Immutable description of evidence-backed navigation templates.

    Construct via :meth:`train` or :meth:`load`; the constructor is only
    needed when rebuilding a profile from a serialized dictionary.
    """

    def __init__(
        self,
        rules=None,
        domain=None,
        min_occurrences=DEFAULT_MIN_OCCURRENCES,
        min_link_density=DEFAULT_MIN_LINK_DENSITY,
        min_link_ratio=DEFAULT_MIN_LINK_RATIO,
        min_links=DEFAULT_MIN_LINKS,
        min_text_length=DEFAULT_MIN_TEXT_LENGTH,
        source_page_count=0,
        created_at=None,
        schema_version=SCHEMA_VERSION,
    ):
        self.domain = domain.lower() if domain else None
        self.min_occurrences = min_occurrences
        self.min_link_density = min_link_density
        self.min_link_ratio = min_link_ratio
        self.min_links = min_links
        self.min_text_length = min_text_length
        self.source_page_count = source_page_count
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()
        self.schema_version = schema_version
        self._rules = [copy.deepcopy(rule) for rule in (rules or [])]

    @property
    def scope(self):
        """Human-readable declaration of where the profile may be used."""
        return self.domain or "unscoped (no domain restriction recorded)"

    def describe(self):
        """Explain the profile's scope and the evidence behind each rule."""
        lines = [
            "Site profile scope: {}".format(self.scope),
            "Trained from {} reference page(s); a block had to appear on at "
            "least {} page(s).".format(
                self.source_page_count, self.min_occurrences
            ),
            "Navigation evidence thresholds: link density >= {}, link word "
            "ratio >= {}, anchors >= {}, text length >= {}.".format(
                self.min_link_density,
                self.min_link_ratio,
                self.min_links,
                self.min_text_length,
            ),
            "Cross-site use is rejected unless explicitly enabled.",
            "{} rule(s):".format(len(self._rules)),
        ]
        for rule in self._rules:
            lines.append(
                "  - <{} id={!r} class={!r} role={!r}> on {}/{} pages; "
                "evidence: {}; sample: {!r}".format(
                    rule["tag"],
                    rule["id"],
                    " ".join(rule["class"]),
                    rule["role"],
                    rule["page_count"],
                    self.source_page_count,
                    ", ".join(rule["evidence"]),
                    rule["example_text"],
                )
            )
        return "\n".join(lines)

    def evaluate_navigation_evidence(self, elem):
        """Return evidence reasons when a block looks like navigation.

        ``None`` means the block must be kept. The same evaluation runs at
        training time and against every target-page match, so a template
        signature that has been repurposed for prose is not removed.
        """
        if not isinstance(elem.tag, str) or elem.tag not in BLOCK_TAGS:
            return None
        if elem.tag in PROTECTED_TAGS or _looks_editorial(elem):
            return None
        text = normalize_text(elem.text_content())
        if len(text) < self.min_text_length:
            return None
        char_density, word_ratio, anchor_count = link_metrics(elem)
        if anchor_count < self.min_links:
            return None

        reasons = []
        if (
            char_density >= self.min_link_density
            and word_ratio >= self.min_link_ratio
        ):
            reasons.append("link-dominated text")
        item_ratio, child_count = linked_child_ratio(elem)
        if child_count >= self.min_links and item_ratio >= LINKED_ITEM_RATIO:
            reasons.append("repeated link items")
        if _has_navigation_vocabulary(elem) and word_ratio >= self.min_link_ratio:
            reasons.append("navigation vocabulary")
        return reasons or None

    @classmethod
    def train(
        cls,
        pages,
        domain=None,
        min_occurrences=DEFAULT_MIN_OCCURRENCES,
        min_link_density=DEFAULT_MIN_LINK_DENSITY,
        min_link_ratio=DEFAULT_MIN_LINK_RATIO,
        min_links=DEFAULT_MIN_LINKS,
        min_text_length=DEFAULT_MIN_TEXT_LENGTH,
    ):
        """Learn a profile from reference pages of a single site.

        :param pages: iterable of HTML strings or bytes. Each page is parsed
            independently into a private document and never modified.
        :param domain: host name the profile is scoped to.
        :param min_occurrences: number of distinct reference pages a block
            must appear on before it becomes a rule.
        :param min_link_density: minimum anchor character share for the
            link-dominated-text evidence path.
        :param min_link_ratio: minimum anchor word share for density and
            vocabulary evidence paths.
        :param min_links: minimum number of anchors per occurrence.
        :param min_text_length: minimum visible text length per occurrence.
        """
        profile = cls(
            domain=domain,
            min_occurrences=min_occurrences,
            min_link_density=min_link_density,
            min_link_ratio=min_link_ratio,
            min_links=min_links,
            min_text_length=min_text_length,
        )
        observations = {}
        page_count = 0
        for page_number, page in enumerate(pages):
            page_count += 1
            doc = _parse_reference(page)
            for elem in doc.findall(".//*"):
                reasons = profile.evaluate_navigation_evidence(elem)
                if reasons is None:
                    continue
                key = _signature(elem)
                # Only identifiable templates are learnable: a generic tag
                # with neither id nor class could match unrelated prose.
                if not key[1] and not key[2]:
                    continue
                record = observations.get(key)
                if record is None:
                    record = {
                        "tag": key[0],
                        "id": key[1],
                        "class": list(key[2]),
                        "role": key[3],
                        "pages": set(),
                        "evidence": set(),
                        "example_text": normalize_text(
                            elem.text_content()
                        )[:200],
                    }
                    observations[key] = record
                record["pages"].add(page_number)
                record["evidence"].update(reasons)

        rules = []
        for record in observations.values():
            if len(record["pages"]) < min_occurrences:
                continue
            rules.append(
                {
                    "tag": record["tag"],
                    "id": record["id"],
                    "class": record["class"],
                    "role": record["role"],
                    "page_count": len(record["pages"]),
                    "evidence": sorted(record["evidence"]),
                    "example_text": record["example_text"],
                }
            )
        rules.sort(key=lambda rule: (rule["tag"], rule["id"] or "", rule["class"]))

        profile.source_page_count = page_count
        profile._rules = rules
        return profile

    @property
    def rules(self):
        """Copies of the learned rules, safe for callers to inspect."""
        return tuple(copy.deepcopy(rule) for rule in self._rules)

    def describes(self, url_or_host):
        """Whether this profile's scope covers the given URL or host."""
        if not url_or_host or not self.domain:
            return False
        host = url_or_host
        if "//" in host or host.startswith("/"):
            host = urlsplit(host).hostname or ""
        host = host.lower()
        return host == self.domain or host.endswith("." + self.domain)

    @staticmethod
    def signature_matches(elem, rule):
        """Exact signature match: tag, id, full class set and role.

        Matching is intentionally strict so that a rule only fires on the
        well-evidenced template it was learned from, never on loosely
        related containers.
        """
        if elem.tag != rule["tag"]:
            return False
        if (elem.get("id") or None) != (rule.get("id") or None):
            return False
        if tuple(sorted((elem.get("class") or "").split())) != tuple(
            rule["class"]
        ):
            return False
        if (elem.get("role") or None) != (rule.get("role") or None):
            return False
        return True

    def is_evidence_sufficient(self, elem):
        """Re-check navigation evidence against the target page itself."""
        return self.evaluate_navigation_evidence(elem) is not None

    def to_dict(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "domain": self.domain,
            "created_at": self.created_at,
            "source_page_count": self.source_page_count,
            "thresholds": {
                "min_occurrences": self.min_occurrences,
                "min_link_density": self.min_link_density,
                "min_link_ratio": self.min_link_ratio,
                "min_links": self.min_links,
                "min_text_length": self.min_text_length,
            },
            "rules": copy.deepcopy(self._rules),
        }

    @classmethod
    def from_dict(cls, data):
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                "Unsupported site profile schema version: {!r}".format(
                    data.get("schema_version")
                )
            )
        thresholds = data.get("thresholds", {})
        return cls(
            rules=data.get("rules", []),
            domain=data.get("domain"),
            min_occurrences=thresholds.get(
                "min_occurrences", DEFAULT_MIN_OCCURRENCES
            ),
            min_link_density=thresholds.get(
                "min_link_density", DEFAULT_MIN_LINK_DENSITY
            ),
            min_link_ratio=thresholds.get(
                "min_link_ratio", DEFAULT_MIN_LINK_RATIO
            ),
            min_links=thresholds.get("min_links", DEFAULT_MIN_LINKS),
            min_text_length=thresholds.get(
                "min_text_length", DEFAULT_MIN_TEXT_LENGTH
            ),
            source_page_count=data.get("source_page_count", 0),
            created_at=data.get("created_at"),
            schema_version=data["schema_version"],
        )

    def save(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def __repr__(self):
        return (
            "SiteProfile(domain={!r}, rules={}, source_page_count={})".format(
                self.domain, len(self._rules), self.source_page_count
            )
        )
