"""
Tests for the detailed language analysis (analyze_language).
"""

import pytest

from courlan import LangVerdict, analyze_language


def test_verdict_states():
    "The three headline states plus clear mismatch."
    # clear match: host and path agree with the target
    analysis = analyze_language("https://de.example-shop.com/de/angebote", "de")
    assert analysis.verdict is LangVerdict.MATCH
    assert analysis.accepted and not analysis.needs_review
    # conflict: host says German, path says English
    analysis = analyze_language("https://de.example-shop.com/en/offers", "de")
    assert analysis.verdict is LangVerdict.CONFLICT
    assert analysis.needs_review and not analysis.accepted
    # unknown: no language cue anywhere
    analysis = analyze_language("https://example-shop.com/katalog", "de")
    assert analysis.verdict is LangVerdict.UNKNOWN
    assert not analysis.needs_review
    # clear mismatch: all cues point away from the target
    analysis = analyze_language("https://fr.example-shop.com/en/offers", "de")
    assert analysis.verdict is LangVerdict.MISMATCH
    assert not analysis.accepted and not analysis.needs_review


def test_no_cues_is_not_a_mismatch():
    "Absence of language cues must not be read as the wrong language."
    analysis = analyze_language("https://example-shop.com/katalog", "de")
    assert analysis.verdict is LangVerdict.UNKNOWN
    assert analysis.accepted  # balanced policy tolerates unknowns
    assert analysis.signals == ()


@pytest.mark.parametrize(
    "policy,expected",
    [
        ("lenient", True),  # conflicts are tolerated
        ("balanced", False),  # conflicts go to manual review
        ("strict", False),
    ],
)
def test_policy_on_conflict(policy, expected):
    url = "https://de.example-shop.com/en/offers"
    assert analyze_language(url, "de", policy=policy).accepted is expected


@pytest.mark.parametrize(
    "policy,expected",
    [("lenient", True), ("balanced", True), ("strict", False)],
)
def test_policy_on_unknown(policy, expected):
    url = "https://example-shop.com/katalog"
    assert analyze_language(url, "de", policy=policy).accepted is expected


def test_strict_policy_requires_explicit_match():
    analysis = analyze_language(
        "https://example-shop.com/de/angebote", "de", policy="strict"
    )
    assert analysis.verdict is LangVerdict.MATCH and analysis.accepted


def test_conflicting_site_walkthrough():
    "A shop whose German subdomain serves English paths."
    cases = {
        # domain and path agree: filter in automatically
        "https://de.example-shop.com/de/angebote": (LangVerdict.MATCH, True),
        # domain contradicts path (and query): manual decision
        "https://de.example-shop.com/en/offers": (LangVerdict.CONFLICT, False),
        "https://de.example-shop.com/en/offers?lang=en": (LangVerdict.CONFLICT, False),
        # no cues at all: unknown, kept by default
        "https://de.example-shop.com/katalog": (LangVerdict.MATCH, True),
        # every cue points elsewhere: filter out automatically
        "https://fr.example-shop.com/en/offers": (LangVerdict.MISMATCH, False),
    }
    # the catalog page has no path cue, only the matching host label
    analysis = analyze_language("https://de.example-shop.com/katalog", "de")
    assert analysis.verdict is LangVerdict.MATCH
    for url, (verdict, accepted) in cases.items():
        analysis = analyze_language(url, "de")
        assert analysis.verdict is verdict, url
        assert analysis.accepted is accepted, url
    # the conflicting URLs are exactly the ones flagged for review
    assert analyze_language(
        "https://de.example-shop.com/en/offers?lang=en", "de"
    ).needs_review


def test_query_signals():
    "Language statements in the query string count as labels."
    analysis = analyze_language("https://example-shop.com/p?lang=de", "de")
    assert analysis.verdict is LangVerdict.MATCH
    assert analysis.signals[0].source == "query"
    # spelled-out values are normalized through the alias table
    analysis = analyze_language("https://example-shop.com/p?language=german", "de")
    assert analysis.verdict is LangVerdict.MATCH
    # locale form in a parameter
    analysis = analyze_language("https://example-shop.com/p?lang=en_US", "en")
    assert analysis.verdict is LangVerdict.MATCH
    # contradicting parameter
    analysis = analyze_language("https://example-shop.com/p?lang=fr", "de")
    assert analysis.verdict is LangVerdict.MISMATCH
    # a parameter without value states nothing
    analysis = analyze_language("https://example-shop.com/p?lang=&id=3", "de")
    assert analysis.verdict is LangVerdict.UNKNOWN
    assert analysis.signals == ()


def test_article_paths_are_not_labels():
    "Segments that merely look like language codes stay candidates."
    # /de/ buried in an article path: recorded but not decisive
    analysis = analyze_language("https://example-shop.com/report/de/sommer-sale", "de")
    assert analysis.verdict is LangVerdict.UNKNOWN
    assert analysis.accepted
    assert len(analysis.signals) == 1
    signal = analysis.signals[0]
    assert (signal.source, signal.cue, signal.match, signal.label) == (
        "path",
        "de",
        True,
        False,
    )
    # too many look-alike segments: none of them can label the page
    analysis = analyze_language("https://x.com/en/x/de/x/fr/", "en")
    assert analysis.verdict is LangVerdict.UNKNOWN
    assert all(not s.label for s in analysis.signals)
    # /ch/ is not a language code: kept as context, no decision
    analysis = analyze_language(
        "http://www.verfassungen.de/ch/basel/verf03.htm", "de"
    )
    assert analysis.verdict is LangVerdict.UNKNOWN
    assert analysis.signals[0].match is None
    # the leading segment and explicit locales do label a page
    analysis = analyze_language("https://example-shop.com/de/angebote", "de")
    assert analysis.verdict is LangVerdict.MATCH
    analysis = analyze_language("https://example-shop.com/shop/de-AT/angebote", "de")
    assert analysis.verdict is LangVerdict.MATCH
    assert analysis.signals[0].label


def test_variant_matching():
    "Regional variants: primary-language or exact matching."
    url = "https://example-shop.com/de-AT/angebote"
    # primary (default): de-AT counts as German
    assert analyze_language(url, "de").verdict is LangVerdict.MATCH
    assert analyze_language(url, "de-AT").verdict is LangVerdict.MATCH
    # exact: the full locale must be identical
    analysis = analyze_language(url, "de", variant="exact")
    assert analysis.verdict is LangVerdict.MISMATCH
    analysis = analyze_language(url, "de-AT", variant="exact")
    assert analysis.verdict is LangVerdict.MATCH
    analysis = analyze_language(url, "de-CH", variant="exact")
    assert analysis.verdict is LangVerdict.MISMATCH
    # ...while primary matching equates the regional variants
    analysis = analyze_language(url, "de-CH", variant="primary")
    assert analysis.verdict is LangVerdict.MATCH


def test_trailing_slash_option():
    analysis = analyze_language(
        "https://example-shop.com/de", "de", trailing_slash=False
    )
    assert analysis.verdict is LangVerdict.MATCH
    # without the trailing slash the default regex sees no path cue
    analysis = analyze_language("https://example-shop.com/de", "de")
    assert analysis.verdict is LangVerdict.UNKNOWN


def test_invalid_arguments():
    with pytest.raises(ValueError):
        analyze_language("https://example-shop.com/", "de", policy="ruthless")
    with pytest.raises(ValueError):
        analyze_language("https://example-shop.com/", "de", variant="fuzzy")
    with pytest.raises(ValueError):
        analyze_language("https://example-shop.com/", "xx")
    with pytest.raises(ValueError):
        analyze_language("https://example-shop.com/", "de-XY")
