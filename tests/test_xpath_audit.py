import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = Path(__file__).resolve().parent / "xpath_audit_samples"

spec = importlib.util.spec_from_file_location(
    "xpath_audit", REPO_ROOT / "tools" / "xpath_audit.py"
)
xpath_audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = xpath_audit
spec.loader.exec_module(xpath_audit)


@pytest.fixture(scope="module")
def rules():
    return xpath_audit.load_rules(SAMPLES / "rules.json")


def audit_one(rules, page, rule_name):
    audits = xpath_audit.audit_pages([SAMPLES / page], rules)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.parse_error is None
    by_name = {diff.rule.name: diff for diff in audit.diffs}
    return by_name[rule_name]


def test_unchanged(rules):
    diff = audit_one(rules, "unchanged.html", "title")
    assert diff.status == "unchanged"
    assert diff.detail["old_count"] == diff.detail["new_count"] == 1


def test_node_moved_same_value(rules):
    diff = audit_one(rules, "node_moved.html", "price")
    assert diff.status == "node_moved"
    assert diff.old_hits[0].value == diff.new_hits[0].value == "¥25.00"
    assert diff.old_hits[0].node != diff.new_hits[0].node
    moved = diff.detail["moved"]
    assert moved[0]["old_node"].endswith("/span")
    assert moved[0]["new_node"].endswith("/div")


def test_text_changed_same_node(rules):
    diff = audit_one(rules, "text_changed.html", "heading")
    assert diff.status == "text_changed"
    assert diff.old_hits[0].node == diff.new_hits[0].node
    assert diff.old_hits[0].value != diff.new_hits[0].value
    change = diff.detail["text_changes"][0]
    assert change["new_value"] == "龙井茶"


def test_order_changed_when_ordered(rules):
    diff = audit_one(rules, "reordered.html", "nav_ordered")
    assert diff.status == "order_changed"
    assert sorted(diff.detail["old_order"]) == sorted(diff.detail["new_order"])
    assert diff.detail["old_order"] != diff.detail["new_order"]


def test_reorder_ignored_when_unordered(rules):
    """With ordered=false the same reordering must not be flagged as an
    order change; only the node move is reported."""
    diff = audit_one(rules, "reordered.html", "nav_unordered")
    assert diff.status == "node_moved"
    assert "old_order" not in diff.detail


def test_multiplicity_changed(rules):
    diff = audit_one(rules, "multiplicity.html", "products")
    assert diff.status == "changed"
    assert diff.detail["multiplicity_changed"] is True
    assert diff.detail["old_count"] == 4
    assert diff.detail["new_count"] == 3
    assert diff.detail["removed_values"] == [{"value": "茶罐", "count": 1}]


def test_parse_failure_listed_separately(rules):
    audits = xpath_audit.audit_pages([SAMPLES / "broken.html"], rules)
    assert audits[0].parse_error is not None
    assert audits[0].diffs == []
    report = xpath_audit.render_markdown(audits)
    assert "解析失败的页面" in report
    assert "broken.html" in report


def test_rule_error(rules):
    bad = xpath_audit.RuleSpec(name="bad", old="///not-valid[", new="//a")
    audits = xpath_audit.audit_pages([SAMPLES / "unchanged.html"], [bad])
    assert audits[0].diffs[0].status == "rule_error"
    assert "error" in audits[0].diffs[0].detail


def test_report_contains_snippets_and_rule_diff(rules):
    audits = xpath_audit.audit_pages([SAMPLES / "node_moved.html"], rules)
    report = xpath_audit.render_markdown(audits)
    assert "node_moved" in report
    assert '<span class="price" style="display:none">¥25.00</span>' in report
    assert '<div class="price-tag">¥25.00</div>' in report
    assert "//span[@class='price']/text()" in report
    assert "//div[@class='price-tag']/text()" in report


def test_extraction_matches_parsel_getall(rules):
    """The tool must report exactly what Parsel extracts, in Parsel order."""
    from parsel import Selector

    selector = Selector(text=(SAMPLES / "reordered.html").read_text())
    extraction = xpath_audit.extract(selector, "//nav//a/text()")
    assert [hit.value for hit in extraction.hits] == selector.xpath(
        "//nav//a/text()"
    ).getall()


def test_cli_end_to_end(tmp_path, rules):
    md_out = tmp_path / "report.md"
    rc = xpath_audit.main(
        [str(SAMPLES / "rules.json"), str(SAMPLES), "-o", str(md_out)]
    )
    assert rc == 0
    report = md_out.read_text(encoding="utf-8")
    for marker in (
        "unchanged",
        "node_moved",
        "text_changed",
        "order_changed",
        "changed",
        "解析失败的页面",
        "broken.html",
    ):
        assert marker in report

    json_out = tmp_path / "report.json"
    rc = xpath_audit.main(
        [
            str(SAMPLES / "rules.json"),
            str(SAMPLES),
            "-o",
            str(json_out),
            "--format",
            "json",
        ]
    )
    assert rc == 0
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["summary"]["parse_failures"] == 1
    assert data["summary"]["statuses"]["node_moved"] >= 1
    assert data["summary"]["statuses"]["order_changed"] >= 1
    assert data["summary"]["statuses"]["text_changed"] >= 1
