"""
Tests for call-level source policies (htmldate.sources).
"""

import pytest

from lxml import html
from unittest.mock import patch

from htmldate import Source, SourcePolicy, find_date
from htmldate.core import examine_header_dates, examine_time_elements
from htmldate.settings import MIN_DATE
from htmldate.utils import Extractor
from htmldate.validators import get_max_date


def _options(policy: SourcePolicy) -> Extractor:
    return Extractor(
        True, get_max_date(None), MIN_DATE, True, "%Y-%m-%d", policy, None
    )


def _tree(document: str):
    return html.fromstring(document)


URL = "http://example.com/section/2016/07/12/key-words"
URL_DATE = "2016-07-12"

URL_ONLY_DOC = "<html><body><p>Aaa, bbb and filler text.</p></body></html>"
META_DOC = (
    '<html><head><meta name="date" content="2017-01-02"/></head>'
    "<body><p>filler text</p></body></html>"
)
JSON_DOC = (
    '<html><head><script type="application/ld+json">'
    '{"datePublished":"2018-03-04"}</script></head>'
    "<body><p>filler text</p></body></html>"
)
TIME_DOC = (
    '<html><body><time datetime="2019-05-06T10:00:00">May 6 2019</time>'
    "<p>filler text</p></body></html>"
)
CONTENT_DOC = (
    '<html><body><span class="date-published">2020-06-07</span>'
    "<p>filler text</p></body></html>"
)
MULTI_DOC = (
    '<html><head><meta name="date" content="2017-01-02"/>'
    '<script type="application/ld+json">{"datePublished":"2018-03-04"}'
    "</script></head><body>"
    '<time datetime="2019-05-06">x</time>'
    '<span class="date-published">2020-06-07</span></body></html>'
)

CHANNEL_DATES = {
    Source.STRUCTURED: "2018-03-04",
    Source.META: "2017-01-02",
    Source.TIME: "2019-05-06",
    Source.CONTENT: "2020-06-07",
    Source.URL: URL_DATE,
}


def test_default_policy_allows_every_source():
    policy = SourcePolicy()
    assert all(policy.permits(source) for source in Source)
    # passing no policy or an explicit None is equivalent
    assert find_date(META_DOC, original_date=True) == find_date(
        META_DOC, original_date=True, source_policy=None
    )


def test_policy_validation():
    with pytest.raises(ValueError):
        SourcePolicy(allow=frozenset())
    with pytest.raises(ValueError):
        SourcePolicy(
            disable=frozenset({Source.META}), require=frozenset({Source.META})
        )
    with pytest.raises(ValueError):
        SourcePolicy(
            allow=frozenset({Source.URL}), require=frozenset({Source.META})
        )
    with pytest.raises(ValueError):
        SourcePolicy.from_mapping({"allow": ["bogus"]})
    with pytest.raises(ValueError):
        SourcePolicy.from_json("{not json")
    with pytest.raises(TypeError):
        find_date(META_DOC, source_policy=123)


def test_policy_aliases_and_layering():
    policy = SourcePolicy.from_mapping(
        {"disable": ["body", "json-ld", "uri", "header"]}
    )
    assert policy.disable == {
        Source.CONTENT,
        Source.STRUCTURED,
        Source.URL,
        Source.META,
    }
    base = SourcePolicy(disable=frozenset({Source.URL}))
    layered = SourcePolicy.from_mapping({"disable": ["meta"]}, base=base)
    assert layered.disable == {Source.URL, Source.META}
    # the base policy is not mutated by layering
    assert base.disable == frozenset({Source.URL})


def test_url_channel_disabled():
    assert find_date(URL_ONLY_DOC, url=URL) == URL_DATE
    policy = SourcePolicy(disable=frozenset({Source.URL}))
    assert find_date(URL_ONLY_DOC, url=URL, source_policy=policy) is None
    # disabled channels are never queried, not filtered out afterwards
    with patch("htmldate.core.extract_url_date") as mock_url:
        find_date(URL_ONLY_DOC, url=URL, source_policy=policy)
        mock_url.assert_not_called()
    with patch("htmldate.core.img_search") as mock_img:
        find_date(MULTI_DOC, url=URL, source_policy=policy, original_date=True)
        mock_img.assert_not_called()


def test_each_channel_can_be_isolated():
    for source, expected in CHANNEL_DATES.items():
        policy = SourcePolicy(allow=frozenset({source}))
        assert (
            find_date(
                MULTI_DOC,
                url=URL,
                source_policy=policy,
                original_date=True,
            )
            == expected
        ), source


def test_structured_and_meta_gating():
    no_json = SourcePolicy(disable=frozenset({Source.STRUCTURED}))
    for extensive in (False, True):
        assert (
            find_date(
                JSON_DOC,
                source_policy=no_json,
                original_date=True,
                extensive_search=extensive,
            )
            is None
        )
    with patch("htmldate.core.json_search") as mock_json:
        find_date(JSON_DOC, source_policy=no_json, original_date=True)
        mock_json.assert_not_called()

    no_meta = SourcePolicy(disable=frozenset({Source.META}))
    for extensive in (False, True):
        assert (
            find_date(
                META_DOC,
                source_policy=no_meta,
                original_date=True,
                extensive_search=extensive,
            )
            is None
        )
    # the header scanner short-circuits before touching any meta element
    options = _options(no_meta)
    assert examine_header_dates(_tree(META_DOC), options) == (None, None)
    explanation = find_date(
        META_DOC,
        source_policy=no_meta,
        explain=True,
        original_date=True,
    )
    assert explanation["result"] is None
    assert "meta" in explanation["excluded"]
    assert "meta" not in explanation["considered"]


def test_time_and_content_gating():
    no_time = SourcePolicy(disable=frozenset({Source.TIME}))
    # a date carried only by the time element must vanish in both modes
    time_doc = (
        '<html><body><time datetime="2019-05-06"></time>'
        "<p>filler text here</p></body></html>"
    )
    for extensive in (False, True):
        assert (
            find_date(
                time_doc,
                source_policy=no_time,
                original_date=True,
                extensive_search=extensive,
            )
            is None
        )
    # the dedicated scanner returns immediately when the channel is gated
    assert examine_time_elements(_tree(time_doc), _options(no_time)) is None
    explanation = find_date(
        time_doc, source_policy=no_time, explain=True, original_date=True
    )
    assert "time" in explanation["excluded"]
    assert "time" not in explanation["considered"]

    no_content = SourcePolicy(disable=frozenset({Source.CONTENT}))
    for extensive in (False, True):
        assert (
            find_date(
                CONTENT_DOC,
                source_policy=no_content,
                original_date=True,
                extensive_search=extensive,
            )
            is None
        )
    explanation = find_date(
        CONTENT_DOC, source_policy=no_content, explain=True, original_date=True
    )
    assert "content" in explanation["excluded"]
    assert "content" not in explanation["considered"]

    time_only = SourcePolicy(allow=frozenset({Source.TIME}))
    assert find_date(CONTENT_DOC, source_policy=time_only) is None


def test_disabled_markup_stripped_from_search_tree_only():
    from htmldate.core import strip_disabled_sources

    # the caller's tree is never modified: find_date strips a private copy
    caller_tree = _tree(META_DOC)
    assert find_date(
        caller_tree,
        source_policy=SourcePolicy(disable=["meta"]),
        original_date=True,
    ) is None
    assert caller_tree.find(".//meta") is not None

    # the helper itself removes the targeted markup from the tree it is given
    tree = _tree(
        '<html><head><meta name="date" content="2017-01-02"/>'
        '<script type="application/ld+json">{"datePublished":"2018-03-04"}'
        "</script></head><body>"
        '<time datetime="2019-05-06"></time></body></html>'
    )
    strip_disabled_sources(tree, SourcePolicy(disable=["meta", "time"]))
    assert tree.find(".//meta") is None
    assert tree.find(".//time") is None
    # structured data is a different channel and stays untouched
    assert tree.find('.//script[@type="application/ld+json"]') is not None

    # the open policy removes nothing
    open_tree = _tree(META_DOC)
    strip_disabled_sources(open_tree, SourcePolicy())
    assert open_tree.find(".//meta") is not None


def test_og_image_follows_url_channel_not_meta():
    doc = (
        '<html><head><meta name="date" content="2017-01-02"/>'
        '<meta property="og:image" '
        'content="http://img.example.com/2019/05/06/pic.jpg"/></head>'
        "<body><p>filler text</p></body></html>"
    )
    # meta disabled but URL permitted: the image URL date survives
    assert (
        find_date(
            doc,
            source_policy=SourcePolicy(disable=["meta"]),
            original_date=True,
        )
        == "2019-05-06"
    )
    # a URL-only policy reaches the same image-URL date
    assert (
        find_date(
            doc,
            source_policy=SourcePolicy(allow=["url"]),
            original_date=True,
        )
        == "2019-05-06"
    )
    # both channels off: nothing remains
    assert (
        find_date(
            doc,
            source_policy=SourcePolicy(disable=["meta", "url"]),
            original_date=True,
        )
        is None
    )


def test_required_channels_must_corroborate():
    agree_doc = META_DOC.replace("2017-01-02", URL_DATE)
    policy = SourcePolicy(require=frozenset({Source.URL, Source.META}))
    assert find_date(agree_doc, url=URL, source_policy=policy) == URL_DATE

    explanation = find_date(
        agree_doc, url=URL, source_policy=policy, explain=True
    )
    used = {entry["source"]: entry for entry in explanation["used"]}
    # the two required channels must both corroborate, regardless of any
    # additional generic content evidence on the same page
    assert {"url", "meta"} <= set(used)
    assert all(used[name]["required"] for name in ("url", "meta"))

    # both channels present but disagreeing: not enough trustworthy evidence
    disagree = find_date(MULTI_DOC, url=URL, source_policy=policy, original_date=True)
    assert disagree is None
    explanation = find_date(
        MULTI_DOC, url=URL, source_policy=policy, explain=True, original_date=True
    )
    assert {entry["source"] for entry in explanation["disagreement"]} == {
        "url",
        "meta",
    }


def test_required_time_and_content():
    agree = (
        '<html><body><time datetime="2019-05-06">x</time>'
        '<span class="date-published">2019-05-06</span></body></html>'
    )
    disagree = (
        '<html><body><time datetime="2019-05-06">x</time>'
        '<span class="date-published">2020-06-07</span></body></html>'
    )
    policy = SourcePolicy(require=frozenset({Source.TIME, Source.CONTENT}))
    assert find_date(agree, source_policy=policy, original_date=True) == "2019-05-06"
    assert find_date(disagree, source_policy=policy, original_date=True) is None


def test_missing_required_channel_is_unknown():
    policy = SourcePolicy(require=frozenset({Source.STRUCTURED}))
    assert find_date(CONTENT_DOC, source_policy=policy, original_date=True) is None
    explanation = find_date(
        CONTENT_DOC, source_policy=policy, explain=True, original_date=True
    )
    assert explanation["result"] is None
    assert explanation["missing_required"] == ["structured"]


def test_explanation_lists_adopted_and_excluded():
    policy = SourcePolicy(
        allow=frozenset({Source.META, Source.URL}),
        disable=frozenset({Source.URL}),
    )
    explanation = find_date(
        MULTI_DOC,
        url=URL,
        source_policy=policy,
        explain=True,
        original_date=True,
    )
    assert explanation["result"] == "2017-01-02"
    assert [entry["source"] for entry in explanation["used"]] == ["meta"]
    # everything outside the allow list is reported, with URL explicitly disabled
    assert explanation["excluded"]["url"] == "disabled by policy"
    assert set(explanation["excluded"]) == {
        "structured",
        "time",
        "content",
        "url",
    }
    assert explanation["policy"]["disable"] == ["url"]
    assert explanation["missing_required"] == []
    assert explanation["disagreement"] == []


def test_policies_do_not_pollute_each_other():
    no_url = SourcePolicy(disable=frozenset({Source.URL}))
    only_url = SourcePolicy(allow=frozenset({Source.URL}))
    default = SourcePolicy()

    for _ in range(5):
        assert (
            find_date(META_DOC, url=URL, source_policy=no_url, original_date=True)
            == "2017-01-02"
        )
        assert (
            find_date(META_DOC, url=URL, source_policy=only_url, original_date=True)
            == URL_DATE
        )
        # the default open policy is unaffected: the URL channel still wins
        assert find_date(META_DOC, url=URL, original_date=True) == URL_DATE

    # policy objects and the default configuration stay untouched
    assert default.permits(Source.URL)
    assert no_url.disable == frozenset({Source.URL})
    assert only_url.allow == frozenset({Source.URL})

    # evidence is per call: two explain runs never share bookkeeping
    first = find_date(
        META_DOC, source_policy=no_url, explain=True, original_date=True
    )
    second = find_date(
        JSON_DOC,
        source_policy=SourcePolicy(allow=frozenset({Source.STRUCTURED})),
        explain=True,
        original_date=True,
    )
    assert [entry["source"] for entry in first["used"]] == ["meta"]
    assert [entry["source"] for entry in second["used"]] == ["structured"]


def test_mapping_and_json_policy_arguments():
    mapping_policy = {"disable": ["url"]}
    json_policy = '{"disable": ["url"]}'
    expected = find_date(
        MULTI_DOC,
        url=URL,
        source_policy=SourcePolicy(disable=frozenset({Source.URL})),
        original_date=True,
    )
    assert (
        find_date(
            MULTI_DOC, url=URL, source_policy=mapping_policy, original_date=True
        )
        == expected
    )
    assert (
        find_date(
            MULTI_DOC, url=URL, source_policy=json_policy, original_date=True
        )
        == expected
    )
    # without explain the return type stays a plain string or None
    result = find_date(META_DOC, source_policy=mapping_policy, original_date=True)
    assert isinstance(result, str)


def test_cli_policy_arguments(tmp_path):
    from htmldate.cli import _load_policy, _render, parse_args

    args = parse_args(["--source-policy", '{"disable": ["url"]}', "--explain"])
    assert args.explain is True
    policy = _load_policy(args.source_policy)
    assert policy.disable == frozenset({Source.URL})

    # a policy stored in a file is supported too
    policy_file = tmp_path / "policy.json"
    policy_file.write_text('{"require": ["meta", "time"]}', encoding="utf-8")
    loaded = _load_policy(str(policy_file))
    assert loaded.require == frozenset({Source.META, Source.TIME})
    assert _load_policy(None) is None

    # rendering keeps dates plain and serializes audits
    assert _render("2016-07-12") == "2016-07-12"
    assert _render(None) == "None"
    assert '"result"' in _render({"result": None})

    # the default namespace carries no policy and keeps explain off
    defaults = parse_args([])
    assert defaults.source_policy is None and defaults.explain is False
