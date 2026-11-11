#!/usr/bin/env python3
"""Offline compatibility probe: mechanize form handling vs HTML form semantics.

Serves the minimal pages in probe/pages/ through an in-process mechanize
handler (no sockets, fully offline), drives mechanize.Browser through six
scenario cases, and records every generated request into probe/results/
(client-side Request dump plus handler-side wire copy). Each case compares
the observed request against the expected HTML form-submission semantics
and stores the assertions as JSON.

Nothing here patches mechanize; failing cases are kept as evidence.

Run:  .venv/bin/python probe/run_probe.py
"""

import io
import json
import os
import sys
from urllib.parse import parse_qsl, urljoin, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mechanize  # noqa: E402
from mechanize._response import make_response  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
PAGES = os.path.join(ROOT, "pages")
RESULTS = os.path.join(ROOT, "results")
WIRE = os.path.join(RESULTS, "wire")

MECHANIZE_VERSION = ".".join(str(p) for p in mechanize.__version__[:3])

BASE = "http://probe.local"
WIRE_LOG = []


class ProbeHandler(mechanize.BaseHandler):
    """Serves probe pages and records every submitted request in-process."""

    handler_order = -1  # intercept before the real HTTPHandler

    def http_open(self, req):
        split = urlsplit(req.get_full_url())
        if split.path.startswith("/pages/"):
            name = split.path.rsplit("/", 1)[-1]
            with open(os.path.join(PAGES, name), "rb") as fh:
                data = fh.read()
            return make_response(
                data, [("Content-Type", "text/html; charset=utf-8")],
                req.get_full_url(), 200, "OK")
        seq = len(WIRE_LOG) + 1
        body = req.data or b""
        if isinstance(body, str):
            body = body.encode("utf-8")
        body_file = "%03d_body.bin" % seq
        with open(os.path.join(WIRE, body_file), "wb") as fh:
            fh.write(body)
        WIRE_LOG.append({
            "seq": seq,
            "method": req.get_method(),
            "path": split.path,
            "query_pairs": parse_qsl(split.query, keep_blank_values=True),
            "content_type": req.get_header("Content-type"),
            "body_file": "wire/" + body_file,
        })
        return make_response(b"recorded", [("Content-Type", "text/plain")],
                             req.get_full_url(), 200, "OK")


def reset_wire_dir():
    os.makedirs(WIRE, exist_ok=True)
    for old in os.listdir(WIRE):
        os.remove(os.path.join(WIRE, old))


# ---------------------------------------------------------------- helpers

def new_browser():
    br = mechanize.Browser()
    br.set_handle_robots(False)
    br.add_handler(ProbeHandler())
    return br


def load(page):
    br = new_browser()
    br.open(urljoin(BASE, "/pages/" + page))
    return br


def select_by_id(br, form_id):
    for form in br.forms():
        if form.attrs.get("id") == form_id:
            br.form = form
            return form
    raise AssertionError("form id=%r not found" % form_id)


def attempt(steps, label, fn):
    """Run fn, recording success or the exception as evidence."""
    try:
        result = fn()
    except Exception as exc:
        steps.append({
            "action": label,
            "ok": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
        })
        return None
    if not isinstance(result, (str, int, float, bool, list, dict, tuple,
                               type(None))):
        detail = repr(result)
    else:
        detail = result
    steps.append({"action": label, "ok": True, "detail": detail})
    return result


def dump_request(case_id, label, req):
    """Persist the client-side view of a generated Request."""
    case_dir = os.path.join(RESULTS, case_id)
    os.makedirs(case_dir, exist_ok=True)
    data = req.data
    if isinstance(data, str):
        data = data.encode("utf-8")
    body_file = None
    if data is not None:
        body_file = "%s_%s_body.bin" % (case_id, label)
        with open(os.path.join(case_dir, body_file), "wb") as fh:
            fh.write(data)
    headers = {}
    for key, val in req.header_items():
        headers.setdefault(key, val)
    info = {
        "method": req.get_method(),
        "url": req.get_full_url(),
        "headers": headers,
        "body_file": body_file,
        "body_preview": None if data is None else data[:400].decode(
            "utf-8", "replace"),
    }
    path = os.path.join(case_dir, "%s_%s_request.json" % (case_id, label))
    with open(path, "w") as fh:
        json.dump(info, fh, indent=2, ensure_ascii=False)
    return info


def send(br, req):
    """Send a generated request through the wire-recording server."""
    before = len(WIRE_LOG)
    br.open(req)
    return WIRE_LOG[before:]


def wire_pairs(record):
    """Ordered (name, value) pairs from a wire record (query or urlencoded)."""
    if record["query_pairs"]:
        return [tuple(p) for p in record["query_pairs"]]
    with open(os.path.join(RESULTS, record["body_file"]), "rb") as fh:
        body = fh.read()
    ctype = (record["content_type"] or "").split(";")[0].strip()
    if ctype == "application/x-www-form-urlencoded":
        return [tuple(p) for p in parse_qsl(
            body.decode("utf-8", "replace"), keep_blank_values=True)]
    return []


def wire_body(record):
    with open(os.path.join(RESULTS, record["body_file"]), "rb") as fh:
        return fh.read()


def assertion(name, expected, actual, passed):
    return {
        "name": name,
        "expected": expected,
        "actual": actual,
        "pass": bool(passed),
    }


# ---------------------------------------------------------------- cases

def case_01(base):
    """Duplicate field names: two text inputs + two checkboxes share names."""
    case_id, page = "01-duplicate-fields", "01_duplicate_fields.html"
    steps, assertions, submissions = [], [], []

    br = load(page)
    attempt(steps, "select form nr=0", lambda: br.select_form(nr=0))
    attempt(steps, "list controls",
            lambda: [(c.name, c.type) for c in br.form.controls])

    def set_first():
        br.form["tag"] = "first"
        return [br.form.find_control("tag", nr=i).value for i in (0, 1)]
    attempt(steps, "naive: br.form['tag'] = 'first' (ambiguous name)",
            set_first)
    attempt(steps, "adapted: set_value(..., nr=0/1) for each tag", lambda: (
        br.form.set_value("first", "tag", nr=0),
        br.form.set_value("second", "tag", nr=1))[1])

    req = attempt(steps, "build request: form.click(name='go')",
                  lambda: br.form.click(name="go"))
    opt_control_count = sum(1 for c in br.form.controls if c.name == "opt")
    if req is not None:
        dump_request(case_id, "submit", req)
        submissions.extend(send(br, req))

    pairs = wire_pairs(submissions[-1]) if submissions else []
    assertions.append(assertion(
        "same-name checkboxes exposed as separate controls (DOM-like)",
        2, opt_control_count, opt_control_count == 2))
    assertions.append(assertion(
        "both text inputs submitted in document order",
        [("tag", "first"), ("tag", "second")],
        [p for p in pairs if p[0] == "tag"],
        [p for p in pairs if p[0] == "tag"] == [("tag", "first"),
                                                ("tag", "second")]))
    assertions.append(assertion(
        "both checked checkboxes submitted",
        [("opt", "a"), ("opt", "b")],
        [p for p in pairs if p[0] == "opt"],
        [p for p in pairs if p[0] == "opt"] == [("opt", "a"), ("opt", "b")]))
    assertions.append(assertion(
        "full pair sequence matches document order",
        [("tag", "first"), ("tag", "second"), ("opt", "a"), ("opt", "b"),
         ("go", "1")],
        pairs,
        pairs == [("tag", "first"), ("tag", "second"), ("opt", "a"),
                  ("opt", "b"), ("go", "1")]))
    return case_id, page, steps, submissions, assertions


def case_02(base):
    """Disabled fieldset: descendants must not be submitted (HTML5)."""
    case_id, page = "02-disabled-fieldset", "02_disabled_fieldset.html"
    steps, assertions, submissions = [], [], []

    br = load(page)
    attempt(steps, "select form nr=0", lambda: br.select_form(nr=0))
    attempt(steps, "control disabled flags as parsed",
            lambda: {c.name: c.disabled for c in br.form.controls})
    req = attempt(steps, "naive submit: form.click(name='go')",
                  lambda: br.form.click(name="go"))
    if req is not None:
        dump_request(case_id, "naive", req)
        submissions.extend(send(br, req))

    spec_pairs = [("outside", "o"), ("in_legend", "L"), ("go", "1")]
    pairs = wire_pairs(submissions[-1]) if submissions else []
    assertions.append(assertion(
        "naive submit matches HTML5 (fieldset-disabled controls excluded)",
        spec_pairs, pairs, pairs == spec_pairs))
    assertions.append(assertion(
        "extra fields leaked by naive submit",
        [], sorted(p for p in pairs if p not in spec_pairs),
        all(p in spec_pairs for p in pairs)))

    br2 = load(page)
    attempt(steps, "adaptation: select form", lambda: br2.select_form(nr=0))

    def disable_inner():
        for name in ("inner", "inner_sel"):
            br2.form.find_control(name).disabled = True
        return {c.name: c.disabled for c in br2.form.controls}
    attempt(steps, "adaptation: mark fieldset-descendant controls disabled",
            disable_inner)
    req2 = attempt(steps, "adapted submit", lambda: br2.form.click(name="go"))
    if req2 is not None:
        dump_request(case_id, "adapted", req2)
        submissions.extend(send(br2, req2))
    pairs2 = wire_pairs(submissions[-1]) if len(submissions) > 1 else []
    assertions.append(assertion(
        "adapted submit matches HTML5 semantics", spec_pairs, pairs2,
        pairs2 == spec_pairs))
    return case_id, page, steps, submissions, assertions


def case_03(base):
    """External controls: form= attribute associates controls outside <form>."""
    case_id, page = "03-external-controls", "03_external_controls.html"
    steps, assertions, submissions = [], [], []

    br = load(page)
    attempt(steps, "number of forms discovered",
            lambda: len(list(br.forms())))
    attempt(steps, "select form id=f", lambda: select_by_id(br, "f"))
    attempt(steps, "controls associated with form f",
            lambda: [(c.name, c.type) for c in br.form.controls])

    req = attempt(steps, "submit via external button: form.click(name='go')",
                  lambda: br.form.click(name="go"))
    if req is not None:
        dump_request(case_id, "submit", req)
        submissions.extend(send(br, req))

    expected = [("internal", "i"), ("ext", "e"), ("go", "ext_submit")]
    pairs = wire_pairs(submissions[-1]) if submissions else []
    assertions.append(assertion(
        "external controls (form=f) submitted in document order",
        expected, pairs, pairs == expected))
    assertions.append(assertion(
        "orphan (form=no_such_form) and no-form controls excluded",
        "absent", [p for p in pairs if p[0] in ("orphan", "noform")],
        not any(p[0] in ("orphan", "noform") for p in pairs)))
    return case_id, page, steps, submissions, assertions


def case_04(base):
    """Submit-button overrides: formaction / formmethod / formenctype."""
    case_id, page = "04-submit-overrides", "04_submit_overrides.html"
    steps, assertions, submissions = [], [], []

    br = load(page)
    attempt(steps, "select form nr=0", lambda: br.select_form(nr=0))
    req = attempt(steps, "click btn (formaction=/submit/04-override "
                         "formmethod=post)",
                  lambda: br.form.click(name="btn"))
    info = dump_request(case_id, "btn_override", req) if req is not None else {}
    if req is not None:
        attempt(steps, "send generated request", lambda: send(br, req))
    expected_url = urljoin(BASE, "/submit/04-override")
    assertions.append(assertion(
        "formaction resolved against document base URL",
        expected_url, info.get("url"), info.get("url") == expected_url))
    assertions.append(assertion(
        "formmethod=post honoured", "POST", info.get("method"),
        info.get("method") == "POST"))

    br2 = load(page)
    attempt(steps, "reload + select form", lambda: br2.select_form(nr=0))
    attempt(steps, "click btn2 (formenctype=text/plain)",
            lambda: br2.form.click(name="btn2"))
    assertions.append(assertion(
        "text/plain formenctype supported",
        "request generated", "see step error above",
        any(s["action"].startswith("click btn2") and s["ok"] for s in steps)))

    br3 = load(page)
    attempt(steps, "adaptation: reload + select form",
            lambda: br3.select_form(nr=0))

    def fix_formaction():
        ctl = br3.form.find_control(name="btn")
        ctl.attrs["formaction"] = urljoin(BASE, ctl.attrs["formaction"])
        return ctl.attrs["formaction"]
    attempt(steps, "adaptation: resolve formaction against base URL",
            fix_formaction)
    req3 = attempt(steps, "adapted click btn", lambda: br3.form.click(name="btn"))
    if req3 is not None:
        dump_request(case_id, "adapted", req3)
        submissions.extend(send(br3, req3))
    if submissions:
        rec = submissions[-1]
        assertions.append(assertion(
            "adapted request: POST to absolute override URL",
            ("POST", "/submit/04-override"),
            (rec["method"], rec["path"]),
            (rec["method"], rec["path"]) == ("POST", "/submit/04-override")))
        assertions.append(assertion(
            "adapted request body carries all successful controls",
            [("q", "1"), ("btn", "rel")], wire_pairs(rec),
            wire_pairs(rec) == [("q", "1"), ("btn", "rel")]))
    return case_id, page, steps, submissions, assertions


def case_05(base):
    """File inputs: multipart upload, naive assignment, empty file control."""
    case_id, page = "05-file-input", "05_file_input.html"
    steps, assertions, submissions = [], [], []

    br = load(page)
    attempt(steps, "select form id=f1", lambda: select_by_id(br, "f1"))
    attempt(steps, "naive: br.form['doc'] = '/tmp/report.txt'",
            lambda: br.form.__setitem__("doc", "/tmp/report.txt"))
    attempt(steps, "correct: form.add_file(BytesIO, 'text/plain', "
                   "'report.txt', name='doc')",
            lambda: br.form.add_file(io.BytesIO(b"PDF-DATA"), "text/plain",
                                     "report.txt", name="doc"))
    req = attempt(steps, "submit f1", lambda: br.form.click(name="go"))
    if req is not None:
        dump_request(case_id, "upload", req)
        submissions.extend(send(br, req))

    if submissions:
        rec = submissions[-1]
        body = wire_body(rec)
        ctype = rec["content_type"] or ""
        assertions.append(assertion(
            "Content-Type is multipart/form-data with boundary",
            "multipart/form-data; boundary=...", ctype,
            ctype.startswith("multipart/form-data; boundary=")))
        for label, needle in [
                ("text field part present", b'name="note"'),
                ("text field value present", b"hello"),
                ("file part with filename", b'name="doc"; filename="report.txt"'),
                ("file part content type", b"Content-Type: text/plain"),
                ("file bytes present", b"PDF-DATA")]:
            assertions.append(assertion(
                label, needle.decode(), "in body", needle in body))

    br2 = load(page)
    attempt(steps, "select form id=f2 (empty file input)",
            lambda: select_by_id(br2, "f2"))
    req2 = attempt(steps, "submit f2 with no file chosen",
                   lambda: br2.form.click(name="go"))
    if req2 is not None:
        dump_request(case_id, "empty_file", req2)
        submissions.extend(send(br2, req2))
    if len(submissions) > 1:
        body2 = wire_body(submissions[-1])
        assertions.append(assertion(
            "empty file control still sends a part with empty filename",
            b'name="doc2"; filename=""'.decode(), "in body",
            b'name="doc2"; filename=""' in body2))
    return case_id, page, steps, submissions, assertions


def case_06(base):
    """Multi-select and single-select default-selection semantics."""
    case_id, page = "06-multi-select", "06_multi_select.html"
    steps, assertions, submissions = [], [], []

    br = load(page)
    attempt(steps, "select form nr=0", lambda: br.select_form(nr=0))
    attempt(steps, "initial value of multi select",
            lambda: br.form.get_value("opts"))
    attempt(steps, "initial value of single select (no selected attr)",
            lambda: br.form.get_value("single"))

    def naive_single_assign():
        br.form["opts"] = "c"
        return br.form.get_value("opts")
    attempt(steps, "naive: br.form['opts'] = 'c' on <select multiple>",
            naive_single_assign)

    def proper_list_assign():
        br.form["opts"] = ["a", "c"]
        return br.form.get_value("opts")
    attempt(steps, "proper: br.form['opts'] = ['a', 'c']", proper_list_assign)

    req = attempt(steps, "submit", lambda: br.form.click(name="go"))
    if req is not None:
        dump_request(case_id, "naive_default", req)
        submissions.extend(send(br, req))
    pairs = wire_pairs(submissions[-1]) if submissions else []
    assertions.append(assertion(
        "multi select submits exactly the chosen values",
        [("opts", "a"), ("opts", "c")],
        [p for p in pairs if p[0] == "opts"],
        [p for p in pairs if p[0] == "opts"] == [("opts", "a"),
                                                 ("opts", "c")]))
    assertions.append(assertion(
        "single select with no selected attr submits first option "
        "(browser behaviour)",
        [("single", "x")], [p for p in pairs if p[0] == "single"],
        [p for p in pairs if p[0] == "single"] == [("single", "x")]))

    br2 = load(page)
    attempt(steps, "adaptation: reload + select form",
            lambda: br2.select_form(nr=0))
    attempt(steps, "convention probe: br.form['single'] = 'y' (plain string)",
            lambda: br2.form.__setitem__("single", "y"))
    attempt(steps, "proper: br.form['single'] = ['y'] (sequence)",
            lambda: br2.form.__setitem__("single", ["y"]))
    req2 = attempt(steps, "adapted submit", lambda: br2.form.click(name="go"))
    if req2 is not None:
        dump_request(case_id, "adapted", req2)
        submissions.extend(send(br2, req2))
    pairs2 = wire_pairs(submissions[-1]) if len(submissions) > 1 else []
    assertions.append(assertion(
        "adapted submit includes single=y",
        [("single", "y")], [p for p in pairs2 if p[0] == "single"],
        [p for p in pairs2 if p[0] == "single"] == [("single", "y")]))
    return case_id, page, steps, submissions, assertions


# ---------------------------------------------------------------- runner

CASES = [case_01, case_02, case_03, case_04, case_05, case_06]


def main():
    reset_wire_dir()
    summary = {"mechanize_version": MECHANIZE_VERSION,
               "python": sys.version.split()[0], "cases": []}
    for case_fn in CASES:
        case_id, page, steps, submissions, assertions = case_fn(BASE)
        passed = sum(1 for a in assertions if a["pass"])
        record = {
            "id": case_id,
            "page": "pages/" + page,
            "steps": steps,
            "assertions": assertions,
            "assertions_passed": "%d/%d" % (passed, len(assertions)),
            "wire_seqs": [r["seq"] for r in submissions],
        }
        summary["cases"].append(record)
        with open(os.path.join(RESULTS, "%s.json" % case_id), "w") as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)
        print("%-24s assertions %s" % (case_id,
                                       record["assertions_passed"]))
    with open(os.path.join(WIRE, "index.json"), "w") as fh:
        json.dump(WIRE_LOG, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(RESULTS, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    print("evidence written to", RESULTS)


if __name__ == "__main__":
    main()
