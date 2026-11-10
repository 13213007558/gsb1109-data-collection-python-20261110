import pytest

from parsel import Selector
from parsel.rulegroup import (
    CandidateStatus,
    ExtractionError,
    ExtractionRule,
    GroupStatus,
    RuleGroup,
)

RELAUNCH_HTML = """
<html><body>
  <div id="legacy" style="display:none">
    <span class="price">0,00 €</span>
    <h1 class="name">Artikel wird geladen …</h1>
  </div>
  <main id="product">
    <p class="price">49,99 €</p>
    <h1 class="name">Kaffeemühle CM-200</h1>
  </main>
</body></html>
"""

PRICE = r"[1-9]\d*,\d{2}\s*€"


def price_rules():
    return [
        ExtractionRule("#legacy .price::text", name="legacy", pattern=PRICE),
        ExtractionRule("#product .price::text", name="new", pattern=PRICE),
        ExtractionRule("meta[itemprop=price]::attr(content)", name="microdata"),
    ]


def test_first_mode_skips_invalid_legacy_node():
    result = RuleGroup(price_rules(), mode="first").extract(RELAUNCH_HTML)
    assert result.status is GroupStatus.OK
    assert result.value == "49,99 €"
    legacy, new, microdata = result.candidates
    assert legacy.status is CandidateStatus.INVALID_VALUE
    assert legacy.values == ["0,00 €"]
    assert legacy.reasons
    assert new.status is CandidateStatus.OK
    assert microdata.status is CandidateStatus.NO_MATCH


def test_all_failed_is_not_an_empty_string():
    group = RuleGroup([ExtractionRule("span.missing::text")])
    result = group.extract("<p>nothing</p>")
    assert result.status is GroupStatus.ALL_FAILED
    assert not result.ok
    with pytest.raises(ExtractionError):
        _ = result.value
    with pytest.raises(ExtractionError):
        _ = result.values


def test_bad_count_rejects_too_many_results():
    group = RuleGroup([ExtractionRule("li::text", max_results=2)])
    result = group.extract("<ul><li>a</li><li>b</li><li>c</li></ul>")
    assert result.status is GroupStatus.ALL_FAILED
    candidate = result.candidates[0]
    assert candidate.status is CandidateStatus.BAD_COUNT
    assert "at most 2" in candidate.reasons[0]


def test_min_results_enforced():
    group = RuleGroup([ExtractionRule("li::text", min_results=2, max_results=None)])
    result = group.extract("<ul><li>a</li></ul>")
    assert result.candidates[0].status is CandidateStatus.BAD_COUNT


def test_empty_value_rejected_unless_allowed():
    html = '<span class="x">   </span>'
    result = RuleGroup([ExtractionRule("span.x::text")]).extract(html)
    assert result.candidates[0].status is CandidateStatus.INVALID_VALUE
    ok = RuleGroup([ExtractionRule("span.x::text", allow_empty=True)]).extract(html)
    assert ok.status is GroupStatus.OK


def test_length_validation():
    rule = ExtractionRule("p::text", min_length=3, max_length=5)
    assert (
        RuleGroup([rule]).extract("<p>ab</p>").candidates[0].status
        is CandidateStatus.INVALID_VALUE
    )
    assert (
        RuleGroup([rule]).extract("<p>abcdef</p>").candidates[0].status
        is CandidateStatus.INVALID_VALUE
    )
    assert RuleGroup([rule]).extract("<p>abcd</p>").status is GroupStatus.OK


def test_consensus_agreement():
    rules = [
        ExtractionRule("#product .price::text", pattern=PRICE),
        ExtractionRule(query="string(//p[@class='price'])", type="xpath", pattern=PRICE),
    ]
    result = RuleGroup(rules, mode="consensus").extract(RELAUNCH_HTML)
    assert result.status is GroupStatus.OK
    assert result.value == "49,99 €"


def test_consensus_partial_when_some_candidates_missing():
    rules = [
        ExtractionRule("#product .price::text", pattern=PRICE),
        ExtractionRule("meta[itemprop=price]::attr(content)"),
    ]
    result = RuleGroup(rules, mode="consensus").extract(RELAUNCH_HTML)
    assert result.status is GroupStatus.PARTIAL
    assert result.ok
    assert result.value == "49,99 €"


def test_consensus_conflict_when_valid_candidates_disagree():
    html = "<span class='a'>10 €</span><span class='b'>20 €</span>"
    rules = [
        ExtractionRule("span.a::text", pattern=r"€"),
        ExtractionRule("span.b::text", pattern=r"€"),
    ]
    result = RuleGroup(rules, mode="consensus").extract(html)
    assert result.status is GroupStatus.CONFLICT
    assert not result.ok
    with pytest.raises(ExtractionError, match="disagree"):
        _ = result.value
    assert all(c.ok for c in result.candidates)


def test_consensus_all_failed():
    rules = [ExtractionRule("span.a::text"), ExtractionRule("span.b::text")]
    result = RuleGroup(rules, mode="consensus").extract("<p>x</p>")
    assert result.status is GroupStatus.ALL_FAILED


def test_explain_reports_every_candidate():
    result = RuleGroup(price_rules(), mode="first").extract(RELAUNCH_HTML)
    report = result.explain()
    assert "legacy" in report
    assert "new" in report
    assert "microdata" in report
    assert "invalid_value" in report
    assert "no_match" in report


def test_extract_accepts_selector_and_xpath_rules():
    selector = Selector(RELAUNCH_HTML)
    rules = [
        ExtractionRule("//main[@id='product']/p[@class='price']/text()", type="xpath", pattern=PRICE),
    ]
    result = RuleGroup(rules).extract(selector)
    assert result.value == "49,99 €"


def test_multi_value_group():
    group = RuleGroup([ExtractionRule("li::text", min_results=1, max_results=None)])
    result = group.extract("<ul><li>a</li><li>b</li></ul>")
    assert result.values == ["a", "b"]
    with pytest.raises(ExtractionError, match="exactly one"):
        _ = result.value


def test_invalid_rule_configuration():
    with pytest.raises(ValueError, match="selector type"):
        ExtractionRule("p", type="jmespath")
    with pytest.raises(ValueError, match="min_results"):
        ExtractionRule("p", min_results=-1)
    with pytest.raises(ValueError, match="max_results"):
        ExtractionRule("p", min_results=2, max_results=1)
    with pytest.raises(ValueError, match="at least one rule"):
        RuleGroup([])
    with pytest.raises(ValueError, match="mode"):
        RuleGroup([ExtractionRule("p")], mode="random")
