# Formula preservation for readability summaries.
#
# Recognizes MathML markup and the alternative text pages provide for
# formulas (alttext attributes, <annotation> elements, fallback images or
# visually hidden spans). Each formula is kept at its original inline or
# block position. The visual layer and the assistive layer of the same
# formula are merged so the formula is counted only once in the extracted
# text; when two layers cannot be matched reliably, both are kept and the
# formula is flagged with an uncertainty marker. No page scripts are
# executed and no rendering engine is involved.
import re

FALLBACK_FEATURES_RE = re.compile(
    r"mathjax[_-]?preview|mwe-math-fallback|math[_-]?fallback|"
    r"katex-html|sr-only|screen-reader|visually-hidden|assistive",
    re.I,
)

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
    "figure", "figcaption", "footer", "form", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "li", "main", "nav", "p", "pre", "section", "td", "th",
}

TEX_ENCODINGS = ("application/x-tex", "application/x-latex", "tex", "latex")


def normalize_formula_text(text):
    return re.sub(r"\s+", "", text or "")


def _math_alttext(math_elem):
    alttext = (math_elem.get("alttext") or "").strip()
    tex_text = None
    any_text = None
    for annotation in math_elem.findall(".//annotation"):
        text = (annotation.text_content() or "").strip()
        if not text:
            continue
        encoding = (annotation.get("encoding") or "").strip().lower()
        if encoding in TEX_ENCODINGS and tex_text is None:
            tex_text = text
        if any_text is None:
            any_text = text
    return alttext or tex_text or any_text or None


def _drop_annotations(math_elem):
    # <annotation> is by definition an alternate encoding of the enclosing
    # formula, so it reliably corresponds to the visual MathML layer. Its
    # text is lifted into data-alttext and the element is removed to avoid
    # counting the same formula twice in the extracted text.
    for annotation in math_elem.findall(".//annotation"):
        annotation.drop_tree()


def _outermost_formula_wrapper(math_elem):
    node = math_elem
    parent = node.getparent()
    while parent is not None and parent.tag not in ("html", "body"):
        siblings = [child for child in parent if child is not node]
        text = (parent.text or "") + "".join(child.tail or "" for child in parent)
        if siblings or text.strip():
            break
        node = parent
        parent = parent.getparent()
    return node


def _is_fallback_candidate(elem):
    if elem.tag == "img":
        return bool((elem.get("alt") or "").strip())
    if (elem.get("aria-hidden") or "").strip().lower() == "true":
        return True
    features = "{} {}".format(elem.get("class", ""), elem.get("id", ""))
    return bool(FALLBACK_FEATURES_RE.search(features))


def _candidate_text(elem):
    if elem.tag == "img":
        return elem.get("alt") or ""
    return elem.text_content() or ""


def _formula_display(math_elem, wrapper):
    if (math_elem.get("display") or "").strip().lower() == "block":
        return "block"
    parent = wrapper.getparent()
    if parent is not None and parent.tag in BLOCK_TAGS:
        parent_text = normalize_formula_text(parent.text_content())
        math_text = normalize_formula_text(math_elem.text_content())
        if math_text and parent_text == math_text:
            return "block"
    return "inline"


def _deduplicate_fallbacks(math_elem, wrapper, alttext):
    alt_norm = normalize_formula_text(alttext)
    math_norm = normalize_formula_text(math_elem.text_content())
    uncertain = False
    for sibling in (wrapper.getprevious(), wrapper.getnext()):
        if sibling is None or not _is_fallback_candidate(sibling):
            continue
        candidate_norm = normalize_formula_text(_candidate_text(sibling))
        if candidate_norm and (
            candidate_norm == alt_norm or candidate_norm == math_norm
        ):
            sibling.drop_tree()
        else:
            # A formula-flavoured layer we cannot match: keep both
            # representations and flag the correspondence as uncertain.
            uncertain = True
    if uncertain:
        math_elem.set("data-readability-formula-uncertain", "true")


def prepare_formulas(root):
    for math_elem in root.findall(".//math"):
        alttext = _math_alttext(math_elem)
        _drop_annotations(math_elem)
        if alttext:
            math_elem.set("data-alttext", alttext)
        wrapper = _outermost_formula_wrapper(math_elem)
        math_elem.set(
            "data-readability-formula", _formula_display(math_elem, wrapper)
        )
        _deduplicate_fallbacks(math_elem, wrapper, alttext)
