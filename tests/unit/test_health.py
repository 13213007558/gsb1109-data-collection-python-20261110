import pytest

from autoscraper import (
    AutoScraper,
    DRIFT_EXPANSION,
    DRIFT_LOW_COVERAGE,
    DRIFT_MISSING,
    DRIFT_RESULT_SHAPE,
    DRIFT_STRUCTURE,
    DRIFT_UNKNOWN_PAGE,
    RuleHealthCheck,
)

# ---------------------------------------------------------------------------
# Training samples: same structure, different products (normal content change)
# ---------------------------------------------------------------------------
PRODUCT_SAMPLE_1 = """
<div id='product'>
  <h1 class='title'>Sony PlayStation 4 Pro Console - Black</h1>
  <span class='price'>US $349.99</span>
  <div class='rating'><span class='value'>4.8</span></div>
</div>
"""

PRODUCT_SAMPLE_2 = """
<div id='product'>
  <h1 class='title'>Acer Predator Helios 300 Gaming Laptop</h1>
  <span class='price'>US $1,229.49</span>
  <div class='rating'><span class='value'>5.0</span></div>
</div>
"""

PRODUCT_SAMPLE_3 = """
<div id='product'>
  <h1 class='title'>Nintendo Switch OLED White Handheld Console</h1>
  <span class='price'>US $279.00</span>
  <div class='rating'><span class='value'>4.9</span></div>
</div>
"""

# Same leaf tags, moved into new wrappers with new classes: a page redesign.
REDESIGNED_SAMPLE = """
<div class='pdp'>
  <section class='hero'>
    <h1 class='product-name'>Xbox Series X 1TB Console Bundle</h1>
    <div class='cost'><span class='amount'>US $499.00</span></div>
    <div class='stars'><b class='score'>4.7</b></div>
  </section>
</div>
"""

# No recognizable product structure at all: an unknown page must not pass.
UNKNOWN_SAMPLE = """
<article>
  <h2>Ten gardening tips for spring</h2>
  <p>Welcome to our blog, today we talk about soil and seeds.</p>
</article>
"""

LIST_SAMPLE_1 = """
<ul class='products'>
  <li class='item'><h3 class='name'>Wireless Mouse</h3></li>
  <li class='item'><h3 class='name'>Mechanical Keyboard</h3></li>
</ul>
"""

# Markup reuses the product structure for promo banners: rule expansion.
LIST_SAMPLE_PROMO = """
<ul class='products'>
  <li class='item'><h3 class='name'>Wireless Mouse</h3></li>
  <li class='item'><h3 class='name'>Mechanical Keyboard</h3></li>
  <li class='item'><h3 class='name'>Flash Sale 50% Off Today Only 2026</h3></li>
  <li class='item'><h3 class='name'>Limited Deal Extra 90% Off Ends Midnight</h3></li>
</ul>
"""

PROMO_SLIPPAGE_SAMPLE = """
<div id='product'>
  <h1 class='title'>50% OFF 2026 DEAL SAVE 90% TODAY LIMITED OFFER 75% BONUS</h1>
  <span class='price'>US $279.00</span>
  <div class='rating'><span class='value'>4.9</span></div>
</div>
"""

THRESHOLDS = {
    "min_match_count": 1,
    "max_match_count": 1,
    "min_coverage": 0.8,
    "max_unknown_fraction": 0.5,
    "max_depth_change": 0,
    "max_tag_jaccard_distance": 0.2,
    "max_digit_fraction_change": 0.1,
    "max_blank_fraction_change": 0.5,
    "max_length_ratio_change": 0.5,
}


def _build_scraper():
    scraper = AutoScraper()
    scraper.build(
        html=PRODUCT_SAMPLE_1,
        wanted_dict={
            "title": ["Sony PlayStation 4 Pro Console - Black"],
            "price": ["US $349.99"],
            "rating": ["4.8"],
        },
    )
    return scraper


def _rule_for_alias(check, alias):
    for rule_id, baseline in check.baselines.items():
        if baseline["rule"]["alias"] == alias:
            return rule_id
    raise AssertionError("no rule with alias %s" % alias)


def _kinds(report):
    return {flag["kind"] for flag in report["flags"]}


def test_record_baseline_captures_structure_counts_and_shape():
    scraper = _build_scraper()
    check = scraper.health_check(THRESHOLDS)
    summary = check.record_baseline(
        [{"html": PRODUCT_SAMPLE_1}, {"html": PRODUCT_SAMPLE_2}]
    )

    assert summary["rule_count"] == len(scraper.stack_list)
    assert summary["sample_count"] == 2
    title_id = _rule_for_alias(check, "title")
    title_baseline = check.baselines[title_id]
    assert title_baseline["rule"]["path"][-1] == "h1"
    assert title_baseline["typical_match_count"] == 1
    assert title_baseline["median_length"] > 0
    assert 0.0 <= title_baseline["typical_digit_fraction"] <= 1.0
    # raw content text is summarized, not stored as training copies
    assert "PlayStation" not in str(title_baseline)


def test_normal_product_change_is_healthy_even_though_text_changed():
    scraper = _build_scraper()
    check = scraper.health_check(THRESHOLDS)
    check.record_baseline(
        [{"html": PRODUCT_SAMPLE_1}, {"html": PRODUCT_SAMPLE_2}]
    )

    report = check.check(html=PRODUCT_SAMPLE_3)

    assert report["healthy"] is True
    assert report["flags"] == []
    title_id = _rule_for_alias(check, "title")
    # the wording is genuinely different from every training sample ...
    assert report["observations"][title_id]["sample_values"] == [
        "Nintendo Switch OLED White Handheld Console"
    ]
    # ... yet structure and counts hold, so no rule is flagged
    assert report["observations"][title_id]["match_count"] == 1


def test_page_redesign_flags_structure_change_with_evidence():
    scraper = _build_scraper()
    check = scraper.health_check(THRESHOLDS)
    check.record_baseline([{"html": PRODUCT_SAMPLE_1}])
    before = [dict(stack) for stack in scraper.stack_list]

    report = check.check(html=REDESIGNED_SAMPLE)

    assert report["healthy"] is False
    kinds = _kinds(report)
    assert DRIFT_STRUCTURE in kinds
    assert DRIFT_LOW_COVERAGE in kinds
    assert DRIFT_UNKNOWN_PAGE in kinds

    structure_flags = [
        flag for flag in report["flags"] if flag["kind"] == DRIFT_STRUCTURE
    ]
    title_id = _rule_for_alias(check, "title")
    title_flag = next(
        flag for flag in structure_flags if flag["rule_id"] == title_id
    )
    evidence = title_flag["evidence"]
    assert evidence["leaf_tag"] == "h1"
    assert evidence["leaf_tag_count"] == 1
    assert evidence["leaf_exact_match_count"] == 0
    assert evidence["depth_change"] > 0
    assert evidence["candidate_paths"][0][-1] == "h1"
    assert evidence["baseline_path"][-1] == "h1"
    # the check is read-only: rules survive a failed check untouched
    assert [stack["hash"] for stack in scraper.stack_list] == [
        stack["hash"] for stack in before
    ]


def test_unknown_page_is_not_reported_as_success_or_redesign():
    scraper = _build_scraper()
    check = scraper.health_check(THRESHOLDS)
    check.record_baseline([{"html": PRODUCT_SAMPLE_1}])

    report = check.check(html=UNKNOWN_SAMPLE)

    assert report["healthy"] is False
    kinds = _kinds(report)
    assert DRIFT_UNKNOWN_PAGE in kinds
    assert DRIFT_LOW_COVERAGE in kinds
    assert DRIFT_MISSING in kinds
    # leaf tags vanished entirely, so there is no structural evidence to claim
    # a redesign; unknown pages must not be guessed into place
    assert DRIFT_STRUCTURE not in kinds


def test_expansion_flag_when_rule_bleeds_into_promo_copy():
    scraper = AutoScraper()
    scraper.build(html=LIST_SAMPLE_1, wanted_list=["Wireless Mouse"])
    check = scraper.health_check({"max_match_count": 2})
    check.record_baseline([{"html": LIST_SAMPLE_1}])

    report = check.check(html=LIST_SAMPLE_PROMO)

    assert report["healthy"] is False
    flag = next(flag for flag in report["flags"] if flag["kind"] == DRIFT_EXPANSION)
    assert flag["evidence"]["match_count"] == 4
    assert flag["evidence"]["threshold"] == 2
    assert any("Flash Sale" in value for value in flag["evidence"]["sample_values"])


def test_result_shape_change_catches_promo_slippage_but_not_new_product():
    scraper = _build_scraper()
    check = scraper.health_check(
        {"max_digit_fraction_change": 0.08, "max_length_ratio_change": 0.6}
    )
    check.record_baseline(
        [{"html": PRODUCT_SAMPLE_1}, {"html": PRODUCT_SAMPLE_2}]
    )

    healthy_report = check.check(html=PRODUCT_SAMPLE_3)
    assert healthy_report["healthy"] is True

    slippage_report = check.check(html=PROMO_SLIPPAGE_SAMPLE)
    shape_flags = [
        flag
        for flag in slippage_report["flags"]
        if flag["kind"] == DRIFT_RESULT_SHAPE
    ]
    title_id = _rule_for_alias(check, "title")
    title_flag = next(
        flag for flag in shape_flags if flag["rule_id"] == title_id
    )
    assert title_flag["evidence"]["digit_fraction_change"] > 0.08
    assert any("50% OFF" in v for v in title_flag["evidence"]["sample_values"])


def test_alias_is_attached_to_rule_level_flags():
    scraper = _build_scraper()
    check = scraper.health_check(THRESHOLDS)
    check.record_baseline([{"html": PRODUCT_SAMPLE_1}])

    report = check.check(html=REDESIGNED_SAMPLE)
    title_flags = [
        flag
        for flag in report["flags"]
        if flag["rule_id"] == _rule_for_alias(check, "title")
    ]
    assert title_flags
    assert all(flag["alias"] == "title" for flag in title_flags)


def test_check_requires_recorded_baseline():
    scraper = _build_scraper()
    check = scraper.health_check(THRESHOLDS)
    with pytest.raises(ValueError):
        check.check(html=PRODUCT_SAMPLE_1)


def test_record_baseline_requires_rules_and_samples():
    scraper = AutoScraper()
    check = scraper.health_check(THRESHOLDS)
    with pytest.raises(ValueError):
        check.record_baseline([{"html": PRODUCT_SAMPLE_1}])

    scraper = _build_scraper()
    check = scraper.health_check(THRESHOLDS)
    with pytest.raises(ValueError):
        check.record_baseline([])


def test_thresholds_are_validated():
    scraper = _build_scraper()
    with pytest.raises(ValueError):
        scraper.health_check({"min_coverage": 1.5})
    with pytest.raises(ValueError):
        scraper.health_check({"min_match_count": -1})


def test_without_thresholds_nothing_is_flagged():
    scraper = _build_scraper()
    check = scraper.health_check()
    check.record_baseline([{"html": PRODUCT_SAMPLE_1}])

    report = check.check(html=REDESIGNED_SAMPLE)

    # every dimension is opt-in: no explicit threshold means no verdict,
    # observations are still collected for callers that judge themselves
    assert report["healthy"] is True
    assert report["flags"] == []
    assert report["observations"]


def test_baseline_serialization_roundtrip(tmp_path):
    scraper = _build_scraper()
    check = scraper.health_check(THRESHOLDS)
    check.record_baseline([{"html": PRODUCT_SAMPLE_1}])

    import json

    blob = json.dumps(check.to_dict())
    restored = RuleHealthCheck.from_dict(scraper, json.loads(blob))

    expected = check.check(html=REDESIGNED_SAMPLE)
    actual = restored.check(html=REDESIGNED_SAMPLE)
    assert _kinds(actual) == _kinds(expected)
    assert actual["healthy"] == expected["healthy"]
