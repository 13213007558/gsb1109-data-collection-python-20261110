from __future__ import annotations

import gc
import weakref
from typing import Any

import pytest

import parsel.selector as selector_module
from parsel import Selector, SelectorDocument, SelectorList
from parsel.selector import (
    CannotDropElementWithoutParent,
    CannotRemoveElementWithoutRoot,
)

HTML = """
<html><body><ul class="items">
  <li class="product" data-id="a"><a href="/a">A</a></li>
  <li class="product" data-id="b"><a href="/b">B</a></li>
  <li class="product" data-id="c"><a href="/c">C</a></li>
</ul></body></html>
"""

XML = """<?xml version="1.0"?>
<feed xmlns="http://example.org/ns">
  <entry id="1"><title>One</title></entry>
  <entry id="2"><title>Two</title></entry>
</feed>
"""


class TestSharedParsing:
    def test_document_parses_only_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = 0
        original = selector_module.create_root_node

        def counted(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(selector_module, "create_root_node", counted)
        document = SelectorDocument(text=HTML)
        views = [document.selector() for _ in range(25)]
        assert calls == 1
        assert all(view.document is document for view in views)

    def test_standalone_selector_keeps_original_interface(self) -> None:
        selector = Selector(text=HTML)
        assert selector.css("li.product::attr(data-id)").getall() == ["a", "b", "c"]
        assert isinstance(selector.document, SelectorDocument)
        # Nested selectors stay in the same parsed document.
        assert selector.css("li")[0].document is selector.document

    def test_views_share_root_and_match_standalone_results(self) -> None:
        document = SelectorDocument(text=HTML)
        standalone = Selector(text=HTML)
        for view in [document.selector() for _ in range(5)]:
            assert view.root is document.root
            assert view.css("li a::attr(href)").getall() == ["/a", "/b", "/c"]
            assert view.xpath("//li/@data-id").getall() == ["a", "b", "c"]
            assert view.get() == standalone.get()

    def test_body_and_explicit_type_arguments(self) -> None:
        document = SelectorDocument(body=HTML.encode(), type="html")
        view = document.selector()
        assert view.type == "html"
        assert view.css("li.product").getall() == Selector(
            body=HTML.encode()
        ).css("li.product").getall()

    def test_root_argument_wraps_without_parsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed = Selector(text=HTML).root

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("wrapping an existing root must not parse")

        monkeypatch.setattr(selector_module, "create_root_node", fail)
        document = SelectorDocument(root=parsed, type="html")
        assert document.root is parsed
        assert document.selector().css("li").getall() == Selector(root=parsed).css(
            "li"
        ).getall()

    def test_invalid_arguments(self) -> None:
        with pytest.raises(ValueError, match="needs text, body, or root"):
            SelectorDocument()
        with pytest.raises(ValueError, match="Invalid type"):
            SelectorDocument(text=HTML, type="yaml")
        with pytest.raises(TypeError, match="text argument"):
            SelectorDocument(text=b"<html/>")  # type: ignore[arg-type]

    def test_text_and_root_warns(self) -> None:
        with pytest.warns(UserWarning, match="root is being ignored"):
            SelectorDocument(text=HTML, root=Selector(text="<i/>").root)


class TestPerQueryState:
    def test_namespaces_are_private_per_view(self) -> None:
        document = SelectorDocument(text=XML, type="xml")
        with_ns = document.selector()
        without_ns = document.selector()
        with_ns.register_namespace("e", "http://example.org/ns")

        assert len(with_ns.xpath("//e:entry")) == 2
        # The prefix is unknown to the other view, so it gets an XPath error.
        with pytest.raises(ValueError, match="XPath error"):
            without_ns.xpath("//e:entry")
        # Ad-hoc namespaces on one call do not leak either.
        assert (
            len(
                without_ns.xpath(
                    "//f:entry", namespaces={"f": "http://example.org/ns"}
                )
            )
            == 2
        )
        with pytest.raises(ValueError, match="XPath error"):
            without_ns.xpath("//f:entry")

    def test_xpath_variables_are_private_per_call(self) -> None:
        document = SelectorDocument(text=HTML)
        first = document.selector()
        second = document.selector()
        assert first.xpath("//li[@data-id=$id]", id="a").getall() == [
            '<li class="product" data-id="a"><a href="/a">A</a></li>'
        ]
        assert second.xpath("//li[@data-id=$id]", id="c").getall() != first.xpath(
            "//li[@data-id=$id]", id="a"
        ).getall()

    def test_nested_selectors_keep_independent_namespaces(self) -> None:
        document = SelectorDocument(text=HTML)
        view = document.selector(namespaces={"p": "http://example.org/ns"})
        child = view.css("li")[0]
        assert child.namespaces.get("p") == "http://example.org/ns"
        child.register_namespace("q", "http://example.org/other")
        assert "q" not in view.namespaces
        assert child.document is document


class TestCopyOnWrite:
    def test_drop_isolated_between_views(self) -> None:
        document = SelectorDocument(text=HTML)
        writer = document.selector()
        reader = document.selector()

        writer.css("li.product")[0].drop()

        assert len(reader.css("li.product")) == 3
        assert len(writer.css("li.product")) == 2
        assert reader.document is document
        assert writer.document is not document
        assert writer.document._branch is True

    def test_drop_visible_within_one_query_lineage(self) -> None:
        document = SelectorDocument(text=HTML)
        view = document.selector()
        items = view.css("li.product")
        items.drop()
        assert view.css("li.product") == []
        # A sibling view keeps the original data.
        assert len(document.selector().css("li.product")) == 3

    def test_bulk_drop_remaps_every_live_selector(self) -> None:
        view = SelectorDocument(text=HTML).selector()
        items = view.css("li.product")
        links = view.css("li a")
        items.drop()
        # Selectors created before the copy now point at copied nodes and
        # still serialize correctly.
        assert [link.get() for link in links] == [
            '<a href="/a">A</a>',
            '<a href="/b">B</a>',
            '<a href="/c">C</a>',
        ]
        assert view.css("li") == []

    def test_failed_drop_does_not_branch(self) -> None:
        document = SelectorDocument(text=HTML)
        view = document.selector()
        with pytest.raises(CannotRemoveElementWithoutRoot):
            view.css("a::text")[0].drop()
        assert view.document is document
        root_view = document.selector()
        with pytest.raises(CannotDropElementWithoutParent):
            root_view.xpath("/html").drop()
        assert root_view.document is document
        assert len(document.selector().css("li")) == 3

    def test_drop_keeps_xml_tail_on_branch(self) -> None:
        document = SelectorDocument(text="<a><b/>tail<c/></a>", type="xml")
        writer = document.selector()
        reader = document.selector()
        writer.xpath("//b").drop()
        assert writer.get() == "<a>tail<c/></a>"
        assert reader.get() == "<a><b/>tail<c/></a>"

    def test_remove_namespaces_isolated(self) -> None:
        document = SelectorDocument(text=XML, type="xml")
        writer = document.selector()
        reader = document.selector()
        writer.remove_namespaces()
        assert len(writer.xpath("//entry")) == 2
        assert reader.xpath("//entry") == []
        assert len(reader.xpath("//*[local-name()='entry']")) == 2

    def test_nested_root_document_copies_equivalent_subtree(self) -> None:
        standalone = Selector(text=HTML)
        list_root = standalone.css("ul")[0].root
        document = SelectorDocument(root=list_root, type="html")
        view = document.selector()
        item = view.css("li")[0]
        item.drop()
        assert len(view.css("li")) == 2
        assert len(document.selector().css("li")) == 3

    def test_branch_does_not_parse_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = SelectorDocument(text=HTML)

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("copy-on-write must not reparse the source text")

        monkeypatch.setattr(selector_module, "create_root_node", fail)
        view = document.selector()
        view.css("li")[0].drop()
        assert len(view.css("li")) == 2

    def test_children_created_after_mutation_stay_on_branch(self) -> None:
        document = SelectorDocument(text=HTML)
        view = document.selector()
        view.css("li")[0].drop()
        remaining = view.css("li")
        assert len(remaining) == 2
        # Children created after the copy point at copied nodes and keep
        # working, including a second mutation.
        remaining[0].drop()
        assert len(view.css("li")) == 1
        assert len(document.selector().css("li")) == 3

    def test_two_views_get_independent_branches(self) -> None:
        document = SelectorDocument(text=HTML)
        first = document.selector()
        second = document.selector()
        first.css("li")[0].drop()
        second.css("li")[1].drop()
        assert first.document is not second.document
        assert [v.get() for v in first.css("li::attr(data-id)")] == ["b", "c"]
        assert [v.get() for v in second.css("li::attr(data-id)")] == ["a", "c"]
        assert len(document.selector().css("li")) == 3

    def test_text_and_attribute_selectors_survive_migration(self) -> None:
        view = SelectorDocument(text=HTML).selector()
        texts = view.css("li a::text")
        hrefs = view.css("li a::attr(href)")
        view.css("li")[0].drop()
        # Non-element results are not remapped and keep serializing fine.
        assert texts.getall() == ["A", "B", "C"]
        assert hrefs.getall() == ["/a", "/b", "/c"]
        assert view.css("li a::text").getall() == ["B", "C"]


class TestRelease:
    def test_document_released_with_last_view(self) -> None:
        gc.collect()
        document = SelectorDocument(text=HTML)
        reference = weakref.ref(document)
        views = [document.selector() for _ in range(8)]
        nested = [item for view in views for item in view.css("li")]
        del document
        gc.collect()
        assert reference() is not None
        del views, nested
        gc.collect()
        assert reference() is None

    def test_branch_released_independently(self) -> None:
        gc.collect()
        document = SelectorDocument(text=HTML)
        original_ref = weakref.ref(document)
        reader = document.selector()
        writer = document.selector()
        writer.css("li")[0].drop()
        assert writer.document is not document
        branch_ref = weakref.ref(writer.document)
        del writer
        gc.collect()
        assert branch_ref() is None
        assert original_ref() is document
        assert len(reader.css("li")) == 3
        del document, reader
        gc.collect()
        assert original_ref() is None

    def test_weakrefable_slots(self) -> None:
        view = SelectorDocument(text=HTML).selector()
        weakref.ref(view)
        assert not hasattr(view, "__dict__")
        assert not hasattr(view.document, "__dict__")


class TestBackwardCompatibility:
    def test_text_attribute_delegates_to_document(self) -> None:
        selector = Selector(text=HTML)
        assert selector._text == HTML
        document = SelectorDocument(text=HTML)
        assert document.selector()._text == HTML
        assert Selector(root=selector.root)._text is None

    def test_standalone_selector_drop_behaves_as_before(self) -> None:
        selector = Selector(
            text="<html><body><ul><li>1</li><li>2</li></ul></body></html>"
        )
        selector.css("li").drop()
        assert selector.css("li") == []
        assert isinstance(selector.css("body"), SelectorList)

    def test_selector_and_document_use_slots(self) -> None:
        selector = Selector(text=HTML)
        assert not hasattr(selector, "__dict__")
        assert not hasattr(selector.document, "__dict__")

    def test_smart_strings_text_nodes_migrate(self) -> None:
        class SmartStringsSelector(Selector):
            _lxml_smart_strings = True

        selector = SmartStringsSelector(text="<a><b>one</b><b>two</b></a>")
        text_nodes = selector.xpath("//b/text()")
        assert all(hasattr(node.root, "getparent") for node in text_nodes)
        selector.xpath("//b")[0].drop()
        assert text_nodes[1].get() == "two"
        assert selector.xpath("//b/text()").getall() == ["two"]


class TestJsonSharing:
    def test_json_views_share_parsed_data(self) -> None:
        payload = '{"items": [{"name": "a"}, {"name": "b"}]}'
        document = SelectorDocument(text=payload)
        assert document.type == "json"
        first = document.selector()
        second = document.selector()
        assert first.jmespath("items[*].name").getall() == ["a", "b"]
        assert second.jsonpath("$.items[*].name").getall() == ["a", "b"]
        assert first.root is second.root is document.root


class TestSubclassCompatibility:
    def test_custom_selector_keeps_shared_lineage(self) -> None:
        class CustomList(SelectorList["CustomSelector"]):
            pass

        class CustomSelector(Selector):
            selectorlist_cls = CustomList  # type: ignore[assignment]

            def label(self) -> str:
                return "item:" + (self.css("::attr(data-id)").get() or "")

        class CustomDocument(SelectorDocument):
            selector_cls = CustomSelector

        document = CustomDocument(text=HTML)
        view = document.selector()
        items = view.css("li.product")
        assert isinstance(items, CustomList)
        assert [item.label() for item in items] == ["item:a", "item:b", "item:c"]
        items[0].drop()
        assert isinstance(view.css("li.product"), CustomList)
        assert len(view.css("li.product")) == 2
        assert len(document.selector().css("li.product")) == 3
