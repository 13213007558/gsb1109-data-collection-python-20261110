from __future__ import annotations

from itertools import permutations

import pytest
from lxml import etree

from parsel import EditBatch, EditConflictError, InvalidEditError, Selector, apply_edits
from parsel.edits import DropElements, RemoveAttribute, ReplaceElements, SetAttribute


def make_selector(markup: str, type_: str = "html") -> Selector:
    if type_ == "html":
        return Selector(text=markup)
    return Selector(text=markup, type="xml")


class TestDrop:
    def test_drop_html_keeps_tail(self) -> None:
        sel = make_selector("<html><body>before<span>inside</span> after</body></html>")
        batch = sel.edit_batch().drop(sel.css("span"))
        assert sel.apply_edits(batch).get() == "<html><body>before after</body></html>"
        # The original tree is untouched in copy mode.
        assert sel.css("span")

    @pytest.mark.parametrize(
        ("xml", "expected"),
        [
            ("<a><b/>tail<c/></a>", "<a>tail<c/></a>"),
            ("<a>text<b/>tail<c/></a>", "<a>texttail<c/></a>"),
            ("<a><c/>previous<b/>tail</a>", "<a><c/>previoustail</a>"),
            ("<a><!--comment--><b/>tail</a>", "<a><!--comment-->tail</a>"),
            ("<a><b/>tail<b/>tail2</a>", "<a>tailtail2</a>"),
        ],
    )
    def test_drop_xml_keeps_tail(self, xml: str, expected: str) -> None:
        sel = make_selector(xml, "xml")
        batch = sel.edit_batch().drop(sel.xpath("//b"))
        assert sel.apply_edits(batch).get() == expected

    def test_drop_deduplicates_repeated_hits(self) -> None:
        sel = make_selector("<html><body><b>x</b><b>y</b></body></html>")
        # The same element reaches one edit through two different selectors.
        repeated = list(sel.xpath("//b[1]")) + list(sel.css("b:first-child"))
        edit = DropElements.from_selectors(repeated)
        batch = sel.edit_batch().add(edit)
        assert sel.apply_edits(batch).get() == "<html><body><b>y</b></body></html>"

    def test_drop_in_place(self) -> None:
        sel = make_selector("<html><body><b>x</b>tail</body></html>")
        batch = sel.edit_batch().drop(sel.css("b"))
        result = sel.apply_edits(batch, in_place=True)
        assert result is sel
        assert sel.get() == "<html><body>tail</body></html>"


class TestNestedTargets:
    """One edit may match nested nodes, e.g. a broad ``//div`` selector."""

    def test_nested_drop_in_one_edit(self) -> None:
        sel = make_selector("<a>t0<div>t1<p>t2<b/>tb</p>tp</div>td<c/>tc</a>", "xml")
        batch = sel.edit_batch().drop(sel.xpath("//div | //p | //b"))
        assert sel.apply_edits(batch).get() == "<a>t0td<c/>tc</a>"

    def test_nested_replace_in_one_edit(self) -> None:
        sel = make_selector("<a><div><p><b>x</b></p></div>tail</a>", "xml")
        batch = sel.edit_batch().replace(sel.xpath("//div | //b"), "<n/>")
        assert sel.apply_edits(batch).get() == "<a><n/>tail</a>"

    def test_nested_targets_converge_regardless_of_inner_order(self) -> None:
        markup = "<a>t0<div>t1<p>t2<b/>tb</p>tp</div>td<c/>tc</a>"
        reference = Selector(text=markup, type="xml")
        elements = [selector.root for selector in reference.xpath("//div | //p | //b")]
        # Copy mode keeps the reference tree intact for both applications.
        forward = etree.tostring(
            apply_edits(reference.root, [DropElements(elements)]),
            encoding="unicode",
        )
        reversed_ = etree.tostring(
            apply_edits(reference.root, [DropElements(list(reversed(elements)))]),
            encoding="unicode",
        )
        assert forward == reversed_ == "<a>t0td<c/>tc</a>"

    def test_nesting_across_two_edits_still_conflicts(self) -> None:
        sel = make_selector("<a><div><p><b/></p></div></a>", "xml")
        batch = sel.edit_batch()
        batch.add(DropElements.from_selectors(sel.xpath("//div")))
        batch.add(DropElements.from_selectors(sel.xpath("//b")))
        with pytest.raises(EditConflictError) as exc_info:
            sel.apply_edits(batch)
        assert tuple(type(edit) for edit in exc_info.value.edits) == (
            DropElements,
            DropElements,
        )


class TestReplace:
    def test_replace_html(self) -> None:
        sel = make_selector("<html><body><a href='old'>x</a>tail</body></html>")
        batch = sel.edit_batch().replace(
            sel.css("a"), '<a href="https://new.example/">x</a>'
        )
        result = sel.apply_edits(batch)
        assert result.css("a::attr(href)").get() == "https://new.example/"
        assert result.get() == (
            '<html><body><a href="https://new.example/">x</a>tail</body></html>'
        )

    def test_replace_with_multiple_nodes_keeps_tail(self) -> None:
        sel = make_selector("<html><body><div><a>x</a>tail</div></body></html>")
        batch = sel.edit_batch().replace(sel.css("a"), "lead<b>y</b><i>z</i>")
        assert sel.apply_edits(batch).get() == (
            "<html><body><div>lead<b>y</b><i>z</i>tail</div></body></html>"
        )

    def test_replace_with_text_only(self) -> None:
        sel = make_selector("<html><body><div><a>x</a>tail</div></body></html>")
        batch = sel.edit_batch().replace(sel.css("a"), "just text")
        assert sel.apply_edits(batch).get() == (
            "<html><body><div>just texttail</div></body></html>"
        )

    def test_replace_with_empty_markup(self) -> None:
        sel = make_selector("<html><body><div><a>x</a>tail</div></body></html>")
        batch = sel.edit_batch().replace(sel.css("a"), "")
        assert (
            sel.apply_edits(batch).get() == "<html><body><div>tail</div></body></html>"
        )

    @pytest.mark.parametrize(
        ("markup", "expected"),
        [
            ("   ", "<html><body><div>   tail</div></body></html>"),
            ("  lead  ", "<html><body><div>  lead  tail</div></body></html>"),
            ("  <b>x</b>  ", "<html><body><div>  <b>x</b>  tail</div></body></html>"),
        ],
    )
    def test_replace_keeps_leading_whitespace(self, markup: str, expected: str) -> None:
        sel = make_selector("<html><body><div><a>x</a>tail</div></body></html>")
        batch = sel.edit_batch().replace(sel.css("a"), markup)
        assert sel.apply_edits(batch).get() == expected

    def test_replace_xml(self) -> None:
        sel = make_selector("<a>t1<b/>t2<c/>t3</a>", "xml")
        batch = sel.edit_batch().replace(sel.xpath("//b"), "<x/>mid<y/>")
        assert sel.apply_edits(batch).get() == "<a>t1<x/>mid<y/>t2<c/>t3</a>"

    def test_replace_several_elements(self) -> None:
        sel = make_selector(
            "<html><body><a class='old'>1</a><a class='old'>2</a>end</body></html>"
        )
        batch = sel.edit_batch().replace(sel.css("a.old"), "<b>r</b>")
        assert sel.apply_edits(batch).get() == (
            "<html><body><b>r</b><b>r</b>end</body></html>"
        )

    def test_invalid_xml_markup_fails_before_touching_tree(self) -> None:
        sel = make_selector("<a><b/><c/></a>", "xml")
        before = sel.get()
        batch = sel.edit_batch().replace(sel.xpath("//b"), "<unclosed>")
        with pytest.raises(InvalidEditError):
            sel.apply_edits(batch)
        assert sel.get() == before


class TestAttributes:
    def test_set_and_remove_attributes(self) -> None:
        sel = make_selector('<html><body><a href="x" id="y">z</a></body></html>')
        batch = sel.edit_batch()
        batch.set_attribute(sel.css("a"), "href", "n")
        batch.remove_attribute(sel.css("a"), "id")
        assert sel.apply_edits(batch).get() == (
            '<html><body><a href="n">z</a></body></html>'
        )

    def test_remove_missing_attribute_is_noop(self) -> None:
        sel = make_selector("<html><body><a>z</a></body></html>")
        batch = sel.edit_batch().remove_attribute(sel.css("a"), "id")
        assert sel.apply_edits(batch).get() == "<html><body><a>z</a></body></html>"

    def test_namespaced_attributes_use_clark_names(self) -> None:
        xml = '<a xmlns:ns="http://ns.example"><b ns:h="1" h="2"/></a>'
        sel = make_selector(xml, "xml")
        batch = sel.edit_batch()
        batch.set_attribute(sel.xpath("//b"), "{http://ns.example}fresh", "v")
        batch.remove_attribute(sel.xpath("//b"), "{http://ns.example}h")
        result = sel.apply_edits(batch)
        assert result.get() == (
            '<a xmlns:ns="http://ns.example"><b h="2" ns:fresh="v"/></a>'
        )

    def test_different_attributes_on_same_element_coexist(self) -> None:
        sel = make_selector('<html><body><a href="x">z</a></body></html>')
        batch = sel.edit_batch()
        batch.set_attribute(sel.css("a"), "a1", "1")
        batch.set_attribute(sel.css("a"), "a2", "2")
        batch.remove_attribute(sel.css("a"), "href")
        assert sel.apply_edits(batch).get() == (
            '<html><body><a a1="1" a2="2">z</a></body></html>'
        )


class TestModes:
    def test_copy_mode_leaves_original_untouched(self) -> None:
        sel = make_selector("<html><body><b>x</b>tail</body></html>")
        original = sel.get()
        batch = sel.edit_batch().drop(sel.css("b"))
        result = sel.apply_edits(batch)
        assert result is not sel
        assert result.root is not sel.root
        assert sel.get() == original
        assert result.css("b") == []

    def test_in_place_mode_returns_same_tree(self) -> None:
        sel = make_selector("<html><body><b>x</b></body></html>")
        batch = sel.edit_batch().drop(sel.css("b"))
        root = apply_edits(sel.root, batch, in_place=True)
        assert root is sel.root

    def test_apply_edits_accepts_edit_batch_and_iterable(self) -> None:
        sel = make_selector("<html><body><b>x</b></body></html>")
        edit = DropElements.from_selectors(sel.css("b"))
        new_root = apply_edits(sel.root, EditBatch([edit]))
        assert etree.tostring(new_root, encoding="unicode", method="html") == (
            "<html><body></body></html>"
        )
        sel = make_selector("<html><body><b>x</b></body></html>")
        edit = DropElements.from_selectors(sel.css("b"))
        new_root = apply_edits(sel.root, [edit])
        assert etree.tostring(new_root, encoding="unicode", method="html") == (
            "<html><body></body></html>"
        )

    def test_empty_batch_copy_still_deep_copies(self) -> None:
        sel = make_selector("<html><body>x</body></html>")
        new_root = apply_edits(sel.root, [])
        assert new_root is not sel.root
        assert etree.tostring(new_root, encoding="unicode") == sel.get()


class TestConflicts:
    def _assert_conflict(
        self, sel: Selector, batch: EditBatch, types: tuple[type, type]
    ) -> None:
        before = sel.get()
        with pytest.raises(EditConflictError) as exc_info:
            sel.apply_edits(batch)
        assert tuple(type(edit) for edit in exc_info.value.edits) == types
        # No half-edited tree, in either mode.
        assert sel.get() == before
        with pytest.raises(EditConflictError):
            sel.apply_edits(batch, in_place=True)
        assert sel.get() == before

    def test_ancestor_drop_vs_descendant_replace(self) -> None:
        sel = make_selector("<html><body><p><b>x</b></p></body></html>")
        batch = sel.edit_batch()
        batch.drop(sel.css("p"))
        batch.replace(sel.css("b"), "<i/>")
        self._assert_conflict(sel, batch, (DropElements, ReplaceElements))

    def test_descendant_replace_vs_ancestor_drop_reversed(self) -> None:
        sel = make_selector("<html><body><p><b>x</b></p></body></html>")
        batch = sel.edit_batch()
        batch.replace(sel.css("b"), "<i/>")
        batch.drop(sel.css("p"))
        self._assert_conflict(sel, batch, (ReplaceElements, DropElements))

    def test_two_structural_edits_on_same_node(self) -> None:
        sel = make_selector("<html><body><p><b>x</b></p></body></html>")
        batch = sel.edit_batch()
        batch.drop(sel.css("b"))
        batch.replace(sel.css("b"), "<i/>")
        self._assert_conflict(sel, batch, (DropElements, ReplaceElements))

    def test_sibling_structural_edits_do_not_conflict(self) -> None:
        sel = make_selector("<html><body><p><b>1</b><i>2</i></p></body></html>")
        batch = sel.edit_batch()
        batch.drop(sel.css("b"))
        batch.replace(sel.css("i"), "<u>2</u>")
        assert sel.apply_edits(batch).get() == (
            "<html><body><p><u>2</u></p></body></html>"
        )

    def test_two_sets_of_same_attribute(self) -> None:
        sel = make_selector("<html><body><a>x</a></body></html>")
        batch = sel.edit_batch()
        batch.set_attribute(sel.css("a"), "href", "1")
        batch.set_attribute(sel.css("a"), "href", "2")
        self._assert_conflict(sel, batch, (SetAttribute, SetAttribute))

    def test_set_and_remove_same_attribute(self) -> None:
        sel = make_selector('<html><body><a href="x">z</a></body></html>')
        batch = sel.edit_batch()
        batch.set_attribute(sel.css("a"), "href", "n")
        batch.remove_attribute(sel.css("a"), "href")
        self._assert_conflict(sel, batch, (SetAttribute, RemoveAttribute))

    def test_remove_then_set_same_attribute_reversed(self) -> None:
        sel = make_selector('<html><body><a href="x">z</a></body></html>')
        batch = sel.edit_batch()
        batch.remove_attribute(sel.css("a"), "href")
        batch.set_attribute(sel.css("a"), "href", "n")
        self._assert_conflict(sel, batch, (RemoveAttribute, SetAttribute))

    def test_two_removes_of_same_attribute(self) -> None:
        sel = make_selector('<html><body><a href="x">z</a></body></html>')
        batch = sel.edit_batch()
        batch.remove_attribute(sel.css("a"), "href")
        batch.remove_attribute(sel.css("a"), "href")
        self._assert_conflict(sel, batch, (RemoveAttribute, RemoveAttribute))

    def test_attribute_edit_under_dropped_ancestor(self) -> None:
        sel = make_selector('<html><body><p><a href="x">z</a></p></body></html>')
        batch = sel.edit_batch()
        batch.drop(sel.css("p"))
        batch.set_attribute(sel.css("a"), "href", "n")
        self._assert_conflict(sel, batch, (DropElements, SetAttribute))

    def test_attribute_edit_under_dropped_ancestor_reversed(self) -> None:
        sel = make_selector('<html><body><p><a href="x">z</a></p></body></html>')
        batch = sel.edit_batch()
        batch.set_attribute(sel.css("a"), "href", "n")
        batch.drop(sel.css("p"))
        self._assert_conflict(sel, batch, (SetAttribute, DropElements))

    def test_attribute_edit_on_replaced_node(self) -> None:
        sel = make_selector('<html><body><a href="x">z</a></body></html>')
        batch = sel.edit_batch()
        batch.replace(sel.css("a"), "<b>r</b>")
        batch.remove_attribute(sel.css("a"), "href")
        self._assert_conflict(sel, batch, (ReplaceElements, RemoveAttribute))

    def test_attribute_on_sibling_of_structural_edit_is_fine(self) -> None:
        sel = make_selector(
            '<html><body><p><a href="x">z</a><i>w</i></p></body></html>'
        )
        batch = sel.edit_batch()
        batch.drop(sel.css("a"))
        batch.set_attribute(sel.css("i"), "id", "kept")
        assert sel.apply_edits(batch).get() == (
            '<html><body><p><i id="kept">w</i></p></body></html>'
        )


class TestOrderIndependence:
    @pytest.mark.parametrize("type_", ["html", "xml"])
    def test_same_output_for_every_commit_order(self, type_: str) -> None:
        if type_ == "html":
            sel = make_selector(
                "<html><body>"
                "<p>before<span class='ad'>ad</span>tail</p>"
                "<a class='old' href='http://old/'>l</a> after"
                "<div class='ad'>d</div> end"
                "</body></html>"
            )
            batch = sel.edit_batch()
            batch.drop(sel.css(".ad"))
            batch.replace(sel.css("a.old"), '<a href="https://new.example/">l</a>')
            batch.set_attribute(sel.css("p"), "data-clean", "1")
            batch.remove_attribute(sel.css("body"), "bg")
        else:
            sel = make_selector(
                '<a xmlns:ns="http://ns.example">'
                "<p>before<b class='ad'/>tail</p>"
                '<c ns:h="1"/><d>x</d> end'
                "</a>",
                "xml",
            )
            batch = sel.edit_batch()
            batch.drop(sel.xpath("//b"))
            batch.replace(sel.xpath("//d"), "<n/>")
            batch.set_attribute(sel.xpath("//c"), "{http://ns.example}k", "v")
            batch.remove_attribute(sel.xpath("//c"), "{http://ns.example}h")
        edits = list(batch)
        outputs = {sel.apply_edits(list(order)).get() for order in permutations(edits)}
        assert len(outputs) == 1


class TestInvalidEdits:
    def test_pseudo_element_selector_rejected(self) -> None:
        sel = make_selector("<html><body><b>x</b></body></html>")
        with pytest.raises(ValueError, match="does not wrap an element"):
            sel.edit_batch().drop(sel.css("b::text"))

    def test_root_drop_rejected_and_tree_kept(self) -> None:
        sel = make_selector("<html><body><b>x</b></body></html>")
        before = sel.get()
        with pytest.raises(InvalidEditError):
            sel.apply_edits(sel.edit_batch().drop(sel.xpath("/html")))
        assert sel.get() == before

    def test_root_replace_rejected(self) -> None:
        sel = make_selector("<a><b/></a>", "xml")
        with pytest.raises(InvalidEditError):
            sel.apply_edits(sel.edit_batch().replace(sel.xpath("/a"), "<r/>"))

    def test_attribute_edit_on_root_is_allowed(self) -> None:
        sel = make_selector('<a h="1"><b/></a>', "xml")
        batch = sel.edit_batch().remove_attribute(sel.xpath("/a"), "h")
        assert sel.apply_edits(batch).get() == "<a><b/></a>"

    def test_element_from_another_tree_rejected(self) -> None:
        sel = make_selector("<html><body><b>x</b></body></html>")
        foreign = make_selector("<html><body><p>foreign</p></body></html>")
        batch = EditBatch().drop(foreign.css("p"))
        with pytest.raises(InvalidEditError):
            apply_edits(sel.root, batch)

    def test_json_and_text_selectors_rejected(self) -> None:
        sel = Selector(text='{"a": 1}')
        with pytest.raises(ValueError, match="Cannot collect tree edits"):
            sel.edit_batch()

    def test_invalid_attribute_name_fails_before_touching_tree(self) -> None:
        sel = make_selector("<a><b/></a>", "xml")
        before = sel.get()
        batch = sel.edit_batch().set_attribute(sel.xpath("//b"), "bad name", "v")
        with pytest.raises(InvalidEditError):
            sel.apply_edits(batch)
        assert sel.get() == before


class TestBatchApi:
    def test_chaining_and_iteration(self) -> None:
        sel = make_selector(
            "<html><body><b class='ad'>x</b><a href='o'>l</a></body></html>"
        )
        batch = (
            sel.edit_batch()
            .drop(sel.css(".ad"))
            .replace(sel.css("a"), "<a href='n'>l</a>")
            .set_attribute(sel.css("body"), "ok", "1")
            .remove_attribute(sel.css("body"), "bg")
        )
        assert len(batch) == 4
        assert [type(edit) for edit in batch] == [
            DropElements,
            ReplaceElements,
            SetAttribute,
            RemoveAttribute,
        ]
        assert sel.apply_edits(batch).get() == (
            '<html><body ok="1"><a href="n">l</a></body></html>'
        )

    def test_add_prebuilt_edits(self) -> None:
        sel = make_selector("<html><body><b>x</b><i>y</i></body></html>")
        batch = EditBatch()
        batch.add(DropElements.from_selectors(sel.css("b")))
        batch.add(ReplaceElements.from_selectors(sel.css("i"), "<u>z</u>"))
        assert sel.apply_edits(batch).get() == ("<html><body><u>z</u></body></html>")

    def test_selector_list_input(self) -> None:
        sel = make_selector("<html><body><ul><li>1</li><li>2</li></ul></body></html>")
        batch = sel.edit_batch().drop(sel.css("li"))
        assert sel.apply_edits(batch).get() == ("<html><body><ul></ul></body></html>")


class TestIntegratedScenarios:
    def test_remove_ads_rewrite_links_keep_tail_in_one_pass(self) -> None:
        doc = """
        <html><body>
        <p>Intro <span class="ad">buy now</span> kept tail</p>
        <a class="old" href="http://old.example/x">link</a> after link
        <div class="ad"><b>ad</b></div> trailing text
        </body></html>
        """
        sel = make_selector(doc)
        batch = sel.edit_batch()
        batch.drop(sel.css(".ad"))
        batch.replace(
            sel.css("a.old"),
            '<a class="new" href="https://new.example/x">link</a>',
        )
        batch.set_attribute(sel.css("p"), "data-cleaned", "1")
        result = sel.apply_edits(batch)
        assert "ad" not in result.get()
        assert "http://old.example" not in result.get()
        assert "kept tail" in result.get()
        assert "trailing text" in result.get()
        assert "after link" in result.get()
        assert result.css("a::attr(href)").get() == "https://new.example/x"
        assert result.css("p::attr(data-cleaned)").get() == "1"

    def test_namespace_declarations_survive_copy(self) -> None:
        xml = '<a xmlns:ns="http://ns.example"><b ns:h="1"/></a>'
        sel = make_selector(xml, "xml")
        batch = sel.edit_batch().drop(sel.xpath("//b"))
        result = sel.apply_edits(batch)
        assert "xmlns:ns" in result.get()
        assert sel.get() == xml
