import json

import pytest

from autoscraper import AutoScraper
from autoscraper.rules import (
    CURRENT_RULE_VERSION,
    FORMAT_NAME,
    LEGACY_VERSION,
    RuleFormatError,
    migrate_file,
    parse_document,
    plan_migration,
    runtime_rules,
    verify_behavior,
)

HTML = "<ul><li>Banana</li><li>Apple</li><li>Orange</li></ul>"
HTML_TWO_BLOCKS = (
    "<div><ul><li>Banana</li></ul><ul><li>Grape</li></ul></div>"
)
HTML_COMPLEX = """
<div id="main">
  <ul class="fruits">
    <li class="item"><span class="name">Banana</span><a href="/banana" class="link">More</a></li>
    <li class="item"><span class="name">Apple</span><a href="/apple" class="link">More</a></li>
    <li class="item"><span class="name">Orange</span><a href="/orange" class="link">More</a></li>
  </ul>
  <p class="info">Fresh fruits</p>
  <a class="external" href="/shop">Shop Now</a>
</div>
"""
URL = "https://example.com/index.html"


def _build_scraper(**kwargs):
    scraper = AutoScraper()
    scraper.build(html=HTML, wanted_list=["Banana"], **kwargs)
    return scraper


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_legacy(path, scraper, bare=False):
    payload = scraper.stack_list if bare else {"stack_list": scraper.stack_list}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


# --------------------------------------------------------------------------
# Versioned save/load
# --------------------------------------------------------------------------

def test_save_writes_versioned_v2_document(tmp_path):
    scraper = _build_scraper()
    path = tmp_path / "rules.json"
    scraper.save(path)

    payload = _read_json(path)
    assert payload["format"] == FORMAT_NAME
    assert payload["version"] == CURRENT_RULE_VERSION
    assert isinstance(payload["rules"], list) and payload["rules"]

    rule = payload["rules"][0]
    assert set(rule) == {"rule_id", "alias", "hash", "match", "path"}
    assert rule["rule_id"] == scraper.stack_list[0]["stack_id"]
    assert rule["hash"] == scraper.stack_list[0]["hash"]
    assert set(rule["match"]) == {
        "wanted_attr", "is_full_url", "is_non_rec_text", "url",
    }
    assert isinstance(rule["match"]["is_full_url"], bool)
    assert isinstance(rule["match"]["is_non_rec_text"], bool)

    path_entries = rule["path"]
    assert all(entry["position"] is None for entry in path_entries[-1:])
    assert all(
        isinstance(entry["position"], int) and entry["position"] >= 0
        for entry in path_entries[:-1]
    )


def test_v2_round_trip_preserves_identity_alias_and_behavior(tmp_path):
    scraper = AutoScraper()
    scraper.build(html=HTML, wanted_dict={"fruit": ["Banana"]})
    path = tmp_path / "rules.json"
    scraper.save(path)

    loaded = AutoScraper()
    loaded.load(path)
    assert loaded.rule_version == CURRENT_RULE_VERSION

    for original, restored in zip(scraper.stack_list, loaded.stack_list):
        assert original["stack_id"] == restored["stack_id"]
        assert original["hash"] == restored["hash"]
        assert original["alias"] == restored["alias"] == "fruit"
        assert [tuple(entry) for entry in original["content"]] == [
            tuple(entry) for entry in restored["content"]
        ]
        assert original["wanted_attr"] == restored["wanted_attr"]
        assert bool(original["is_full_url"]) == restored["is_full_url"]
        assert bool(original["is_non_rec_text"]) == restored["is_non_rec_text"]

    assert loaded.get_result_exact(html=HTML) == scraper.get_result_exact(html=HTML)
    assert loaded.get_result_similar(
        html=HTML, contain_sibling_leaves=True
    ) == scraper.get_result_similar(html=HTML, contain_sibling_leaves=True)
    assert loaded.get_result_similar(
        html=HTML, grouped=True, contain_sibling_leaves=True
    ) == scraper.get_result_similar(html=HTML, grouped=True, contain_sibling_leaves=True)
    assert loaded.get_result_similar(
        html=HTML, group_by_alias=True, contain_sibling_leaves=True
    ) == scraper.get_result_similar(
        html=HTML, group_by_alias=True, contain_sibling_leaves=True
    )
    assert loaded.get_result_similar(
        html=HTML, contain_sibling_leaves=True, keep_order=True
    ) == scraper.get_result_similar(
        html=HTML, contain_sibling_leaves=True, keep_order=True
    )


def test_save_and_load_v1_format(tmp_path):
    scraper = _build_scraper()
    path = tmp_path / "rules-v1.json"
    scraper.save(path, version=1)

    payload = _read_json(path)
    assert payload["version"] == 1 and payload["format"] == FORMAT_NAME
    assert "content" in payload["rules"][0]

    loaded = AutoScraper()
    loaded.load(path)
    assert loaded.rule_version == 1
    assert loaded.get_result_exact(html=HTML) == scraper.get_result_exact(html=HTML)
    assert loaded.get_result_similar(
        html=HTML, contain_sibling_leaves=True
    ) == scraper.get_result_similar(html=HTML, contain_sibling_leaves=True)


def test_load_legacy_dict_and_bare_list(tmp_path):
    scraper = _build_scraper()
    for bare in (False, True):
        path = tmp_path / ("legacy-%s.json" % bare)
        _write_legacy(path, scraper, bare=bare)

        loaded = AutoScraper()
        loaded.load(path)
        assert loaded.rule_version == LEGACY_VERSION
        assert loaded.get_result_exact(html=HTML) == scraper.get_result_exact(html=HTML)
        assert loaded.get_result_similar(
            html=HTML, contain_sibling_leaves=True
        ) == scraper.get_result_similar(html=HTML, contain_sibling_leaves=True)


def test_legacy_load_then_save_upgrades_without_losing_rules(tmp_path):
    scraper = _build_scraper()
    legacy_path = tmp_path / "legacy.json"
    _write_legacy(legacy_path, scraper)

    loaded = AutoScraper()
    loaded.load(legacy_path)
    upgraded_path = tmp_path / "upgraded.json"
    loaded.save(upgraded_path)

    payload = _read_json(upgraded_path)
    assert payload["version"] == CURRENT_RULE_VERSION
    assert len(payload["rules"]) == len(scraper.stack_list)
    assert AutoScraper().load  # sanity: loadable
    again = AutoScraper()
    again.load(upgraded_path)
    assert again.get_result_exact(html=HTML) == scraper.get_result_exact(html=HTML)


# --------------------------------------------------------------------------
# Migration entry point and change manifest
# --------------------------------------------------------------------------

def test_migration_writes_new_file_and_keeps_original(tmp_path):
    scraper = _build_scraper()
    src = tmp_path / "legacy.json"
    dst = tmp_path / "migrated.json"
    _write_legacy(src, scraper)
    original_bytes = src.read_bytes()

    report = migrate_file(src, dst, pages=[HTML])

    assert src.read_bytes() == original_bytes
    payload = _read_json(dst)
    assert payload["format"] == FORMAT_NAME
    assert payload["version"] == CURRENT_RULE_VERSION
    assert len(payload["rules"]) == len(scraper.stack_list)

    plan = report["plan"]
    assert plan["source_version"] == LEGACY_VERSION
    assert plan["target_version"] == CURRENT_RULE_VERSION
    assert plan["rule_count"] == len(scraper.stack_list)
    actions = {change["action"] for change in plan["changes"]}
    assert {"wrapper", "normalize", "restructure"} <= actions
    assert report["output_path"] == str(dst)
    assert report["verification"]["verified"] is True


def test_migration_manifest_is_available_without_writing(tmp_path):
    scraper = _build_scraper()
    src = tmp_path / "legacy.json"
    _write_legacy(src, scraper)

    plan = AutoScraper.plan_rule_migration(src)
    assert plan["rule_count"] == len(scraper.stack_list)
    assert not (tmp_path / "migrated.json").exists()
    assert all(change["rule_id"] or change["action"] == "wrapper"
               for change in plan["changes"])


def test_migration_refuses_same_path_and_existing_destination(tmp_path):
    scraper = _build_scraper()
    src = tmp_path / "legacy.json"
    dst = tmp_path / "migrated.json"
    _write_legacy(src, scraper)

    with pytest.raises(RuleFormatError):
        migrate_file(src, src)

    migrate_file(src, dst)
    with pytest.raises(FileExistsError):
        migrate_file(src, dst)

    # overwrite=True is the explicit escape hatch
    report = migrate_file(src, dst, overwrite=True, pages=[HTML])
    assert report["verification"]["verified"] is True


def test_migration_v1_to_v2(tmp_path):
    scraper = _build_scraper()
    src = tmp_path / "v1.json"
    scraper.save(src, version=1)
    dst = tmp_path / "v2.json"

    report = AutoScraper.migrate_rule_file(src, dst, pages=[HTML])
    assert _read_json(dst)["version"] == CURRENT_RULE_VERSION
    assert report["plan"]["source_version"] == 1
    assert report["verification"]["verified"] is True

    migrated = AutoScraper()
    migrated.load(dst)
    assert migrated.get_result_exact(html=HTML) == scraper.get_result_exact(html=HTML)


def test_migration_preserves_full_url_attribute_matching(tmp_path):
    scraper = AutoScraper()
    scraper.build(url=URL, html=HTML_COMPLEX, wanted_list=["https://example.com/apple"])
    src = tmp_path / "legacy.json"
    _write_legacy(src, scraper)
    dst = tmp_path / "v2.json"

    report = migrate_file(src, dst, pages=[{"html": HTML_COMPLEX, "url": URL}])
    assert report["verification"]["verified"] is True

    payload = _read_json(dst)
    full_url_rules = [
        rule for rule in payload["rules"] if rule["match"]["is_full_url"]
    ]
    assert full_url_rules
    rule = full_url_rules[0]
    assert rule["match"]["wanted_attr"] == "href"
    assert rule["match"]["url"] == URL

    migrated = AutoScraper()
    migrated.load(dst)
    assert migrated.get_result_exact(
        html=HTML_COMPLEX, url=URL
    ) == scraper.get_result_exact(html=HTML_COMPLEX, url=URL)
    assert migrated.get_result_similar(
        html=HTML_COMPLEX, url=URL, contain_sibling_leaves=True
    ) == scraper.get_result_similar(
        html=HTML_COMPLEX, url=URL, contain_sibling_leaves=True
    )


# --------------------------------------------------------------------------
# Behavior verification against local pages
# --------------------------------------------------------------------------

def test_verification_equivalent_across_distinct_local_pages(tmp_path):
    page_one = (
        "<div><ul class='list'><li>Banana</li><li>Apple</li></ul>"
        "<ul class='list'><li>Grape</li></ul></div>"
    )
    page_two = (
        "<div><ul class='list'><li>Banana</li><li>Apple</li></ul>"
        "<ul class='list'><li>Melon</li></ul></div>"
    )
    scraper = AutoScraper()
    scraper.build(html=page_one, wanted_list=["Banana"])
    src = tmp_path / "legacy.json"
    _write_legacy(src, scraper)
    dst = tmp_path / "v2.json"

    report = migrate_file(src, dst, pages=[page_one, page_two])
    verification = report["verification"]
    assert verification["verified"] is True
    assert len(verification["pages"]) == 2
    for page in verification["pages"]:
        assert page["equivalent"] is True
        assert page["mismatched_modes"] == []
        for mode, result in page["modes"].items():
            assert result["equivalent"] is True
            assert result["source"] == result["migrated"]


def test_verification_without_pages_is_left_to_caller(tmp_path):
    scraper = _build_scraper()
    src = tmp_path / "legacy.json"
    _write_legacy(src, scraper)
    dst = tmp_path / "v2.json"

    report = migrate_file(src, dst, pages=[])
    verification = report["verification"]
    assert verification["verified"] is False
    assert verification["reason"]
    assert verification["pages"] == []


def test_verification_detects_position_tampering():
    scraper = AutoScraper()
    scraper.build(html=HTML_TWO_BLOCKS, wanted_list=["Banana"])
    document = parse_document(_runtime_v1_payload(scraper))
    plan = plan_migration(document)

    tampered_rules = json.loads(json.dumps(plan.converted_rules))
    for entry in tampered_rules[0]["path"][:-1]:
        entry["position"] += 1

    report = verify_behavior(
        document,
        _document(2, tampered_rules),
        [HTML_TWO_BLOCKS],
    )
    assert report.verified is False
    assert report.pages[0]["mismatched_modes"]


def test_verification_detects_tag_tampering():
    scraper = AutoScraper()
    scraper.build(html=HTML_TWO_BLOCKS, wanted_list=["Banana"])
    document = parse_document(_runtime_v1_payload(scraper))
    plan = plan_migration(document)

    tampered_rules = json.loads(json.dumps(plan.converted_rules))
    tampered_rules[0]["path"][-1]["tag"] = "p"

    report = verify_behavior(document, _document(2, tampered_rules), [HTML_TWO_BLOCKS])
    assert report.verified is False
    assert report.pages[0]["mismatched_modes"]


def _runtime_v1_payload(scraper):
    from autoscraper.rules import build_document

    return build_document(scraper.stack_list, 1)


def _document(version, rules):
    from autoscraper.rules import RuleDocument

    return RuleDocument(version, rules)


# --------------------------------------------------------------------------
# Strict validation: unknown versions, missing fields, contradictions
# --------------------------------------------------------------------------

def _v2_rule(rule_id="rule_aaaaaaaa", rule_hash="a" * 64, tag="a"):
    return {
        "rule_id": rule_id,
        "alias": "",
        "hash": rule_hash,
        "match": {
            "wanted_attr": None,
            "is_full_url": False,
            "is_non_rec_text": False,
            "url": "",
        },
        "path": [{"tag": tag, "attrs": {"class": "", "style": ""}, "position": None}],
    }


def test_reject_unknown_version():
    with pytest.raises(RuleFormatError, match="unknown rule file version"):
        parse_document({"format": FORMAT_NAME, "version": 99, "rules": []})


def test_reject_wrong_format_marker():
    with pytest.raises(RuleFormatError, match="format marker"):
        parse_document({"format": "nope", "version": 2, "rules": []})


def test_reject_top_level_without_markers():
    with pytest.raises(RuleFormatError):
        parse_document({"something": "else"})
    with pytest.raises(RuleFormatError):
        parse_document("not-an-object")


@pytest.mark.parametrize(
    "rule",
    [
        {},
        {"rule_id": "rule_aaaaaaaa", "alias": "", "hash": "a" * 64},
        {
            "rule_id": "rule_aaaaaaaa",
            "alias": "",
            "hash": "a" * 64,
            "match": {},
            "path": [{"tag": "a", "attrs": {}, "position": None}],
        },
        {
            "rule_id": "rule_aaaaaaaa",
            "alias": "",
            "hash": "a" * 64,
            "match": {
                "wanted_attr": None,
                "is_full_url": False,
                "is_non_rec_text": False,
                "url": "",
            },
            "path": [],
        },
    ],
)
def test_reject_missing_v2_fields(rule):
    with pytest.raises(RuleFormatError):
        parse_document({"format": FORMAT_NAME, "version": 2, "rules": [rule]})


def test_reject_contradictory_rule_id_and_hash():
    rule = _v2_rule(rule_id="rule_deadbeef", rule_hash="a" * 64)
    with pytest.raises(RuleFormatError, match="contradicts hash"):
        parse_document({"format": FORMAT_NAME, "version": 2, "rules": [rule]})


def test_reject_invalid_hash_digest():
    rule = _v2_rule(rule_id="rule_zzzzzzzz", rule_hash="z" * 64)
    with pytest.raises(RuleFormatError, match="hexadecimal"):
        parse_document({"format": FORMAT_NAME, "version": 2, "rules": [rule]})


def test_reject_duplicate_identities():
    # Internally consistent identities (id == hash prefix) that still collide.
    hash_one = "a" * 8 + "1" * 56
    hash_two = "a" * 8 + "2" * 56
    rules = [
        _v2_rule(rule_id="rule_aaaaaaaa", rule_hash=hash_one, tag="a"),
        _v2_rule(rule_id="rule_aaaaaaaa", rule_hash=hash_two, tag="p"),
    ]
    with pytest.raises(RuleFormatError, match="duplicate rule id"):
        parse_document({"format": FORMAT_NAME, "version": 2, "rules": rules})

    distinct_hash_rules = [
        _v2_rule(rule_id="rule_aaaaaaaa", rule_hash=hash_one, tag="a"),
        _v2_rule(
            rule_id="rule_bbbbbbbb", rule_hash="b" * 8 + "2" * 56, tag="p"
        ),
    ]
    document = parse_document(
        {"format": FORMAT_NAME, "version": 2, "rules": distinct_hash_rules}
    )
    assert len(document.rules) == 2


def test_reject_contradictory_full_url_description():
    rule = _v2_rule()
    rule["match"] = {
        "wanted_attr": "href",
        "is_full_url": True,
        "is_non_rec_text": False,
        "url": "",
    }
    with pytest.raises(RuleFormatError, match="is_full_url=true requires url"):
        parse_document({"format": FORMAT_NAME, "version": 2, "rules": [rule]})

    rule["match"]["url"] = URL
    rule["match"]["wanted_attr"] = None
    with pytest.raises(RuleFormatError, match="is_full_url=true requires wanted_attr"):
        parse_document({"format": FORMAT_NAME, "version": 2, "rules": [rule]})


def test_reject_bad_positions_and_leaf_position():
    rule = _v2_rule()
    rule["path"] = [
        {"tag": "ul", "attrs": {}, "position": -1},
        {"tag": "li", "attrs": {}, "position": None},
    ]
    with pytest.raises(RuleFormatError, match="non-negative integer"):
        parse_document({"format": FORMAT_NAME, "version": 2, "rules": [rule]})

    rule["path"] = [
        {"tag": "ul", "attrs": {}, "position": 0},
        {"tag": "li", "attrs": {}, "position": 1},
    ]
    with pytest.raises(RuleFormatError, match="leaf entry must have position null"):
        parse_document({"format": FORMAT_NAME, "version": 2, "rules": [rule]})


def test_reject_non_boolean_flags_and_bad_attrs():
    rule = _v2_rule()
    rule["match"]["is_full_url"] = "yes"
    with pytest.raises(RuleFormatError, match="must be a boolean"):
        parse_document({"format": FORMAT_NAME, "version": 2, "rules": [rule]})

    rule = _v2_rule()
    rule["path"][0]["attrs"] = ["not", "a", "dict"]
    with pytest.raises(RuleFormatError, match="attrs must be an object"):
        parse_document({"format": FORMAT_NAME, "version": 2, "rules": [rule]})


def test_reject_legacy_rule_missing_content():
    with pytest.raises(RuleFormatError, match="missing content"):
        parse_document({"stack_list": [{"alias": "x"}]})


def test_runtime_rules_round_trip_through_all_versions():
    from autoscraper.rules import build_document

    scraper = _build_scraper()
    expected_ids = [stack["stack_id"] for stack in scraper.stack_list]
    for version in (1, CURRENT_RULE_VERSION):
        payload = build_document(scraper.stack_list, version)
        document = parse_document(payload)
        restored = runtime_rules(document)
        assert [stack["stack_id"] for stack in restored] == expected_ids
        assert AutoScraper(runtime_rules(document)).get_result_exact(html=HTML) == (
            scraper.get_result_exact(html=HTML)
        )
