"""
Reporting helpers: turn verification results into JSON lines or human text.
"""

import json

from .errors import CorruptionError


def result_to_jsonl(result):
    return json.dumps(result.to_dict(), ensure_ascii=False)


def summarize(results):
    summary = {"ok": 0, "index_inconsistent": 0, "corruption": 0, "total": 0}
    for result in results:
        summary["total"] += 1
        summary[result.status] += 1
    return summary


def render_text(results):
    lines = []
    for result in results:
        entry = result.entry
        if result.ok:
            lines.append("OK       offset={0:<8} id={1} url={2}".format(
                entry.offset, entry.record_id, entry.url))
            continue

        error = result.error
        tag = "CORRUPT" if isinstance(error, CorruptionError) else "BAD-INDEX"
        lines.append("{0} offset={1:<8} {2}".format(
            tag, entry.offset, error.message))
        for key, value in sorted(error.detail.items()):
            lines.append("           {0}: {1}".format(key, value))

    summary = summarize(results)
    lines.append("")
    lines.append(
        "summary: {total} checked, {ok} ok, "
        "{index_inconsistent} index inconsistencies, {corruption} corruptions"
        .format(**summary))
    return "\n".join(lines)


def exit_code(results):
    """0 = all ok, 1 = bad index, 2 = corruption, 3 = both."""
    summary = summarize(results)
    code = 0
    if summary["index_inconsistent"]:
        code |= 1
    if summary["corruption"]:
        code |= 2
    return code
