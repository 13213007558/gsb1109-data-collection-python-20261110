"""Tests for CSS selector match explanations."""

from __future__ import annotations

import pytest
from cssselect.parser import SelectorSyntaxError
from hypothesis import given, settings
from hypothesis import strategies as st
from lxml import etree

from parsel import Selector, UnsupportedSelectorSyntax
from parsel.cssmatch import (
    POSITION_CONDITION,
    RELATION_CONDITION,
    SELF_CONDITION,
    explain_css_match,
)

CATALOG = """
<html>
<body>
<section class="catalog">
  <h2>Products</h2>
  <ul>
    <li><span class="name">a</span><span class="price">10</span></li>
    <li><span class="name">b</span><span class="price">20</span></li>
    <li><span class="name">c</span><span class="price">30</span></li>
  </ul>
</section>
</body>
</html>
"""

# The headline selector combining an ancestor chain, an adjacent sibling
# combinator and an nth-child position condition.
HEADLINE_CSS = "section.catalog > ul > li:nth-child(2) + li span.price"


@pytest.fixture
def catalog() -> Selector:
    return Selector(text=CATALOG)


def _all_elements(selector: Selector) -> list[Selector]:
    return list(selector.xpath("//*"))


def _assert_aligned_with_engine(
    selector: Selector, css: str, *, document_type: str = "html"
) -> None:
    matched_nodes = {matched.root for matched in selector.css(css)}
    for node in _all_elements(selector):
        explanation = explain_css_match(css, node.root, document_type=document_type)
        assert explanation.matched == (node.root in matched_nodes), css


def test_headline_success_witness(catalog: Selector) -> None:
    target = catalog.css("span.price")[2]
    explanation = target.css_match_explanation(HEADLINE_CSS)

    assert explanation.matched is True
    assert target.root in {node.root for node in catalog.css(HEADLINE_CSS)}
    assert explanation.failure_reasons == ()
    branch = explanation.branches[0]
    assert branch.matched is True
    assert branch.failure_reasons == ()
    # Every individual check passes on a successful witness.
    assert all(
        check.passed for compound in branch.compounds for check in compound.checks
    )
    assert all(combinator.passed for combinator in branch.combinators)
    # The three requirement categories are all represented.
    categories = {
        check.category for compound in branch.compounds for check in compound.checks
    }
    assert categories == {SELF_CONDITION, POSITION_CONDITION}
    assert {combinator.combinator for combinator in branch.combinators} == {
        ">",
        "+",
        " ",
    }


def test_headline_missing_adjacent_sibling(catalog: Selector) -> None:
    # The first price's li has no preceding sibling, so the "+" requirement
    # can never hold.
    target = catalog.css("span.price")[0]
    explanation = target.css_match_explanation(HEADLINE_CSS)

    assert explanation.matched is False
    assert target.root not in {node.root for node in catalog.css(HEADLINE_CSS)}
    sibling_checks = [
        combinator
        for combinator in explanation.branches[0].combinators
        if combinator.combinator == "+"
    ]
    assert sibling_checks
    assert sibling_checks[0].passed is False
    assert "adjacent sibling" in sibling_checks[0].detail
    assert any("adjacent sibling" in reason for reason in explanation.failure_reasons)


def test_headline_position_condition_failure(catalog: Selector) -> None:
    # The second price sits in the first li: its adjacent sibling exists but
    # is not the second child, so :nth-child(2) is the missing requirement.
    target = catalog.css("span.price")[1]
    explanation = target.css_match_explanation(HEADLINE_CSS)

    assert explanation.matched is False
    nth_checks = [
        check
        for compound in explanation.branches[0].compounds
        for check in compound.checks
        if check.expression == ":nth-child(2)" and check.category == POSITION_CONDITION
    ]
    assert nth_checks
    assert nth_checks[0].passed is False
    assert "1 element sibling position" in nth_checks[0].detail
    assert any("nth-child(2)" in reason for reason in explanation.failure_reasons)


def test_self_conditions_separately_reported() -> None:
    selector = Selector(text='<div id="x" class="a b" data-k="v"><p>text</p></div>')
    div = selector.css("div")[0]
    explanation = div.css_match_explanation("div#y.c[data-k='w']:nth-child(5)")
    branch = explanation.branches[0]
    by_expression = {
        check.expression: check
        for compound in branch.compounds
        for check in compound.checks
    }
    assert by_expression["div"].passed is True
    assert by_expression["#y"].passed is False
    assert by_expression[".c"].passed is False
    assert by_expression["[data-k='w']"].passed is False
    nth = by_expression[":nth-child(5)"]
    assert nth.category == POSITION_CONDITION
    assert nth.passed is False
    assert len(explanation.failure_reasons) == 4


def test_node_conditions_distinct_from_relations(catalog: Selector) -> None:
    target = catalog.css("span.price")[0]
    explanation = target.css_match_explanation("section.nope > span.price")
    branch = explanation.branches[0]
    # The node's own span.price conditions hold ...
    own = [
        check
        for compound in branch.compounds
        if compound.expression in {"span", "span.price"}
        for check in compound.checks
    ]
    assert own
    assert all(check.passed for check in own)
    # ... while the ancestor relationship is the reported failure.
    assert any(
        not combinator.passed and combinator.combinator == ">"
        for combinator in branch.combinators
    )


def test_selector_list_branches_explained_separately() -> None:
    selector = Selector(text="<div><p class='a'>1</p><span>2</span></div>")
    paragraph = selector.css("p")[0]
    explanation = paragraph.css_match_explanation("div.x, p.a, span.z")

    assert explanation.matched is True
    assert [branch.selector for branch in explanation.branches] == [
        "div.x",
        "p.a",
        "span.z",
    ]
    statuses = {branch.selector: branch.matched for branch in explanation.branches}
    assert statuses == {"div.x": False, "p.a": True, "span.z": False}
    # Aggregated reasons only mention the failing branches.
    joined = "\n".join(explanation.failure_reasons)
    assert "div.x" in joined
    assert "span.z" in joined
    assert "p.a" not in joined


def test_general_and_adjacent_sibling_relations() -> None:
    selector = Selector(
        text="<ul><li>1</li><li class='k'>2</li><li>3</li><li class='t'>4</li></ul>"
    )
    target = selector.css("li.t")[0]
    assert target.css_match_explanation("li.k ~ li.t").matched is True
    missing = target.css_match_explanation("li.missing ~ li.t")
    assert missing.matched is False
    assert any("general sibling" in reason for reason in missing.failure_reasons)


def test_ancestor_backtracking_does_not_report_false_failure() -> None:
    selector = Selector(
        text="<div class='top'><div class='mid'><p class='x'>deep</p></div></div>"
    )
    paragraph = selector.css("p.x")[0]
    # The nearest ancestor (.mid) fails .top, a farther one matches: the
    # explanation must backtrack instead of concluding a failure.
    explanation = paragraph.css_match_explanation(".top p.x")
    assert explanation.matched is True
    assert explanation.failure_reasons == ()


def test_nth_series_and_structural_pseudos() -> None:
    selector = Selector(text="<ul><li><b>x</b></li><li>i2</li><li>i3</li></ul>")
    list_items = selector.css("li")
    assert list_items[1].css_match_explanation("li:nth-child(2)").matched
    assert list_items[2].css_match_explanation("li:nth-child(odd)").matched
    assert list_items[0].css_match_explanation("li:first-child").matched
    assert list_items[2].css_match_explanation("li:last-child").matched
    assert selector.css("b")[0].css_match_explanation("b:only-of-type").matched
    assert list_items[1].css_match_explanation("li:nth-of-type(2)").matched
    assert not list_items[0].css_match_explanation("li:nth-child(2)").matched


def test_nth_last_child_counts_from_end() -> None:
    selector = Selector(text="<ul><li>1</li><li>2</li><li>3</li></ul>")
    first = selector.css("li")[0]
    explanation = first.css_match_explanation("li:nth-last-child(3)")
    assert explanation.matched is True
    check = next(
        check
        for compound in explanation.branches[0].compounds
        for check in compound.checks
        if check.category == POSITION_CONDITION
    )
    assert "3 element sibling position" in check.detail


def test_attribute_operators_aligned(catalog: Selector) -> None:
    for css in (
        "[class]",
        "[class^='pr']",
        "[class$='ce']",
        "[class*='ric']",
        "[class~='price']",
        "[class='price']",
        "[class!='other']",
    ):
        _assert_aligned_with_engine(catalog, css)


def test_case_insensitive_attribute_flag() -> None:
    selector = Selector(text="<a data-v='AbC'>x</a>")
    anchor = selector.css("a")[0]
    assert anchor.css_match_explanation("[data-v='abc' i]").matched is True
    assert anchor.css_match_explanation("[data-v='abc' s]").matched is False


def test_negation_matching_where_and_relation() -> None:
    selector = Selector(
        text="<div><section><h2>t</h2></section><p class='x'>a</p><p>b</p></div>"
    )
    for css in (
        "p:not(.x)",
        "div:has(section)",
        "section:has(> h2)",
        "p:is(.x)",
        "p:where(.x)",
        "section:has(h2, p)",
    ):
        _assert_aligned_with_engine(selector, css)


def test_never_matching_pseudos_report_failure() -> None:
    selector = Selector(text="<a href='x'>l</a>")
    anchor = selector.css("a")[0]
    explanation = anchor.css_match_explanation("a:hover")
    check = next(
        check
        for compound in explanation.branches[0].compounds
        for check in compound.checks
        if check.expression == ":hover"
    )
    assert check.passed is False
    assert "never matches" in check.detail


def test_engine_backed_pseudos_aligned() -> None:
    selector = Selector(
        text="<form><input disabled><input type='checkbox' checked>"
        "<a href='x'>l</a></form>"
    )
    for css in (":disabled", ":checked", "a:link", ":enabled"):
        _assert_aligned_with_engine(selector, css)


def test_lang_pseudo_aligned() -> None:
    selector = Selector(text="<html lang='fr-MC'><body><p>x</p></body></html>")
    for css in ("html:lang(fr)", "p:lang(fr)", "p:lang(en)"):
        _assert_aligned_with_engine(selector, css)


def test_contains_pseudo() -> None:
    selector = Selector(text="<p>Hello World</p>")
    paragraph = selector.css("p")[0]
    assert paragraph.css_match_explanation("p:contains('Hello')").matched
    assert not paragraph.css_match_explanation("p:contains('bye')").matched


def test_pseudo_elements_recorded_but_not_evaluated_as_nodes() -> None:
    selector = Selector(text="<p class='x'>text</p>")
    paragraph = selector.css("p")[0]
    text_explanation = paragraph.css_match_explanation("p.x::text")
    assert text_explanation.matched is True
    assert text_explanation.branches[0].pseudo_element == "::text"
    attr_explanation = paragraph.css_match_explanation("p::attr(class)")
    assert attr_explanation.branches[0].pseudo_element == "::attr(class)"


def test_scope_pseudo_uses_selector_root() -> None:
    selector = Selector(text="<div><p>1</p><p>2</p></div>")
    div = selector.css("div")[0]
    first_paragraph = div.css("p")[0]
    # The scope of a nested selector is the node it wraps, mirroring the
    # engine's div.css(":scope > p") result for the child paragraphs.
    assert (
        explain_css_match(":scope > p", first_paragraph.root, scope=div.root).matched
        is True
    )
    # Explaining the div itself fails because the target type of the
    # selector is p, and the child relationship cannot start from the node.
    assert div.css_match_explanation(":scope > p").matched is False
    # A paragraph wrapping itself is its own scope and is not a child of
    # itself, matching the engine result for paragraph.css(":scope > p").
    assert first_paragraph.css_match_explanation(":scope > p").matched is False
    assert len(first_paragraph.css(":scope > p")) == 0


def test_xml_mode_is_case_sensitive() -> None:
    selector = Selector(text="<Root><Item>1</Item><ITEM>2</ITEM></Root>", type="xml")
    _assert_aligned_with_engine(selector, "Item", document_type="xml")
    item = selector.css("Item")[0]
    explanation = item.css_match_explanation("ITEM")
    assert explanation.matched is False
    assert "expected element type 'ITEM'" in explanation.failure_reasons[0]


def test_explanation_does_not_modify_document(catalog: Selector) -> None:
    before = etree.tostring(catalog.root)
    for node in _all_elements(catalog):
        node.css_match_explanation(
            "section.catalog > ul > li:nth-child(9) + li span.missing"
        )
    assert etree.tostring(catalog.root) == before


def test_selector_list_returns_one_explanation_per_node(catalog: Selector) -> None:
    prices = catalog.css("span.price")
    explanations = prices.css_match_explanation("span.price")
    assert len(explanations) == len(prices)
    assert all(explanation.matched for explanation in explanations)
    assert [explanation.node for explanation in explanations] == [
        explanation.node for explanation in explanations
    ]


def test_unsupported_syntax_is_rejected_without_guessing() -> None:
    selector = Selector(text="<p class='x'>t</p>")
    paragraph = selector.css("p")[0]
    for css in (
        "p:unknown-pseudo",
        "p::before",
        "*:nth-of-type(2)",
        "p:nth-child(foo)",
    ):
        with pytest.raises(UnsupportedSelectorSyntax):
            paragraph.css_match_explanation(css)


def test_namespaced_selectors_are_rejected() -> None:
    selector = Selector(
        text='<root xmlns:ns="http://x"><ns:p>t</ns:p></root>', type="xml"
    )
    paragraph = selector.xpath("//ns:p", namespaces={"ns": "http://x"})[0]
    with pytest.raises(UnsupportedSelectorSyntax):
        paragraph.css_match_explanation("ns|p")


def test_invalid_grammar_raises_selector_syntax_error() -> None:
    selector = Selector(text="<p>t</p>")
    paragraph = selector.css("p")[0]
    with pytest.raises(SelectorSyntaxError):
        paragraph.css_match_explanation("p >> div")


def test_non_element_and_non_markup_selectors_are_rejected() -> None:
    selector = Selector(text="<p class='x'>t</p>")
    with pytest.raises(ValueError, match="element node"):
        selector.css("p::text")[0].css_match_explanation("p")
    with pytest.raises(ValueError, match="element node"):
        selector.css("p::attr(class)")[0].css_match_explanation("p")

    json_selector = Selector(text='{"a": 1}')
    with pytest.raises(ValueError, match="type"):
        json_selector.css_match_explanation("a")


def test_relative_selector_category_for_has() -> None:
    selector = Selector(text="<div><p>x</p></div>")
    div = selector.css("div")[0]
    failing = div.css_match_explanation("div:has(section)")
    relation_checks = [
        check
        for compound in failing.branches[0].compounds
        for check in compound.checks
        if check.category == RELATION_CONDITION
    ]
    assert relation_checks
    assert relation_checks[0].passed is False
    assert ":has(section)" in relation_checks[0].expression


def test_many_selectors_align_with_engine(catalog: Selector) -> None:
    for css in (
        "span",
        "span.price",
        ".catalog",
        "section > h2",
        "ul li",
        "li + li",
        "li ~ li",
        "li:nth-child(2)",
        "li:nth-child(odd)",
        "li:nth-child(2n+1)",
        "li:nth-last-child(1)",
        "span:first-child",
        "span:last-child",
        "section.catalog li span.price",
        "li:not(:first-child)",
        "div, p, span",
    ):
        _assert_aligned_with_engine(catalog, css)


# Controlled vocabularies keep the generated selectors inside the syntax the
# engine supports, so every generated case can actually be explained.
_RANDOM_TAGS = ["div", "p", "span", "li", "ul", "a", "b"]
_RANDOM_CLASSES = ["x", "y", "z"]
_RANDOM_IDS = ["i1", "i2"]
_RANDOM_STRUCTURAL = [
    ":first-child",
    ":last-child",
    ":nth-child(1)",
    ":nth-child(2)",
    ":nth-child(odd)",
    ":empty",
]


def _make_compound(
    type_part: str,
    class_part: str | None,
    id_part: str | None,
    structural: str | None,
) -> str:
    return "".join(
        part for part in (type_part, id_part, class_part, structural) if part
    )


_random_compound = st.builds(
    _make_compound,
    st.sampled_from([*_RANDOM_TAGS, "*"]),
    st.one_of(st.none(), st.sampled_from(_RANDOM_CLASSES).map(".{0}".format)),
    st.one_of(st.none(), st.sampled_from(_RANDOM_IDS).map("#{0}".format)),
    st.one_of(st.none(), st.sampled_from(_RANDOM_STRUCTURAL)),
)


def _render_chain(compounds: list[str], combinators: list[str]) -> str:
    rendered = compounds[0]
    for combinator, compound in zip(combinators, compounds[1:], strict=True):
        rendered += (combinator if combinator == " " else f" {combinator} ") + compound
    return rendered


_random_selector = st.lists(_random_compound, min_size=1, max_size=3).flatmap(
    lambda compounds: st.builds(
        _render_chain,
        st.just(compounds),
        st.lists(
            st.sampled_from([" ", ">", "+", "~"]),
            min_size=len(compounds) - 1,
            max_size=len(compounds) - 1,
        ),
    )
)

_random_node = st.recursive(
    st.fixed_dictionaries(
        {
            "tag": st.sampled_from(_RANDOM_TAGS),
            "class_": st.sampled_from([*_RANDOM_CLASSES, ""]),
            "id_": st.sampled_from([*_RANDOM_IDS, ""]),
            "children": st.just([]),
        }
    ),
    lambda children: st.fixed_dictionaries(
        {
            "tag": st.sampled_from(_RANDOM_TAGS),
            "class_": st.sampled_from([*_RANDOM_CLASSES, ""]),
            "id_": st.sampled_from([*_RANDOM_IDS, ""]),
            "children": st.lists(children, max_size=3),
        }
    ),
    max_leaves=10,
)


def _render_node(node: dict[str, object]) -> str:
    tag = node["tag"]
    attributes = ""
    if node["class_"]:
        attributes += f' class="{node["class_"]}"'
    if node["id_"]:
        attributes += f' id="{node["id_"]}"'
    children = "".join(
        _render_node(child)
        for child in node["children"]  # type: ignore[arg-type]
    )
    return f"<{tag}{attributes}>{children}</{tag}>"


@given(node=_random_node, css=_random_selector)
@settings(max_examples=120, deadline=None)
def test_random_selectors_align_with_engine(node: dict[str, object], css: str) -> None:
    selector = Selector(text=f"<root>{_render_node(node)}</root>")
    matched_nodes = {matched.root for matched in selector.css(css)}
    for element in selector.xpath("//*"):
        explanation = explain_css_match(css, element.root)
        assert explanation.matched == (element.root in matched_nodes), css


@given(node=_random_node, css=_random_selector)
@settings(max_examples=60, deadline=None)
def test_random_explanations_never_mutate_document(
    node: dict[str, object], css: str
) -> None:
    selector = Selector(text=f"<root>{_render_node(node)}</root>")
    before = etree.tostring(selector.root)
    for element in selector.xpath("//*"):
        explain_css_match(css, element.root)
    assert etree.tostring(selector.root) == before
