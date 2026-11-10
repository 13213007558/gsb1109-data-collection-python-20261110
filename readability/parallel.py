"""Detection and merging of parallel desktop/mobile article representations.

Some pages embed two complete copies of the same article (typically a desktop
and a mobile variant toggled through CSS) inside the same HTML document.  The
generic scoring pipeline then keeps both copies, so the extracted body
contains every paragraph twice.

The functions in this module collapse a duplicate only when two containers
can be positively identified as parallel representations of the same
article: a long, mostly one-to-one aligned sequence of matching blocks.
Repeated quotes, headings or table rows that legitimately occur inside a
single article never reach that threshold and are therefore left untouched.

When the correspondence cannot be confirmed, the pair is reported as a
candidate instead of being mutated.  Paragraphs that only exist in one
variant are moved into source-labelled wrappers rather than discarded, and
the retained base variant is chosen by document order (or an explicit
desktop marker), never by length.
"""
import re
from difflib import SequenceMatcher

from lxml.html import fragment_fromstring


BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "li", "pre", "td", "th", "caption", "dt", "dd",
}
CONTAINER_TAGS = {"div", "section", "article", "main"}

MIN_CONTAINER_LENGTH = 250
MIN_BLOCK_ITEMS = 4
MIN_MATCHED_PAIRS = 3
MIN_DISTINCT_MATCHES = 3
ANCHORED_SIMILARITY_THRESHOLD = 0.95
LENGTH_RATIO_LIMIT = 1.8
LABELED_LENGTH_RATIO_LIMIT = 3.0
SHORT_TEXT = 40
NEAR_COMPLETE_COVERAGE = 0.85
CANDIDATE_COVERAGE = 0.5
LABELED_COVERAGE_FLOOR = 0.35
MAX_FUZZY_CELLS = 400

MOBILE_RE = re.compile(
    r"(?:^|[\s_-])(?:mobile|smartphone|handheld|amp)(?:$|[\s_-])", re.I
)
DESKTOP_RE = re.compile(
    r"(?:^|[\s_-])(?:desktop|fullsite|full-site)(?:$|[\s_-])", re.I
)
WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text):
    return WHITESPACE_RE.sub(" ", text or "").strip()


def _descriptor(elem):
    name = elem.tag
    if elem.get("id"):
        name += "#" + elem.get("id")
    classes = (elem.get("class") or "").split()
    if classes:
        name += "." + ".".join(classes)
    return name


def _text_length(elem):
    return len(_normalize(elem.text_content()))


def _variant_label(elem):
    features = "{} {}".format(elem.get("class", ""), elem.get("id", ""))
    if MOBILE_RE.search(features):
        return "mobile"
    if DESKTOP_RE.search(features):
        return "desktop"
    return None


def _block_items(container):
    """Outermost block-level descendants of a container, in document order."""
    items = []
    for elem in container.iter():
        if elem is container or not isinstance(elem.tag, str):
            continue
        if elem.tag not in BLOCK_TAGS:
            continue
        if any(
            ancestor is not container
            and isinstance(ancestor.tag, str)
            and ancestor.tag in BLOCK_TAGS
            for ancestor in elem.iterancestors()
        ):
            continue
        items.append(elem)
    return items


def _block_text(elem):
    return _normalize(elem.text_content())


def _text_similarity(first_text, second_text, first_len, second_len):
    if not first_text or not second_text:
        return 0.0
    if first_text == second_text:
        return 1.0
    if first_len < SHORT_TEXT or second_len < SHORT_TEXT:
        return 0.0
    if (
        first_len > second_len * LENGTH_RATIO_LIMIT
        or second_len > first_len * LENGTH_RATIO_LIMIT
    ):
        return 0.0
    return SequenceMatcher(
        None, first_text, second_text, autojunk=False
    ).ratio()


def _exact_anchors(first_texts, second_texts):
    """Order-preserving pairs of blocks with identical text.

    Exact equality is trusted even for short blocks such as headings; fuzzy
    matching elsewhere keeps the length guard.
    """
    second_positions = {}
    for j, text in enumerate(second_texts):
        if text:
            second_positions.setdefault(text, []).append(j)
    anchors = []
    last_j = -1
    for i, text in enumerate(first_texts):
        if not text:
            continue
        j = next(
            (
                position
                for position in second_positions.get(text, [])
                if position > last_j
            ),
            None,
        )
        if j is not None:
            anchors.append((i, j))
            last_j = j
    return anchors


def _build_similarity_matrix(
    first_texts, second_texts, anchors, fuzzy_budget=MAX_FUZZY_CELLS
):
    """Exact anchors plus fuzzy matches strictly enclosed by two anchors."""
    n = len(first_texts)
    m = len(second_texts)
    matrix = [[0.0] * m for _ in range(n)]
    for i, j in anchors:
        matrix[i][j] = 1.0

    for span in range(len(anchors) - 1):
        start_i, start_j = anchors[span]
        end_i, end_j = anchors[span + 1]
        if end_i - start_i <= 1 or end_j - start_j <= 1:
            continue
        for i in range(start_i + 1, end_i):
            first_len = len(first_texts[i])
            if first_len < SHORT_TEXT:
                continue
            for j in range(start_j + 1, end_j):
                second_len = len(second_texts[j])
                if second_len < SHORT_TEXT:
                    continue
                if (
                    first_len > second_len * LENGTH_RATIO_LIMIT
                    or second_len > first_len * LENGTH_RATIO_LIMIT
                ):
                    continue
                if fuzzy_budget <= 0:
                    break
                fuzzy_budget -= 1
                score = _text_similarity(
                    first_texts[i], second_texts[j], first_len, second_len
                )
                if score >= ANCHORED_SIMILARITY_THRESHOLD:
                    matrix[i][j] = score
    return matrix


def _align_blocks(first_items, second_items):
    """Monotonic alignment of two block sequences.

    Exact text matches act as anchors; fuzzy matches are only accepted in the
    window between two anchors.  Returns the matched pairs, the blocks unique
    to each side, the number of distinct matched texts, and the per-side text
    coverage.
    """
    first_texts = [_block_text(item) for item in first_items]
    second_texts = [_block_text(item) for item in second_items]

    anchors = _exact_anchors(first_texts, second_texts)
    if len(anchors) < MIN_MATCHED_PAIRS:
        return None

    similarity = _build_similarity_matrix(first_texts, second_texts, anchors)
    n = len(first_items)
    m = len(second_items)

    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = max(dp[i - 1][j], dp[i][j - 1])
            if similarity[i - 1][j - 1]:
                best = max(
                    best,
                    dp[i - 1][j - 1] + similarity[i - 1][j - 1],
                )
            dp[i][j] = best

    pairs = []
    unique_first = []
    unique_second = []
    i, j = n, m
    while i > 0 and j > 0:
        if similarity[i - 1][j - 1] and abs(
            dp[i][j]
            - dp[i - 1][j - 1]
            - similarity[i - 1][j - 1]
        ) < 1e-9:
            pairs.append((first_items[i - 1], second_items[j - 1]))
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            unique_first.append(first_items[i - 1])
            i -= 1
        else:
            unique_second.append(second_items[j - 1])
            j -= 1
    unique_first.extend(reversed(first_items[:i]))
    unique_second.extend(reversed(second_items[:j]))
    pairs.reverse()
    unique_first.reverse()
    unique_second.reverse()

    first_index = {id(elem): pos for pos, elem in enumerate(first_items)}
    second_index = {id(elem): pos for pos, elem in enumerate(second_items)}
    distinct = {
        first_texts[first_index[id(first)]] for first, _ in pairs
    }
    first_total = sum(len(text) for text in first_texts) or 1
    second_total = sum(len(text) for text in second_texts) or 1
    first_covered = sum(
        len(first_texts[first_index[id(first)]]) for first, _ in pairs
    )
    second_covered = sum(
        len(second_texts[second_index[id(second)]])
        for _, second in pairs
    )
    coverage = {
        "primary": first_covered / first_total,
        "secondary": second_covered / second_total,
    }
    return {
        "pairs": pairs,
        "unique_first": unique_first,
        "unique_second": unique_second,
        "distinct_matches": len(distinct),
        "coverage": coverage,
    }


class ParallelVariant:
    """A pair of containers believed to represent the same article."""

    def __init__(self, primary, secondary, alignment, confident, reason):
        self.primary = primary
        self.secondary = secondary
        self.pairs = alignment["pairs"]
        self.unique_primary = alignment["unique_first"]
        self.unique_secondary = alignment["unique_second"]
        self.distinct_matches = alignment["distinct_matches"]
        self.coverage = alignment["coverage"]
        self.confident = confident
        self.reason = reason
        self.primary_label = _variant_label(primary) or "primary"
        self.secondary_label = _variant_label(secondary) or "secondary"


def _share_frame(first, second):
    if first.getparent() is second.getparent():
        return True
    first_ancestors = tuple(first.iterancestors())
    common = next(
        (ancestor for ancestor in second.iterancestors()
         if ancestor in first_ancestors),
        None,
    )
    if common is None:
        return False
    return common.tag in ("article", "main", "body")


def _candidate_containers(root):
    containers = []
    for elem in root.iter():
        if not isinstance(elem.tag, str) or elem.tag not in CONTAINER_TAGS:
            continue
        if _text_length(elem) < MIN_CONTAINER_LENGTH:
            continue
        items = _block_items(elem)
        if len(items) < MIN_BLOCK_ITEMS:
            continue
        containers.append((elem, items))
    return containers


def _order_variants(first, second):
    """Choose the base variant by label or document order, never length."""
    if _variant_label(first) == "desktop":
        return first, second
    if _variant_label(second) == "desktop":
        return second, first
    if _variant_label(first) == "mobile" and _variant_label(second) != "mobile":
        return second, first
    return first, second


def _reorder_alignment(alignment):
    return {
        "pairs": [
            (second, first) for first, second in alignment["pairs"]
        ],
        "unique_first": alignment["unique_second"],
        "unique_second": alignment["unique_first"],
        "distinct_matches": alignment["distinct_matches"],
        "coverage": {
            "primary": alignment["coverage"]["secondary"],
            "secondary": alignment["coverage"]["primary"],
        },
    }


def find_parallel_variants(root):
    """Find parallel article variants without mutating the document."""
    containers = _candidate_containers(root)
    findings = []
    for index, (first, first_items) in enumerate(containers):
        first_ancestors = set(first.iterancestors())
        first_length = _text_length(first)
        for second, second_items in containers[index + 1:]:
            if second in first_ancestors or first in set(
                second.iterancestors()
            ):
                continue
            if not _share_frame(first, second):
                continue
            first_label = _variant_label(first)
            second_label = _variant_label(second)
            explicit_labels = (
                first_label in ("desktop", "mobile")
                and second_label in ("desktop", "mobile")
                and first_label != second_label
            )
            second_length = _text_length(second)
            length_limit = (
                LABELED_LENGTH_RATIO_LIMIT
                if explicit_labels
                else LENGTH_RATIO_LIMIT
            )
            if (
                first_length > second_length * length_limit
                or second_length > first_length * length_limit
            ):
                continue
            alignment = _align_blocks(first_items, second_items)
            if alignment is None:
                continue
            pairs = alignment["pairs"]
            if len(pairs) < MIN_MATCHED_PAIRS:
                continue
            if alignment["distinct_matches"] < MIN_DISTINCT_MATCHES:
                continue
            coverage = alignment["coverage"]
            count_coverage_first = len(pairs) / len(first_items)
            count_coverage_second = len(pairs) / len(second_items)
            min_count_coverage = min(
                count_coverage_first, count_coverage_second
            )
            min_text_coverage = min(coverage.values())

            near_complete = (
                min_count_coverage >= NEAR_COMPLETE_COVERAGE
                and min_text_coverage >= NEAR_COMPLETE_COVERAGE
            )

            if explicit_labels:
                if (
                    len(pairs) <= MIN_MATCHED_PAIRS
                    or min_count_coverage < LABELED_COVERAGE_FLOOR
                    or min_text_coverage < LABELED_COVERAGE_FLOOR
                ):
                    continue
                confident = True
                reason = (
                    "explicit desktop/mobile labels"
                    if near_complete
                    else "explicit desktop/mobile labels with partial overlap"
                )
            else:
                if (
                    min_count_coverage < CANDIDATE_COVERAGE
                    or min_text_coverage < CANDIDATE_COVERAGE
                ):
                    continue
                confident = near_complete
                reason = (
                    "near-complete block alignment"
                    if confident
                    else "partial overlap without confirmed parallel labels"
                )

            primary, secondary = _order_variants(first, second)
            if primary is not first:
                alignment = _reorder_alignment(alignment)
            findings.append(
                ParallelVariant(
                    primary, secondary, alignment, confident, reason
                )
            )

    findings.sort(
        key=lambda variant: (
            variant.confident,
            len(variant.pairs),
            min(variant.coverage.values()),
        ),
        reverse=True,
    )
    return _select_disjoint(findings)


def _select_disjoint(findings):
    selected = []
    used = set()
    for variant in findings:
        if variant.primary in used or variant.secondary in used:
            continue
        if used.intersection(variant.primary.iterancestors()) or used.intersection(
            variant.secondary.iterancestors()
        ):
            continue
        selected.append(variant)
        used.add(variant.primary)
        used.add(variant.secondary)
    return selected


def _make_source_wrapper(label):
    return fragment_fromstring(
        '<div data-readability-variant-source="{}"/>'.format(label)
    )


def _document_order_index(root):
    return {elem: index for index, elem in enumerate(root.iter())}


def _move_unique_blocks(variant, order_index):
    """Reparent secondary-only blocks next to their aligned position."""
    pair_positions = sorted(
        (order_index[secondary], primary)
        for primary, secondary in variant.pairs
    )
    ordered_unique = sorted(
        variant.unique_secondary, key=lambda elem: order_index[elem]
    )

    group = []
    preceding = None

    def flush():
        if not group:
            return
        following = next(
            (
                primary
                for position, primary in pair_positions
                if position > order_index[group[-1]]
            ),
            None,
        )
        wrapper = _make_source_wrapper(variant.secondary_label)
        if preceding is not None:
            preceding.addnext(wrapper)
        elif following is not None:
            following.addprevious(wrapper)
        else:
            variant.primary.insert(0, wrapper)
        for block in group:
            wrapper.append(block)
        group.clear()

    for block in ordered_unique:
        position = order_index[block]
        current_preceding = None
        for pair_position, primary in pair_positions:
            if pair_position < position:
                current_preceding = primary
            else:
                break
        if current_preceding is not preceding:
            flush()
            preceding = current_preceding
        group.append(block)
    flush()


def _move_remaining_content(variant):
    """Preserve media and loose text the secondary carries outside blocks."""
    leftovers = []
    for child in list(variant.secondary):
        if child.getparent() is None:
            continue
        has_media = bool(
            child.xpath(
                ".//img | .//figure | .//svg | .//video | .//audio"
            )
        )
        if has_media or _text_length(child) >= MIN_CONTAINER_LENGTH:
            leftovers.append(child)
    if not leftovers:
        return
    wrapper = _make_source_wrapper(variant.secondary_label)
    variant.primary.append(wrapper)
    for child in leftovers:
        wrapper.append(child)


def merge_parallel_variants(root):
    """Merge confirmed parallel variants and report every detected pair.

    Confident pairs are collapsed into the primary variant: matched blocks of
    the secondary are dropped, secondary-only blocks are moved into
    source-labelled wrappers, and the secondary container is removed.
    Unconfirmed pairs are returned as candidates without touching the DOM.
    """
    reports = []
    for variant in find_parallel_variants(root):
        if not variant.confident:
            reports.append(serialize_variant(variant, status="candidate"))
            continue

        order_index = _document_order_index(root)
        for _, secondary_block in variant.pairs:
            if secondary_block.getparent() is not None:
                secondary_block.drop_tree()
        _move_unique_blocks(variant, order_index)
        _move_remaining_content(variant)

        variant.primary.set(
            "data-readability-parallel-merge",
            "{}+{}".format(
                variant.primary_label, variant.secondary_label
            ),
        )
        if variant.secondary.getparent() is not None:
            variant.secondary.drop_tree()
        reports.append(serialize_variant(variant, status="merged"))
    return reports


def _preview(elem):
    text = _normalize(elem.text_content())
    if len(text) <= 60:
        return text
    return text[:57] + "..."


def serialize_variant(variant, status):
    return {
        "status": status,
        "reason": variant.reason,
        "primary": {
            "label": variant.primary_label,
            "descriptor": _descriptor(variant.primary),
        },
        "secondary": {
            "label": variant.secondary_label,
            "descriptor": _descriptor(variant.secondary),
        },
        "matched_blocks": len(variant.pairs),
        "distinct_matched_blocks": variant.distinct_matches,
        "coverage": variant.coverage,
        "unique_primary": [
            _preview(block) for block in variant.unique_primary
        ],
        "unique_secondary": [
            _preview(block) for block in variant.unique_secondary
        ],
    }
