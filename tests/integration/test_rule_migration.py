import json

from autoscraper import AutoScraper
from autoscraper.rules import RuleFormatError, load_document

PAGE_ONE = """
<div id='product'>
  <h1 class='title'>Sony PlayStation 4 Pro</h1>
  <span class='price'>US $349.99</span>
  <a class='deal' href='/deal/ps4'>See deal</a>
</div>
"""

PAGE_TWO = """
<div id='product'>
  <h1 class='title'>Acer Predator Helios 300</h1>
  <span class='price'>US $1,229.49</span>
  <a class='deal' href='/deal/laptop'>See deal</a>
</div>
"""

URL = "https://shop.example.com/item/ps4"


def _legacy_file(path, scraper):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"stack_list": scraper.stack_list}, handle)


def test_legacy_rules_migrate_losslessly_across_local_pages(tmp_path):
    scraper = AutoScraper()
    scraper.build(
        url=URL,
        html=PAGE_ONE,
        wanted_dict={
            "title": ["Sony PlayStation 4 Pro"],
            "price": ["US $349.99"],
            "deal": ["https://shop.example.com/deal/ps4"],
        },
    )
    src = tmp_path / "legacy-rules.json"
    dst = tmp_path / "rules-v2.json"
    _legacy_file(src, scraper)

    report = AutoScraper.migrate_rule_file(
        src,
        dst,
        pages=[
            {"html": PAGE_ONE, "url": URL},
            {"html": PAGE_TWO, "url": "https://shop.example.com/item/laptop"},
        ],
    )

    assert report["plan"]["source_version"] == 0
    assert report["plan"]["target_version"] == 2
    assert report["verification"]["verified"] is True
    assert len(report["verification"]["pages"]) == 2
    assert src.exists()  # original rule file untouched

    document = load_document(dst)
    assert document.version == 2
    aliases = {rule["alias"] for rule in document.rules}
    assert {"title", "price", "deal"} <= aliases
    assert any(rule["match"]["is_full_url"] for rule in document.rules)

    original = AutoScraper()
    original.load(src)
    migrated = AutoScraper()
    migrated.load(dst)

    for page_html, page_url in (
        (PAGE_ONE, URL),
        (PAGE_TWO, "https://shop.example.com/item/laptop"),
    ):
        assert migrated.get_result_exact(
            html=page_html, url=page_url
        ) == original.get_result_exact(html=page_html, url=page_url)
        assert migrated.get_result_similar(
            html=page_html, url=page_url, contain_sibling_leaves=True
        ) == original.get_result_similar(
            html=page_html, url=page_url, contain_sibling_leaves=True
        )
        assert migrated.get_result_similar(
            html=page_html, url=page_url, group_by_alias=True,
            contain_sibling_leaves=True,
        ) == original.get_result_similar(
            html=page_html, url=page_url, group_by_alias=True,
            contain_sibling_leaves=True,
        )

    grouped = migrated.get_result_similar(
        html=PAGE_TWO, url="https://shop.example.com/item/laptop",
        group_by_alias=True, contain_sibling_leaves=True,
    )
    assert "US $1,229.49" in grouped["price"]
    assert "https://shop.example.com/deal/laptop" in grouped["deal"]


def test_migration_manifest_lists_every_rule_before_writing(tmp_path):
    scraper = AutoScraper()
    scraper.build(html=PAGE_ONE, wanted_list=["US $349.99"])
    src = tmp_path / "legacy-rules.json"
    _legacy_file(src, scraper)

    plan = AutoScraper.plan_rule_migration(src)
    rule_ids = {stack["stack_id"] for stack in scraper.stack_list}
    manifest_ids = {
        change["rule_id"]
        for change in plan["changes"]
        if change["action"] == "restructure"
    }
    assert manifest_ids == rule_ids
    assert not (tmp_path / "rules-v2.json").exists()


def test_corrupt_legacy_file_is_rejected_not_migrated(tmp_path):
    src = tmp_path / "corrupt.json"
    src.write_text('{"stack_list": [{"alias": "x"}]}', encoding="utf-8")
    dst = tmp_path / "out.json"

    try:
        AutoScraper.migrate_rule_file(src, dst)
    except RuleFormatError:
        pass
    else:
        raise AssertionError("corrupt rule file must be rejected")
    assert not dst.exists()
