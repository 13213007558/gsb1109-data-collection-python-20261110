#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for filling controls by their visible label.

Covers mechanize.HTMLForm.find_control_by_label / set_by_label /
get_by_label, including explicit and wrapping <label> association,
configurable auxiliary label sources, ambiguity and the hidden/disabled
boundaries, and the headline scenario where a site renames field names but
keeps the visible labels.
"""

import os
import unittest
from urllib.parse import parse_qs

import mechanize
import mechanize._form as _form
from mechanize import AmbiguityError, ControlNotFoundError
from mechanize._html import content_parser, find_declared_encoding

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "label_fill_data")


def parse(html, label_sources=(), base_url="http://example.com/"):
    if isinstance(html, str):
        raw = html.encode("utf-8")
    else:
        raw = html
    root = content_parser(raw, transport_encoding="utf-8")
    form_encoding = find_declared_encoding(raw) or "utf-8"
    forms, _ = _form.parse_forms(
        root, base_url, select_default=False,
        encoding=form_encoding, label_sources=label_sources)
    return forms[0]


def read_fixture(name):
    with open(os.path.join(DATA_DIR, name), "rb") as fh:
        return fh.read()


# The single, name-agnostic filling script reused against both page versions.
def fill_by_visible_labels(form):
    identities = {}
    identities["corp"] = form.set_by_label("企业名称", "示例科技有限公司")
    identities["contact"] = form.set_by_label("联系人", "张三")
    identities["memo"] = form.set_by_label("备注", "线上填报")
    return identities


class ExplicitAndWrappingLabelTests(unittest.TestCase):

    def setUp(self):
        self.form = parse("""\
<form action="/save" method="post">
  <label for="corp">企业名称</label>
  <input type="text" id="corp" name="corpName">
  <label>联系人<input type="text" name="contactPerson"></label>
</form>""")

    def test_explicit_for_id_association(self):
        control = self.form.find_control_by_label("企业名称")
        self.assertEqual(control.name, "corpName")
        self.assertEqual(control.id, "corp")
        self.assertEqual(control.type, "text")

    def test_wrapping_label_association(self):
        control = self.form.find_control_by_label("联系人")
        self.assertEqual(control.name, "contactPerson")

    def test_whitespace_is_compressed_but_case_matters(self):
        form = parse("""\
<form><label for="x">  企业   名称 </label>
<input id="x" name="n"></form>""")
        self.assertEqual(form.find_control_by_label("企业 名称").name, "n")
        self.assertRaises(
            ControlNotFoundError, form.find_control_by_label, "企业名")

    def test_set_by_label_fills_and_returns_identity(self):
        control = self.form.set_by_label("企业名称", "Acme")
        self.assertEqual(control.name, "corpName")
        self.assertEqual(self.form["corpName"], "Acme")
        found, value = self.form.get_by_label("联系人")
        self.assertEqual(found.name, "contactPerson")
        self.assertEqual(value, "")

    def test_returned_control_allows_further_narrowing(self):
        control = self.form.find_control_by_label("企业名称")
        # the returned identity can be fed back into the name-based API
        again = self.form.find_control(control.name, id=control.id)
        self.assertIs(again, control)


class AmbiguityTests(unittest.TestCase):

    def setUp(self):
        self.form = parse("""\
<form>
  <label for="a">备注</label><input id="a" name="n1">
  <label for="b">备注</label><input id="b" name="n2">
</form>""")

    def test_duplicate_label_is_ambiguous_not_first_match(self):
        with self.assertRaises(AmbiguityError) as ctx:
            self.form.find_control_by_label("备注")
        message = str(ctx.exception)
        # the error must expose every candidate's identity
        self.assertIn("n1", message)
        self.assertIn("n2", message)

    def test_nr_disambiguates_in_document_order(self):
        self.assertEqual(self.form.find_control_by_label("备注", nr=0).id, "a")
        self.assertEqual(self.form.find_control_by_label("备注", nr=1).id, "b")
        self.assertRaises(
            ControlNotFoundError,
            self.form.find_control_by_label, "备注", nr=2)

    def test_same_name_duplicate_label_is_still_ambiguous(self):
        form = parse("""\
<form>
  <label for="p1">电话</label><input id="p1" name="phone">
  <label for="p2">电话</label><input id="p2" name="phone">
</form>""")
        self.assertRaises(
            AmbiguityError, form.find_control_by_label, "电话")

    def test_type_filter_resolves_otherwise_ambiguous_label(self):
        form = parse("""\
<form>
  <label for="a">数量</label><input id="a" name="qty" type="text">
  <label for="b">数量</label>
  <select id="b" name="qty_choice"><option>1</option></select>
</form>""")
        self.assertEqual(
            form.find_control_by_label("数量", type="select").id, "b")


class HiddenDisabledBoundaryTests(unittest.TestCase):

    def setUp(self):
        self.form = parse("""\
<form>
  <label for="h">令牌</label>
  <input id="h" type="hidden" name="token" value="t">
  <label for="d">停用</label>
  <input id="d" disabled name="off">
  <label for="ok">正常</label>
  <input id="ok" name="live">
</form>""")

    def test_hidden_control_excluded_by_default(self):
        self.assertRaises(
            ControlNotFoundError, self.form.find_control_by_label, "令牌")
        hidden = self.form.find_control_by_label(
            "令牌", include_hidden=True)
        self.assertEqual(hidden.type, "hidden")

    def test_disabled_control_excluded_by_default(self):
        self.assertRaises(
            ControlNotFoundError, self.form.find_control_by_label, "停用")
        disabled = self.form.find_control_by_label(
            "停用", include_disabled=True)
        self.assertEqual(disabled.name, "off")

    def test_visible_control_still_found(self):
        self.assertEqual(
            self.form.find_control_by_label("正常").name, "live")

    def test_missing_label_raises_not_found(self):
        self.assertRaises(
            ControlNotFoundError,
            self.form.find_control_by_label, "不存在的标签")

    def test_empty_label_rejected(self):
        self.assertRaises(
            ValueError, self.form.find_control_by_label, "  ")


class AuxiliaryLabelSourceTests(unittest.TestCase):

    TABLE_HTML = """\
<form><table>
  <tr><td>企业名称</td><td><input name="field_a"></td></tr>
  <tr><th>联系人</th><td><input name="field_c"></td></tr>
</table>
<input name="field_b" aria-label="联系人" title="备用联系人">
</form>"""

    def test_auxiliary_sources_disabled_by_default(self):
        form = parse(self.TABLE_HTML)
        self.assertRaises(
            ControlNotFoundError, form.find_control_by_label, "企业名称")

    def test_table_cell_text_source(self):
        form = parse(self.TABLE_HTML, label_sources=("td",))
        self.assertEqual(
            form.find_control_by_label("企业名称").name, "field_a")

    def test_attribute_sources(self):
        form = parse(
            self.TABLE_HTML,
            label_sources=("td",
                           ("input", "aria-label"),
                           ("input", "title")))
        controls = [
            form.find_control_by_label("联系人", nr=0),
            form.find_control_by_label("联系人", nr=1),
        ]
        self.assertEqual(
            sorted(c.name for c in controls),
            ["field_b", "field_c"])
        self.assertEqual(
            form.find_control_by_label("备用联系人").name, "field_b")

    def test_callable_source(self):
        def data_caption(elem, parent_map):
            return elem.get("data-caption")

        form = parse(
            '<form><input name="z" data-caption="备注说明"></form>',
            label_sources=(data_caption,))
        self.assertEqual(
            form.find_control_by_label("备注说明").name, "z")

    def test_textarea_and_option_text_are_not_labels(self):
        form = parse("""\
<form><table>
  <tr><td>备注<textarea name="t">企业名称</textarea></td></tr>
  <tr><td>分类<select name="s">
    <option>选项A</option><option>选项B</option>
  </select></td></tr>
</table></form>""", label_sources=("td",))
        self.assertEqual(
            [l.text for l in form.find_control("t").get_labels()], ["备注"])
        select_labels = form.find_control("s").get_labels()
        self.assertTrue(
            all("选项A" not in l.text and "选项B" not in l.text
                for l in select_labels))
        self.assertEqual(form.find_control_by_label("分类").name, "s")

    def test_label_sources_recorded_on_form(self):
        form = parse(self.TABLE_HTML, label_sources=("td",))
        self.assertEqual(form.label_sources, (("td", None),))


class ListControlLabelTests(unittest.TestCase):

    def test_radio_and_checkbox_controls_have_no_control_labels(self):
        # the control itself carries no label; its items do
        form = parse("""\
<form>
  <label><input type="checkbox" name="genre" value="western">西部片</label>
  <label><input type="checkbox" name="genre" value="horror">恐怖片</label>
</form>""")
        self.assertRaises(
            ControlNotFoundError,
            form.find_control_by_label, "西部片")
        control = form.find_control("genre")
        item = control.get(label="西部片")
        self.assertEqual(item.name, "western")


class TwoPageVersionTests(unittest.TestCase):
    """The site renames every field, but the visible labels stay the same."""

    def _browser(self, html):
        browser = mechanize.Browser(label_sources=("td",))
        browser.set_html(html, "http://example.com/register")
        browser.select_form(nr=0)
        return browser

    def _request_fields(self, browser):
        url, data, headers = browser.click_request_data()
        self.assertEqual(url, "http://example.com/submit")
        return {key: values[0] for key, values in parse_qs(data).items()}

    def test_same_script_old_page_uses_old_field_names(self):
        browser = self._browser(read_fixture("form_v1.html").decode("utf-8"))
        identities = fill_by_visible_labels(browser.form)

        # the returned identities reveal what was actually selected
        self.assertEqual(identities["corp"].name, "corpName")
        self.assertEqual(identities["contact"].name, "contactPerson")
        self.assertEqual(identities["memo"].name, "memo")

        fields = self._request_fields(browser)
        self.assertEqual(fields["corpName"], "示例科技有限公司")
        self.assertEqual(fields["contactPerson"], "张三")
        self.assertEqual(fields["memo"], "线上填报")
        self.assertEqual(fields["csrf_token"], "token-v1")

    def test_same_script_new_page_uses_renamed_field_names(self):
        browser = self._browser(read_fixture("form_v2.html").decode("utf-8"))
        identities = fill_by_visible_labels(browser.form)

        # labels unchanged, but the selected controls are the renamed ones
        self.assertEqual(identities["corp"].name, "enterprise_name")
        self.assertEqual(identities["contact"].name, "linkman")
        self.assertEqual(identities["memo"].name, "remark_text")

        fields = self._request_fields(browser)
        self.assertEqual(fields["enterprise_name"], "示例科技有限公司")
        self.assertEqual(fields["linkman"], "张三")
        self.assertEqual(fields["remark_text"], "线上填报")
        self.assertEqual(fields["csrf_token"], "token-v2")
        # old field names must not leak into the new request
        self.assertNotIn("corpName", fields)
        self.assertNotIn("contactPerson", fields)

    def test_runtime_label_source_configuration(self):
        browser = mechanize.Browser()
        browser.set_html(read_fixture("form_v2.html").decode("utf-8"),
                         "http://example.com/register")
        # before configuring the table-cell source, the labels are invisible
        browser.select_form(nr=0)
        self.assertRaises(
            ControlNotFoundError,
            browser.form.find_control_by_label, "企业名称")
        browser.set_label_sources(("td",))
        # re-select to force the form to be re-parsed with the new sources
        browser.select_form(nr=0)
        control = browser.form.find_control_by_label("企业名称")
        self.assertEqual(control.name, "enterprise_name")


if __name__ == "__main__":
    unittest.main()
