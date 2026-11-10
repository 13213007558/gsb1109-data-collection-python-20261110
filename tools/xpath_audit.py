"""Offline audit tool for upgrading XPath extraction rules.

Compares an old and a new XPath rule set against a directory of local HTML
pages using Parsel, and reports, for every page and rule:

* value changes (including multiplicity, i.e. hit-count changes),
* order changes (only for rules that declare ``ordered: true``),
* node-position changes, split into two distinct cases:

  * ``node_moved``: the extracted values are identical, but they now come
    from different nodes,
  * ``text_changed``: the same nodes are matched, but their extracted
    values differ,

* pages that fail to parse, listed separately.

Extraction itself is always performed by Parsel (``Selector.xpath`` +
``getall``); node positions are read-only metadata collected from the same
lxml tree, so Parsel's extraction semantics are not modified.

Usage:
    python3 tools/xpath_audit.py rules.json pages/ -o report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from parsel import Selector
except ImportError:  # pragma: no cover - running as a loose script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from parsel import Selector

from lxml import etree

STATUS_UNCHANGED = "unchanged"
STATUS_ORDER_CHANGED = "order_changed"
STATUS_NODE_MOVED = "node_moved"
STATUS_TEXT_CHANGED = "text_changed"
STATUS_CHANGED = "changed"
STATUS_RULE_ERROR = "rule_error"

ALL_STATUSES = (
    STATUS_UNCHANGED,
    STATUS_ORDER_CHANGED,
    STATUS_NODE_MOVED,
    STATUS_TEXT_CHANGED,
    STATUS_CHANGED,
    STATUS_RULE_ERROR,
)

SNIPPET_LIMIT = 200
MAX_SNIPPETS = 3


@dataclass
class RuleSpec:
    """One named rule: the old XPath, the new XPath, and whether the order
    of the extracted values is significant."""

    name: str
    old: str
    new: str
    ordered: bool = True


@dataclass
class Hit:
    """A single extracted value plus read-only positional metadata."""

    value: str
    node: str | None  # path of the element the value comes from
    locator: str  # finer location, e.g. an attribute or text node
    snippet: str | None  # minimal serialized fragment for review


@dataclass
class Extraction:
    hits: list[Hit] = field(default_factory=list)
    error: str | None = None


@dataclass
class RuleDiff:
    rule: RuleSpec
    status: str
    old_hits: list[Hit]
    new_hits: list[Hit]
    detail: dict[str, Any]


@dataclass
class PageAudit:
    page: str
    parse_error: str | None
    diffs: list[RuleDiff] = field(default_factory=list)


def _shorten(text: str, limit: int = SNIPPET_LIMIT) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _node_metadata(tree: etree._ElementTree, raw: Any) -> tuple[str | None, str, str | None]:
    """Return (element path, fine-grained locator, snippet) for one raw
    lxml xpath result. Strings produced by text()/attribute steps are
    attributed to their parent element."""
    if isinstance(raw, etree._Element):
        path = tree.getpath(raw)
        snippet = _shorten(etree.tostring(raw, encoding="unicode", method="html"))
        return path, path, snippet
    parent = getattr(raw, "getparent", lambda: None)()
    if parent is None:
        return None, "", None
    path = tree.getpath(parent)
    snippet = _shorten(etree.tostring(parent, encoding="unicode", method="html"))
    if getattr(raw, "is_attribute", False):
        locator = f"{path}/@{raw.attrname}"
    elif getattr(raw, "is_tail", False):
        locator = f"{path}/text()[tail]"
    else:
        locator = f"{path}/text()"
    return path, locator, snippet


def extract(selector: Selector, query: str) -> Extraction:
    """Extract values with Parsel and, in parallel, node positions with a
    read-only lxml evaluation of the same query on the same tree."""
    try:
        values = selector.xpath(query).getall()
    except ValueError as exc:
        return Extraction(error=str(exc))
    raw_results = etree.XPath(
        query, namespaces=selector.namespaces, smart_strings=True
    )(selector.root)
    if not isinstance(raw_results, list):
        raw_results = [raw_results]
    tree = selector.root.getroottree()
    hits = []
    for value, raw in zip(values, raw_results):
        node, locator, snippet = _node_metadata(tree, raw)
        hits.append(Hit(value=value, node=node, locator=locator, snippet=snippet))
    return Extraction(hits=hits)


def _counter_diff(old: list[str], new: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    old_counts = Counter(old)
    new_counts = Counter(new)
    removed = [
        {"value": value, "count": count}
        for value, count in sorted((old_counts - new_counts).items())
    ]
    added = [
        {"value": value, "count": count}
        for value, count in sorted((new_counts - old_counts).items())
    ]
    return removed, added


def compare_extractions(old: Extraction, new: Extraction, ordered: bool) -> tuple[str, dict[str, Any]]:
    """Classify the difference between two extractions of the same page."""
    old_values = [hit.value for hit in old.hits]
    new_values = [hit.value for hit in new.hits]
    old_nodes = [hit.node for hit in old.hits]
    new_nodes = [hit.node for hit in new.hits]

    removed, added = _counter_diff(old_values, new_values)
    detail: dict[str, Any] = {
        "old_count": len(old_values),
        "new_count": len(new_values),
        "multiplicity_changed": len(old_values) != len(new_values),
        "removed_values": removed,
        "added_values": added,
    }

    if ordered:
        values_equal = old_values == new_values
        nodes_equal = old_nodes == new_nodes
        same_value_multiset = Counter(old_values) == Counter(new_values)
    else:
        values_equal = Counter(old_values) == Counter(new_values)
        nodes_equal = Counter(old_nodes) == Counter(new_nodes)
        same_value_multiset = values_equal

    if values_equal and nodes_equal:
        status = STATUS_UNCHANGED
    elif values_equal:
        status = STATUS_NODE_MOVED
        detail["moved"] = [
            {"value": o.value, "old_node": o.node, "new_node": n.node}
            for o, n in zip(old.hits, new.hits)
            if o.node != n.node
        ]
    elif nodes_equal:
        status = STATUS_TEXT_CHANGED
        detail["text_changes"] = [
            {"node": o.node, "old_value": o.value, "new_value": n.value}
            for o, n in zip(old.hits, new.hits)
            if o.value != n.value
        ]
    elif ordered and same_value_multiset:
        status = STATUS_ORDER_CHANGED
        detail["old_order"] = old_values
        detail["new_order"] = new_values
        detail["nodes_changed"] = old_nodes != new_nodes
    else:
        status = STATUS_CHANGED
    return status, detail


def _is_empty_document(selector: Selector) -> bool:
    root = selector.root
    return (
        isinstance(root, etree._Element)
        and len(root) == 0
        and not (root.text or "").strip()
    )


def audit_page(path: Path, rules: list[RuleSpec]) -> PageAudit:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        selector = Selector(text=text)
    except Exception as exc:  # noqa: BLE001 - any parser failure is reported
        return PageAudit(page=path.name, parse_error=f"{type(exc).__name__}: {exc}")
    if _is_empty_document(selector):
        return PageAudit(page=path.name, parse_error="empty or unparseable document")
    diffs = []
    for rule in rules:
        old = extract(selector, rule.old)
        new = extract(selector, rule.new)
        error = old.error or new.error
        if error is not None:
            diffs.append(
                RuleDiff(
                    rule=rule,
                    status=STATUS_RULE_ERROR,
                    old_hits=old.hits,
                    new_hits=new.hits,
                    detail={"error": error},
                )
            )
            continue
        status, detail = compare_extractions(old, new, rule.ordered)
        diffs.append(
            RuleDiff(
                rule=rule,
                status=status,
                old_hits=old.hits,
                new_hits=new.hits,
                detail=detail,
            )
        )
    return PageAudit(page=path.name, parse_error=None, diffs=diffs)


def audit_pages(paths: list[Path], rules: list[RuleSpec]) -> list[PageAudit]:
    return [audit_page(path, rules) for path in paths]


def load_rules(path: Path) -> list[RuleSpec]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data["rules"] if isinstance(data, dict) else data
    rules = []
    for entry in entries:
        rules.append(
            RuleSpec(
                name=entry["name"],
                old=entry["old"],
                new=entry["new"],
                ordered=bool(entry.get("ordered", True)),
            )
        )
    return rules


def collect_pages(paths: list[Path]) -> list[Path]:
    pages: list[Path] = []
    for path in paths:
        if path.is_dir():
            pages.extend(
                sorted(
                    p
                    for p in path.iterdir()
                    if p.suffix.lower() in (".html", ".htm") and p.is_file()
                )
            )
        else:
            pages.append(path)
    return pages


def _snippets(hits: list[Hit]) -> list[str]:
    seen: list[str] = []
    for hit in hits:
        if hit.snippet and hit.snippet not in seen:
            seen.append(hit.snippet)
        if len(seen) >= MAX_SNIPPETS:
            break
    return seen


def _hit_to_dict(hit: Hit) -> dict[str, Any]:
    return {
        "value": hit.value,
        "node": hit.node,
        "locator": hit.locator,
        "snippet": hit.snippet,
    }


def _diff_to_dict(diff: RuleDiff) -> dict[str, Any]:
    return {
        "rule": diff.rule.name,
        "status": diff.status,
        "ordered": diff.rule.ordered,
        "old_xpath": diff.rule.old,
        "new_xpath": diff.rule.new,
        "old_hits": [_hit_to_dict(hit) for hit in diff.old_hits],
        "new_hits": [_hit_to_dict(hit) for hit in diff.new_hits],
        "detail": diff.detail,
    }


def audits_to_dict(audits: list[PageAudit]) -> dict[str, Any]:
    summary = {status: 0 for status in ALL_STATUSES}
    for audit in audits:
        for diff in audit.diffs:
            summary[diff.status] += 1
    return {
        "summary": {
            "pages": len(audits),
            "parse_failures": sum(1 for a in audits if a.parse_error),
            "statuses": summary,
        },
        "pages": [
            {
                "page": audit.page,
                "parse_error": audit.parse_error,
                "diffs": [_diff_to_dict(diff) for diff in audit.diffs],
            }
            for audit in audits
        ],
    }


def render_markdown(audits: list[PageAudit]) -> str:
    data = audits_to_dict(audits)
    lines = [
        "# XPath 规则升级审核报告",
        "",
        f"- 页面总数: {data['summary']['pages']}",
        f"- 解析失败: {data['summary']['parse_failures']}",
    ]
    for status, count in data["summary"]["statuses"].items():
        lines.append(f"- {status}: {count}")
    lines.append("")

    failures = [a for a in audits if a.parse_error]
    if failures:
        lines.append("## 解析失败的页面")
        lines.append("")
        for audit in failures:
            lines.append(f"- `{audit.page}`: {audit.parse_error}")
        lines.append("")

    lines.append("## 规则差异明细")
    for audit in audits:
        if audit.parse_error:
            continue
        lines.append("")
        lines.append(f"### 页面 `{audit.page}`")
        for diff in audit.diffs:
            lines.append("")
            lines.append(f"#### 规则 `{diff.rule.name}` — {diff.status}")
            lines.append("")
            lines.append(f"- 旧规则: `{diff.rule.old}`")
            lines.append(f"- 新规则: `{diff.rule.new}`")
            lines.append(f"- 顺序敏感: {'是' if diff.rule.ordered else '否'}")
            if diff.status == STATUS_RULE_ERROR:
                lines.append(f"- 规则错误: {diff.detail['error']}")
                continue
            lines.append(
                f"- 命中数: {diff.detail['old_count']} → {diff.detail['new_count']}"
            )
            _render_detail(lines, diff)
            _render_snippets(lines, "旧规则命中片段", _snippets(diff.old_hits))
            _render_snippets(lines, "新规则命中片段", _snippets(diff.new_hits))
    lines.append("")
    return "\n".join(lines)


def _render_detail(lines: list[str], diff: RuleDiff) -> None:
    detail = diff.detail
    for item in detail.get("moved", []):
        lines.append(
            f"- 值未变但节点移动: `{_shorten(item['value'], 80)}` "
            f"`{item['old_node']}` → `{item['new_node']}`"
        )
    for item in detail.get("text_changes", []):
        lines.append(
            f"- 节点未变但值变化: `{item['node']}` "
            f"`{_shorten(item['old_value'], 80)}` → `{_shorten(item['new_value'], 80)}`"
        )
    if diff.status == STATUS_ORDER_CHANGED:
        lines.append(f"- 旧顺序: `{detail['old_order']}`")
        lines.append(f"- 新顺序: `{detail['new_order']}`")
    if diff.status == STATUS_TEXT_CHANGED:
        return  # value deltas are already covered by the text_changes lines
    for item in detail.get("removed_values", []):
        lines.append(f"- 减少的值: `{_shorten(item['value'], 80)}` ×{item['count']}")
    for item in detail.get("added_values", []):
        lines.append(f"- 新增的值: `{_shorten(item['value'], 80)}` ×{item['count']}")


def _render_snippets(lines: list[str], title: str, snippets: list[str]) -> None:
    if not snippets:
        return
    lines.append(f"- {title}:")
    for snippet in snippets:
        lines.append(f"  ```html\n  {snippet}\n  ```")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit an XPath rule upgrade against local HTML pages.",
    )
    parser.add_argument("rules", type=Path, help="JSON file with old/new rules")
    parser.add_argument(
        "pages", type=Path, nargs="+", help="HTML files or directories of them"
    )
    parser.add_argument("-o", "--output", type=Path, help="report output file")
    parser.add_argument(
        "--format", choices=("md", "json"), default="md", help="report format"
    )
    args = parser.parse_args(argv)

    rules = load_rules(args.rules)
    pages = collect_pages(args.pages)
    if not pages:
        print("no HTML pages found", file=sys.stderr)
        return 2
    audits = audit_pages(pages, rules)
    if args.format == "json":
        report = json.dumps(audits_to_dict(audits), ensure_ascii=False, indent=2)
    else:
        report = render_markdown(audits)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
