"""Stitching of multi-page articles from already downloaded pages.

Paginated web articles usually repeat the end of the previous page at the
beginning of the next one, so naively concatenating extracted pages produces
duplicate paragraphs (and splits a paragraph when the page break lands in the
middle of one).  This module accepts pages that have *already* been downloaded
by the caller -- it never performs network requests itself -- and stitches them
in the page order given by the caller.

Only boundary overlaps supported by textual evidence are removed.  Identical
quotations that appear in the middle of the body are deliberately preserved,
and missing pages, conflicting titles and joins that cannot be determined are
reported as issues instead of being guessed away.
"""

import re
from collections import namedtuple
from copy import deepcopy

from lxml.etree import tostring
from lxml.html import Element
from lxml.html import fragment_fromstring

from .readability import Document
from .readability import clean


# Issue kinds reported on :class:`StitchedArticle`.
MISSING_PAGE = "missing_page"
CONFLICTING_TITLE = "conflicting_title"
UNCERTAIN_JOIN = "uncertain_join"

# Boundary relations recorded for every adjacent pair of pages.
OVERLAP = "overlap"
CONTINUATION = "continuation"
UNCERTAIN = "uncertain"

# A full-paragraph overlap must be at least this long to count as evidence.
MIN_OVERLAP_CHARS = 20
# Two boundary paragraphs longer than this may be treated as the same
# paragraph once their length difference is at most this drift (ellipsis,
# minor editorial edits between page renders).
NEAR_EQUAL_DRIFT = 12
# Minimum shared prefix fraction for drift that is not a pure truncation.
NEAR_EQUAL_PREFIX_RATIO = 0.85
# A shared prefix must reach this length and share this fraction of the
# shorter paragraph before it is treated as a split paragraph.
MIN_CONTINUATION_CHARS = 12
MIN_CONTINUATION_RATIO = 0.34

NO_TITLE = "[no-title]"

PAGE_SUFFIX_RE = re.compile(
    r"\s*[-–|:/]?\s*page\s+\d+(?:\s+of\s+\d+)?\s*$", re.IGNORECASE
)
BLOCK_TAGS = (
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "blockquote", "li", "pre", "td",
)
TEXT_BLOCK_TAGS = ("p", "blockquote", "li", "pre", "td")


class DownloadedPage(
    namedtuple("DownloadedPage", ["page_id", "html", "url"], defaults=(None,))
):
    """One downloaded page supplied by the caller.

    :param page_id: identifier used for provenance and gap detection.
    :param html: raw HTML downloaded earlier; the module never fetches it.
    :param url: optional source URL, kept for provenance only.
    """


class ArticleSegment(
    namedtuple("ArticleSegment", ["text", "source_page", "elem"])
):
    """A retained paragraph.

    ``source_page`` is a single page id or a tuple of page ids when the
    paragraph was rebuilt from a paragraph split across a page boundary.
    """


class BoundaryIssue(namedtuple("BoundaryIssue", ["kind", "between", "detail"])):
    """An unresolved problem.

    :param kind: one of :data:`MISSING_PAGE`, :data:`CONFLICTING_TITLE` and
        :data:`UNCERTAIN_JOIN`.
    :param between: tuple of the two page ids the issue sits between, or the
        pair of pages whose titles disagree.
    :param detail: human-readable explanation of the evidence found.
    """


class StitchedArticle(
    namedtuple(
        "StitchedArticle",
        [
            "title",
            "page_order",
            "titles",
            "paragraphs",
            "boundaries",
            "issues",
            "merged_html",
        ],
    )
):
    """Result of stitching several downloaded pages into one article."""

    def reading_order(self):
        """Final reading order as ``(source_page, text)`` tuples."""
        return [
            (segment.source_page, segment.text) for segment in self.paragraphs
        ]

    def plain_text(self):
        """All retained paragraphs joined by blank lines."""
        return "\n\n".join(segment.text for segment in self.paragraphs)

    def unresolved_positions(self):
        """Paragraph indices next to which an unresolved boundary sits."""
        return [
            {
                "position": boundary["position"],
                "between": boundary["between"],
                "reason": boundary["reason"],
            }
            for boundary in self.boundaries
            if boundary["relation"] == UNCERTAIN
        ]


Block = namedtuple("Block", ["tag", "text", "norm", "links", "elem"])


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def _normalize_title(title):
    if not title or title == NO_TITLE:
        return ""
    return _normalize(PAGE_SUFFIX_RE.sub("", title))


def _coerce_page(page):
    if isinstance(page, DownloadedPage):
        return page
    if isinstance(page, dict):
        return DownloadedPage(
            page["page_id"], page["html"], page.get("url")
        )
    if isinstance(page, (tuple, list)):
        page_id, html = page[0], page[1]
        url = page[2] if len(page) > 2 else None
        return DownloadedPage(page_id, html, url)
    raise TypeError(
        "pages must be DownloadedPage instances, dicts or tuples, got %r"
        % type(page)
    )


def _extract_blocks(html, url, document_factory):
    doc = document_factory(html, url=url)
    title = doc.title()
    title_norm = _normalize_title(title)
    summary = doc.summary(html_partial=True)
    root = fragment_fromstring(summary)
    tag_test = " or ".join("self::%s" % tag for tag in BLOCK_TAGS)
    candidates = root.xpath(".//*[%s]" % tag_test)
    selected = set(candidates)
    blocks = []
    for elem in candidates:
        # Skip e.g. <p> nested inside a selected <blockquote>.
        if any(ancestor in selected for ancestor in elem.iterancestors()):
            continue
        text = clean(elem.text_content() or "")
        if not text:
            continue
        # A heading repeating the page title is boilerplate shown on every
        # page; the title itself is kept on the stitched result.
        if title_norm and elem.tag in ("h1", "h2") and _normalize(text) == title_norm:
            continue
        links = tuple(
            anchor.get("href")
            for anchor in elem.findall(".//a")
            if anchor.get("href")
        )
        blocks.append(
            Block(elem.tag, text, _normalize(text), links, elem)
        )
    return title, blocks


def _exact_block(left, right):
    if left.norm != right.norm:
        return False
    # Same wording with different link targets are distinct paragraphs; the
    # boundary itself is then reported as unresolved rather than deduplicated.
    return left.links == right.links


def _near_equal_block(left, right):
    """Paragraphs equal apart from minor drift between page renders."""
    if left.links != right.links:
        return False
    if len(left.norm) < MIN_OVERLAP_CHARS or len(right.norm) < MIN_OVERLAP_CHARS:
        return False
    if abs(len(left.norm) - len(right.norm)) > NEAR_EQUAL_DRIFT:
        return False
    shorter, longer = sorted((left.norm, right.norm), key=len)
    if shorter in longer:
        return True
    shared_prefix = _common_prefix_length(left.norm, right.norm)
    return shared_prefix >= len(shorter) * NEAR_EQUAL_PREFIX_RATIO


def _common_prefix_length(left, right):
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _is_continuation(left_block, right_block):
    if left_block.tag not in TEXT_BLOCK_TAGS:
        return None
    if right_block.tag not in TEXT_BLOCK_TAGS:
        return None
    if left_block.links != right_block.links:
        return None
    left_norm = left_block.norm
    right_norm = right_block.norm
    shared = _common_prefix_length(left_norm, right_norm)
    if shared < MIN_CONTINUATION_CHARS:
        return None
    if shared < min(len(left_norm), len(right_norm)) * MIN_CONTINUATION_RATIO:
        return None
    if len(left_norm) < len(right_norm) and right_norm.startswith(left_norm):
        return "right"
    if len(right_norm) < len(left_norm) and left_norm.startswith(right_norm):
        return "left"
    return None


def _detect_boundary(left_blocks, right_blocks):
    """Decide how two adjacent pages join, based on textual evidence only."""
    # 1. Whole-paragraph overlap: a suffix of the previous page is repeated
    #    verbatim as a prefix of the next one. Try the longest shared
    #    sequence first so a multi-paragraph repeat is skipped in one go.
    max_match = min(len(left_blocks), len(right_blocks))
    for match_count in range(max_match, 0, -1):
        if not all(
            _exact_block(
                left_blocks[len(left_blocks) - match_count + offset],
                right_blocks[offset],
            )
            for offset in range(match_count)
        ):
            continue
        matched_chars = sum(
            len(left_blocks[len(left_blocks) - match_count + offset].norm)
            for offset in range(match_count)
        )
        if matched_chars < MIN_OVERLAP_CHARS:
            # The longest match already carries too little evidence; a
            # shorter one can only carry less, so stop searching.
            break
        return {
            "relation": OVERLAP,
            "skip_right": match_count,
            "matched_paragraphs": match_count,
            "matched_chars": matched_chars,
            "reason": "tail of previous page repeats at the top of the next page",
        }

    # 2. Split paragraph: the same text starts both sides and one side holds
    #    the complete paragraph while the other holds a fragment of it.
    #    Runs before fuzzy overlap matching so a half-paragraph fragment is
    #    merged (keeping the complete side) instead of dropped as a repeat.
    if left_blocks and right_blocks:
        complete_side = _is_continuation(left_blocks[-1], right_blocks[0])
        if complete_side:
            return {
                "relation": CONTINUATION,
                "skip_right": 1,
                "complete_side": complete_side,
                "matched_chars": _common_prefix_length(
                    left_blocks[-1].norm, right_blocks[0].norm
                ),
                "reason": (
                    "paragraph split by the page break is completed on the "
                    "next page"
                    if complete_side == "right"
                    else "next page repeats the opening fragment of an "
                    "already complete paragraph"
                ),
            }

    # 3. Fuzzy whole-paragraph overlap: same paragraph with minor editorial
    #    drift between the two page renders.
    if left_blocks and right_blocks and _near_equal_block(
        left_blocks[-1], right_blocks[0]
    ):
        return {
            "relation": OVERLAP,
            "skip_right": 1,
            "matched_paragraphs": 1,
            "matched_chars": min(
                len(left_blocks[-1].norm), len(right_blocks[0].norm)
            ),
            "reason": "boundary paragraphs match apart from minor editorial "
            "drift between the page renders",
        }

    # 4. No evidence either way: keep everything and flag the join.
    if (
        left_blocks
        and right_blocks
        and left_blocks[-1].norm == right_blocks[0].norm
        and left_blocks[-1].links != right_blocks[0].links
    ):
        reason = (
            "identical paragraph text points to different link targets; it "
            "cannot be proven to be a boundary repeat"
        )
    else:
        reason = (
            "no overlapping paragraph or shared paragraph prefix "
            "found at the page boundary"
        )
    return {
        "relation": UNCERTAIN,
        "skip_right": 0,
        "reason": reason,
    }


def _segment(block, source_page):
    return ArticleSegment(block.text, source_page, block.elem)


def _merged_segment(left_block, right_block, left_id, right_id):
    text = right_block.text
    elem = Element(right_block.tag)
    elem.text = text
    return ArticleSegment(text, (left_id, right_id), elem)


def _source_attribute(source_page):
    if isinstance(source_page, tuple):
        return ",".join(str(identifier) for identifier in source_page)
    return str(source_page)


def _build_merged_html(page_order, paragraphs):
    container = fragment_fromstring(
        '<div class="stitched-article" data-page-order="%s"></div>'
        % ",".join(str(identifier) for identifier in page_order)
    )
    for segment in paragraphs:
        elem = deepcopy(segment.elem)
        elem.set("data-source-page", _source_attribute(segment.source_page))
        container.append(elem)
    return tostring(container, encoding="unicode")


def stitch_article(pages, document_factory=Document):
    """Stitch already downloaded article pages in caller-given order.

    :param pages: ordered iterable of :class:`DownloadedPage` instances, or
        ``(page_id, html)`` / ``(page_id, html, url)`` tuples and equivalent
        dicts.  Page order is taken from the iterable and is never reordered.
    :param document_factory: callable used to build the per-page
        :class:`~readability.Document`; defaults to :class:`Document`.
    :returns: :class:`StitchedArticle` with provenance per paragraph and
        explicit issues for every boundary that could not be resolved.
    :raises ValueError: on an empty page list or duplicate page ids.
    """
    normalized_pages = [_coerce_page(page) for page in pages]
    if not normalized_pages:
        raise ValueError("at least one downloaded page is required")

    page_order = tuple(page.page_id for page in normalized_pages)
    if len(set(page_order)) != len(page_order):
        raise ValueError("duplicate page ids in %r" % (page_order,))

    extracted = [
        _extract_blocks(page.html, page.url, document_factory)
        for page in normalized_pages
    ]
    titles = {
        page.page_id: title
        for page, (title, _) in zip(normalized_pages, extracted)
    }
    page_blocks = [blocks for _, blocks in extracted]

    issues = []

    available_titles = [
        (page_id, title)
        for page_id, title in titles.items()
        if _normalize_title(title)
    ]
    if available_titles:
        first_id, first_title = available_titles[0]
        baseline = _normalize_title(first_title)
        chosen_title = first_title
        for page_id, title in available_titles[1:]:
            if _normalize_title(title) != baseline:
                issues.append(
                    BoundaryIssue(
                        CONFLICTING_TITLE,
                        (first_id, page_id),
                        "titles disagree: %r vs %r" % (first_title, title),
                    )
                )
    else:
        chosen_title = NO_TITLE

    gap_pairs = set()
    integer_ids = [
        identifier
        for identifier in page_order
        if isinstance(identifier, int) and not isinstance(identifier, bool)
    ]
    if len(integer_ids) == len(page_order):
        for left_id, right_id in zip(page_order, page_order[1:]):
            if right_id - left_id > 1:
                gap_pairs.add((left_id, right_id))
                issues.append(
                    BoundaryIssue(
                        MISSING_PAGE,
                        (left_id, right_id),
                        "pages %s are absent between pages %s and %s"
                        % (
                            ", ".join(
                                str(missing)
                                for missing in range(left_id + 1, right_id)
                            ),
                            left_id,
                            right_id,
                        ),
                    )
                )

    boundaries = []
    for index in range(len(normalized_pages) - 1):
        left_page = normalized_pages[index]
        right_page = normalized_pages[index + 1]
        between = (left_page.page_id, right_page.page_id)
        if between in gap_pairs:
            decision = {
                "relation": UNCERTAIN,
                "skip_right": 0,
                "reason": "gap in the page sequence; boundary evidence "
                "cannot be trusted",
            }
        else:
            decision = _detect_boundary(
                page_blocks[index], page_blocks[index + 1]
            )
        boundaries.append(
            {
                "between": between,
                "relation": decision["relation"],
                "skip_right": decision["skip_right"],
                "reason": decision["reason"],
                "detail": {
                    key: value
                    for key, value in decision.items()
                    if key not in ("relation", "skip_right", "reason")
                },
            }
        )
        if decision["relation"] == UNCERTAIN:
            issues.append(
                BoundaryIssue(UNCERTAIN_JOIN, between, decision["reason"])
            )

    paragraphs = []
    for index, (page, blocks) in enumerate(
        zip(normalized_pages, page_blocks)
    ):
        if index == 0:
            paragraphs.extend(_segment(block, page.page_id) for block in blocks)
            continue

        boundary = boundaries[index - 1]
        start = boundary["skip_right"]
        boundary["position"] = len(paragraphs)

        if (
            boundary["relation"] == CONTINUATION
            and boundary["detail"].get("complete_side") == "right"
        ):
            paragraphs[-1] = _merged_segment(
                page_blocks[index - 1][-1],
                blocks[0],
                normalized_pages[index - 1].page_id,
                page.page_id,
            )
        paragraphs.extend(
            _segment(block, page.page_id) for block in blocks[start:]
        )

    merged_html = _build_merged_html(page_order, paragraphs)
    return StitchedArticle(
        title=chosen_title,
        page_order=page_order,
        titles=titles,
        paragraphs=paragraphs,
        boundaries=boundaries,
        issues=issues,
        merged_html=merged_html,
    )
