"""Offline lenient-vs-strict audit for Courlan's check_url pipeline.

Runs every sample through the real courlan.check_url under four configurations
and, in parallel, through an instrumented copy of the _check_url gate sequence
that calls the same courlan primitives. The instrumented keep/reject verdict is
asserted against check_url itself, so the recorded traces stay trustworthy.

Outputs (written next to this script):
  results.tsv  one row per sample x configuration
  trace.md     per-sample gate-by-gate evidence
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from urllib.parse import urlsplit

from courlan import check_url
from courlan.clean import clean_query, normalize_path, rebuild_url, scrub_url
from courlan.core import _host_parts
from courlan.filters import (
    ADULT_AND_VIDEOS,
    ALL_PATH_LANGS,
    FEED_FILTER,
    FILE_TYPE,
    HOST_LANG_FILTER,
    INDEX_PAGE_FILTER,
    NOTCRAWLABLE,
    PATH_LANG_FILTER,
    SITE_STRUCTURE,
    basic_filter,
    extension_filter,
    is_navigation_page,
    is_not_crawlable,
    lang_filter,
    path_filter,
    type_filter,
    validate_url,
)
from courlan.clean import TRACKERS_RE
from courlan.langcodes import langcodes_score
from courlan.settings import ALLOWED_PARAMS, BLACKLIST, LANG_PARAMS
from courlan.urlutils import get_tldinfo

HERE = Path(__file__).resolve().parent
CONFIGS = {
    "L": {"strict": False, "language": None},
    "S": {"strict": True, "language": None},
    "L-en": {"strict": False, "language": "en"},
    "S-en": {"strict": True, "language": "en"},
}


def instrumented_check(url: str, strict: bool, language: str | None) -> dict:
    """Mirror core._check_url gate by gate, recording evidence at each step."""
    trace: list[tuple[str, str, str]] = []

    def record(gate: str, ok: bool, detail: str = "") -> None:
        trace.append((gate, "PASS" if ok else "REJECT", detail))

    keep = True
    reject_gate = ""
    canonical = ""
    domain = ""
    try:
        if basic_filter(url) is False:
            record("basic_filter", False, "scheme prefix or length outside 10..499")
            raise ValueError
        record("basic_filter", True)

        scrubbed = scrub_url(url)
        if scrubbed != url:
            record("scrub_url", True, f"{url} -> {scrubbed}")
        else:
            record("scrub_url", True, "no change")

        ok, parsed = validate_url(scrubbed)
        if not ok:
            record("validate_url", False, "scheme/host plausibility failed")
            raise ValueError
        record("validate_url", True, f"netloc={parsed.netloc}")

        hostinfo = _host_parts(parsed.scheme, parsed.netloc.rpartition("@")[2])
        if hostinfo is None:
            record("domain_filter", False, "unsuitable host name")
            raise ValueError
        scheme, host, label, regdom = hostinfo
        record("domain_filter", True, f"host={host} label={label} registrable={regdom}")

        path = normalize_path(parsed.path)
        if path != parsed.path:
            record("normalize_path", True, f"{parsed.path} -> {path}")
        else:
            record("normalize_path", True, path or "(empty)")

        if extension_filter(path) is False:
            record("extension_filter", False, f"non-whitelisted extension on {path}")
            raise ValueError
        record("extension_filter", True)

        pre_normalized = scrubbed
        if type_filter(scrubbed, strict=strict, with_nav=False) is False:
            reasons = []
            head = scrubbed.partition("?")[0].partition("#")[0]
            if ("feed" in scrubbed or "rss" in scrubbed) and FEED_FILTER.search(head):
                reasons.append("feed/rss pattern")
            if SITE_STRUCTURE.search(scrubbed):
                reasons.append("site-structure pattern")
            if strict and FILE_TYPE.search(scrubbed):
                reasons.append(f"file/image type marker {FILE_TYPE.search(scrubbed)[0]!r}")
            if strict and ADULT_AND_VIDEOS.search(scrubbed):
                reasons.append("adult/video keyword")
            record("type_filter#1", False, "; ".join(reasons))
            raise ValueError
        record("type_filter#1", True)

        try:
            query = clean_query(parsed.query, strict, language)
        except ValueError:
            record("clean_query", False, f"lang param contradicts target {language!r}")
            raise
        qdetail = query or "(empty)"
        record("clean_query", True, qdetail)

        if strict and path_filter(path, query) is False:
            reasons = []
            if NOTCRAWLABLE.search(path):
                reasons.append("not-crawlable (contact/imprint/login)")
            if INDEX_PAGE_FILTER.match(path) and not query:
                reasons.append("index/default/home page without query")
            record("path_filter", False, "; ".join(reasons))
            raise ValueError
        if strict:
            record("path_filter", True)

        canonical = rebuild_url(
            scheme, host, path, query, parsed.fragment, strict, language, True
        )
        record("rebuild_url", True, canonical)

        if canonical != pre_normalized and type_filter(
            canonical, strict=strict, with_nav=False
        ) is False:
            record("type_filter#2", False, "rebuilt URL now matches a rejected pattern")
            raise ValueError
        if canonical != pre_normalized:
            record("type_filter#2", True, "rebuilt URL re-checked")

        if language is not None:
            score_trace = lang_evidence(canonical, language)
            ok_lang = lang_filter(canonical, language, strict, True)
            record(
                "lang_filter",
                ok_lang,
                f"strict={strict} {score_trace}",
            )
            if not ok_lang:
                raise ValueError

        if not regdom or (strict and (label in BLACKLIST or regdom in BLACKLIST)):
            if regdom and (label in BLACKLIST or regdom in BLACKLIST):
                record("blacklist", False, f"label={label} domain={regdom}")
                raise ValueError
            record("registrable_domain", False, "no registrable domain (PSL suffix)")
            raise ValueError
        record("blacklist", True, f"label={label} not blacklisted")
        domain = regdom

    except (AttributeError, ValueError):
        keep = False
        reject_gate = next(
            (gate for gate, verdict, _ in trace if verdict == "REJECT"), "unknown"
        )

    return {
        "keep": keep,
        "canonical": canonical,
        "domain": domain,
        "reject_gate": reject_gate,
        "trace": trace,
    }


def lang_evidence(url: str, language: str) -> str:
    parts = []
    if PATH_LANG_FILTER.match(url):
        for lang, terr in ALL_PATH_LANGS.findall(url):
            segment = lang + terr
            parts.append(f"path:{segment}={langcodes_score(language, segment):+d}")
    match = HOST_LANG_FILTER.match(url)
    if match:
        parts.append(
            f"host:{match[1]}={'+' if match[1].lower() == language else '-'}1"
        )
    return " ".join(parts) or "no cue"


def query_audit(url: str, language: str | None) -> str:
    query = urlsplit(url).query
    if not query:
        return "no query"
    items = []
    for segment in query.split("&"):
        key = segment.split("=", 1)[0]
        low = key.lower()
        strict_kept = low in ALLOWED_PARAMS or low in LANG_PARAMS
        tracker = bool(TRACKERS_RE.search(low))
        items.append(
            f"{key}: lenient={'drop(tracker)' if tracker else 'keep'}"
            f"/strict={'keep' if strict_kept else 'drop'}"
        )
    try:
        strict_q = clean_query(query, True, language)
        lenient_q = clean_query(query, False, language)
    except ValueError:
        return (
            "; ".join(items)
            + f" || clean_query RAISES (lang param contradicts target {language!r})"
        )
    return "; ".join(items) + f" || lenient->'{lenient_q}' strict->'{strict_q}'"


def main() -> None:
    samples = []
    with (HERE / "samples.tsv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            samples.append(row)

    result_rows = []
    trace_blocks = []
    mismatches = []

    for sample in samples:
        sid, url = sample["id"], sample["url"]
        tld_label, tld_domain = get_tldinfo(url)
        block = [
            f"### {sid} [{sample['category']}] {url}",
            f"intent: {sample['intent']}",
            (
                f"attribution: label={tld_label} registrable={tld_domain} | "
                f"navigation={is_navigation_page(url)} not_crawlable={is_not_crawlable(url)}"
            ),
        ]
        if urlsplit(url).query:
            block.append(f"query audit: {query_audit(url, 'en')}")
        for lang in ("en", "de"):
            block.append(
                f"lang_filter({lang}): lenient={lang_filter(url, lang, False, True)} "
                f"strict={lang_filter(url, lang, True, True)} | {lang_evidence(url, lang)}"
            )

        for name, cfg in CONFIGS.items():
            real = check_url(url, **cfg)
            audited = instrumented_check(url, cfg["strict"], cfg["language"])
            if bool(real) != audited["keep"]:
                mismatches.append((sid, name, real, audited))
            result_rows.append(
                {
                    "id": sid,
                    "category": sample["category"],
                    "config": name,
                    "kept": int(audited["keep"]),
                    "reject_gate": audited["reject_gate"],
                    "canonical": audited["canonical"],
                    "domain": audited["domain"],
                    "real_check_url": "None" if real is None else real[0],
                }
            )
            verdict = "KEEP" if audited["keep"] else f"DROP@{audited['reject_gate']}"
            block.append(
                f"- {name}: {verdict}"
                + (f" -> {audited['canonical']} [{audited['domain']}]" if audited["keep"] else "")
            )
            for gate, state, detail in audited["trace"]:
                block.append(f"    - {gate}: {state} {detail}".rstrip())
        trace_blocks.append("\n".join(block))

    with (HERE / "results.tsv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(result_rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(result_rows)

    with (HERE / "trace.md").open("w", encoding="utf-8") as fh:
        fh.write("# Gate-by-gate trace (courlan 1.4.0, trailing_slash=True)\n\n")
        fh.write("\n\n".join(trace_blocks))
        fh.write("\n")

    total = len(samples)
    print(f"samples={total} configs={len(CONFIGS)} rows={len(result_rows)}")
    for name in CONFIGS:
        kept = sum(r["kept"] for r in result_rows if r["config"] == name)
        print(f"{name:>4}: kept {kept:>2}/{total} dropped {total - kept:>2}")
    if mismatches:
        print("SELF-CHECK MISMATCHES:")
        for item in mismatches:
            print(" ", item)
        raise SystemExit(1)
    print("self-check OK: instrumented verdicts match courlan.check_url on every row")


if __name__ == "__main__":
    main()
