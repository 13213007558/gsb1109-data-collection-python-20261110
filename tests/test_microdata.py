import warnings

import pytest

from w3lib.microdata import MicrodataItem, extract_microdata

BASE = "https://shop.example.com/catalog/page.html"


class TestItemsAndIdentity:
    def test_no_scope_returns_empty_list(self):
        assert extract_microdata("<p>plain text only</p>") == []
        assert extract_microdata("") == []

    def test_top_level_items_in_document_order(self):
        html = """
        <div itemscope><span itemprop="name">first</span></div>
        <section itemscope><span itemprop="name">second</span></section>
        """
        items = extract_microdata(html)
        assert [item["properties"]["name"] for item in items] == [
            ["first"],
            ["second"],
        ]

    def test_type_tokens_and_id(self):
        html = (
            '<div itemscope itemtype="https://schema.org/Product '
            'https://example.org/Local" itemid="/products/1"></div>'
        )
        item = extract_microdata(html, BASE)[0]
        assert item["type"] == [
            "https://schema.org/Product",
            "https://example.org/Local",
        ]
        assert item["id"] == "https://shop.example.com/products/1"
        assert item["properties"] == {}

    def test_absolute_id_is_not_rewritten(self):
        html = '<div itemscope itemid="https://id.example.org/x"></div>'
        assert extract_microdata(html, BASE)[0]["id"] == "https://id.example.org/x"

    def test_blank_type_and_id_are_omitted(self):
        item = extract_microdata('<div itemscope itemtype="" itemid=""></div>')[0]
        assert "type" not in item
        assert "id" not in item

    def test_itemscope_without_itemtype_keeps_properties_only(self):
        item = extract_microdata('<div itemscope><span itemprop="n">v</span></div>')[0]
        assert item == {"properties": {"n": ["v"]}}


class TestPropertyValues:
    def test_same_name_keeps_every_value_in_order(self):
        html = """
        <div itemscope>
          <span itemprop="tag">one</span>
          <span itemprop="tag">two</span>
          <span itemprop="tag">three</span>
        </div>
        """
        item = extract_microdata(html)[0]
        assert item["properties"]["tag"] == ["one", "two", "three"]

    def test_one_element_several_itemprop_names(self):
        html = '<div itemscope><a itemprop="url sameAs" href="/x">x</a></div>'
        properties = extract_microdata(html, BASE)[0]["properties"]
        expected = "https://shop.example.com/x"
        assert properties == {"url": [expected], "sameAs": [expected]}

    def test_duplicate_itemprop_tokens_attach_once(self):
        html = '<div itemscope><span itemprop="a a b">v</span></div>'
        assert extract_microdata(html)[0]["properties"] == {
            "a": ["v"],
            "b": ["v"],
        }

    def test_url_valued_elements_are_resolved(self):
        html = """
        <div itemscope>
          <a itemprop="anchor" href="a.html">a</a>
          <area itemprop="area" href="area.html">
          <link itemprop="link" href="link.html">
          <audio itemprop="audio" src="a.mp3"></audio>
          <embed itemprop="embed" src="e.swf">
          <iframe itemprop="iframe" src="f.html"></iframe>
          <img itemprop="image" src="i.png">
          <source itemprop="source" src="s.mp4">
          <track itemprop="track" src="t.vtt">
          <video itemprop="video" src="v.mp4"></video>
          <object itemprop="object" data="o.pdf"></object>
        </div>
        """
        properties = extract_microdata(html, BASE)[0]["properties"]
        base = "https://shop.example.com/catalog/"
        assert properties == {
            "anchor": [base + "a.html"],
            "area": [base + "area.html"],
            "link": [base + "link.html"],
            "audio": [base + "a.mp3"],
            "embed": [base + "e.swf"],
            "iframe": [base + "f.html"],
            "image": [base + "i.png"],
            "source": [base + "s.mp4"],
            "track": [base + "t.vtt"],
            "video": [base + "v.mp4"],
            "object": [base + "o.pdf"],
        }

    def test_missing_url_attribute_is_empty_string(self):
        html = (
            '<div itemscope><a itemprop="a">text</a>'
            '<img itemprop="i"><object itemprop="o"></object></div>'
        )
        properties = extract_microdata(html, BASE)[0]["properties"]
        assert properties == {"a": [""], "i": [""], "o": [""]}

    def test_empty_url_attribute_resolves_to_base(self):
        html = (
            '<div itemscope><a itemprop="a" href="">x</a>'
            '<img itemprop="i" src="  "></div>'
        )
        properties = extract_microdata(html, BASE)[0]["properties"]
        assert properties == {"a": [BASE], "i": [BASE]}

    def test_empty_url_attribute_without_base_stays_empty(self):
        html = '<div itemscope><a itemprop="a" href="">x</a></div>'
        assert extract_microdata(html)[0]["properties"]["a"] == [""]

    def test_named_attribute_elements(self):
        html = """
        <div itemscope>
          <meta itemprop="meta" content="meta value">
          <data itemprop="data" value="data value">shown</data>
          <meter itemprop="meter" value="3">three</meter>
          <time itemprop="time" datetime="2026-11-10">today</time>
        </div>
        """
        assert extract_microdata(html)[0]["properties"] == {
            "meta": ["meta value"],
            "data": ["data value"],
            "meter": ["3"],
            "time": ["2026-11-10"],
        }

    def test_missing_named_attribute_is_empty_string(self):
        html = (
            '<div itemscope><meta itemprop="m">'
            '<data itemprop="d">shown</data>'
            '<time itemprop="t">today</time></div>'
        )
        assert extract_microdata(html)[0]["properties"] == {
            "m": [""],
            "d": [""],
            "t": [""],
        }

    def test_text_content_collapses_whitespace_and_decodes_entities(self):
        html = (
            '<div itemscope><p itemprop="text">'
            "  Hello&nbsp;&amp;\n<b> bold </b>  <i>world</i>\t"
            "</p></div>"
        )
        assert extract_microdata(html)[0]["properties"]["text"] == [
            "Hello\xa0& bold world"
        ]

    def test_text_content_keeps_document_order_across_children(self):
        html = (
            '<div itemscope><p itemprop="s">'
            "a <span>b</span> c <span>d</span> e</p></div>"
        )
        assert extract_microdata(html)[0]["properties"]["s"] == ["a b c d e"]


class TestNestedItems:
    def test_nested_scope_is_item_value_and_not_top_level(self):
        html = """
        <div itemscope itemtype="https://schema.org/Product">
          <span itemprop="name">lamp</span>
          <div itemprop="brand" itemscope itemtype="https://schema.org/Organization">
            <span itemprop="name">Bright Co</span>
          </div>
        </div>
        """
        items = extract_microdata(html, BASE)
        assert len(items) == 1
        brand = items[0]["properties"]["brand"][0]
        assert brand == {
            "type": ["https://schema.org/Organization"],
            "properties": {"name": ["Bright Co"]},
        }

    def test_nested_scope_without_itemprop_stays_top_level(self):
        html = """
        <div itemscope itemid="outer">
          <div itemscope><span itemprop="inner">v</span></div>
        </div>
        """
        items = extract_microdata(html, BASE)
        assert [item.get("id") for item in items] == [
            "https://shop.example.com/catalog/outer",
            None,
        ]
        assert items[0]["properties"] == {}
        assert items[1]["properties"] == {"inner": ["v"]}

    def test_nested_scope_owns_its_descendants(self):
        html = """
        <div itemscope>
          <div itemscope><span itemprop="private">mine</span></div>
          <span itemprop="outer">visible</span>
        </div>
        """
        items = extract_microdata(html)
        assert items[0]["properties"] == {"outer": ["visible"]}
        assert items[1]["properties"] == {"private": ["mine"]}

    def test_deeply_nested_item(self):
        html = """
        <div itemscope itemtype="t1">
          <div itemprop="child" itemscope itemtype="t2">
            <div itemprop="grandchild" itemscope itemtype="t3">
              <span itemprop="leaf">deep</span>
            </div>
          </div>
        </div>
        """
        items = extract_microdata(html)
        assert len(items) == 1
        grandchild = items[0]["properties"]["child"][0]["properties"]["grandchild"][0]
        assert grandchild["type"] == ["t3"]
        assert grandchild["properties"]["leaf"] == ["deep"]


class TestItemref:
    def test_references_element_outside_scope_with_ownership(self):
        html = """
        <div itemscope itemref="external">
          <span itemprop="name">product</span>
        </div>
        <p id="external"><span itemprop="color">red</span></p>
        """
        item = extract_microdata(html)[0]
        assert item["properties"]["name"] == ["product"]
        assert item["properties"]["color"] == ["red"]

    def test_shared_reference_collected_for_each_owner(self):
        html = """
        <div itemscope itemref="shared"><span itemprop="name">one</span></div>
        <div itemscope itemref="shared"><span itemprop="name">two</span></div>
        <p id="shared" itemprop="warranty" itemscope>
          <span itemprop="years">2</span>
        </p>
        """
        items = extract_microdata(html, BASE)
        assert len(items) == 2
        for item in items:
            warranty = item["properties"]["warranty"][0]
            assert warranty["properties"]["years"] == ["2"]
        # The shared scope is a property of both, never a top-level item.
        assert all("warranty" in item["properties"] for item in items)

    def test_duplicate_reference_token_collected_once(self):
        html = """
        <div itemscope itemref="ref ref ref"><span itemprop="own">x</span></div>
        <p id="ref"><span itemprop="other">y</span></p>
        """
        properties = extract_microdata(html)[0]["properties"]
        assert properties == {"own": ["x"], "other": ["y"]}

    def test_referenced_scope_earlier_in_document_is_nested(self):
        # The referenced item comes before its owner in the document, so the
        # owner must be built before top-level status can be decided.
        html = """
        <div id="y" itemscope itemprop="maker" itemtype="T-Y">
          <span itemprop="yname">yval</span>
        </div>
        <div id="x" itemscope itemtype="T-X" itemref="y">
          <span itemprop="xname">xval</span>
        </div>
        """
        items = extract_microdata(html)
        assert len(items) == 1
        assert items[0]["type"] == ["T-X"]
        maker = items[0]["properties"]["maker"][0]
        assert maker["type"] == ["T-Y"]
        assert maker["properties"]["yname"] == ["yval"]

    def test_unknown_reference_is_ignored(self):
        html = '<div itemscope itemref="missing"><span itemprop="n">v</span></div>'
        assert extract_microdata(html)[0]["properties"] == {"n": ["v"]}

    def test_reference_inside_own_subtree_is_not_duplicated(self):
        html = """
        <div itemscope itemref="inner">
          <span id="inner" itemprop="name">once</span>
        </div>
        """
        assert extract_microdata(html)[0]["properties"] == {"name": ["once"]}

    def test_subtree_properties_precede_referenced_properties(self):
        html = """
        <div itemscope itemref="later">
          <span itemprop="first">1</span>
        </div>
        <span id="later" itemprop="second">2</span>
        """
        properties = extract_microdata(html)[0]["properties"]
        assert list(properties) == ["first", "second"]

    def test_reference_cycles_terminate(self):
        html = """
        <div id="a" itemscope itemref="b">
          <span itemprop="a-prop">a</span>
        </div>
        <div id="b" itemscope itemref="a">
          <span itemprop="b-prop">b</span>
        </div>
        """
        items = extract_microdata(html, BASE)
        assert items == [
            {"properties": {"a-prop": ["a"]}},
            {"properties": {"b-prop": ["b"]}},
        ]

    def test_nested_reference_cycle_reuses_in_progress_item(self):
        html = """
        <div id="a" itemscope itemref="b"><span itemprop="x">1</span></div>
        <div id="b" itemscope itemprop="y" itemref="a">
          <span itemprop="z">2</span>
        </div>
        """
        items = extract_microdata(html, BASE)
        assert len(items) == 1
        nested = items[0]["properties"]["y"][0]
        assert nested["properties"] == {"z": ["2"]}
        assert items[0]["properties"]["x"] == ["1"]


class TestBaseUrl:
    def test_base_tag_overrides_document_url(self):
        html = (
            '<base href="https://cdn.example.com/assets/page.html">'
            '<div itemscope><img itemprop="image" src="i.png"></div>'
        )
        item = extract_microdata(html, BASE)[0]
        assert item["properties"]["image"] == ["https://cdn.example.com/assets/i.png"]

    def test_relative_baseurl_keeps_values_relative_without_warning(self):
        html = (
            '<div itemscope><a itemprop="u" href="rel">x</a>'
            '<img itemprop="i" src="../i.png"></div>'
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            item = extract_microdata(html, "/local/base/")[0]
        assert item["properties"] == {"u": ["rel"], "i": ["../i.png"]}

    def test_no_baseurl_at_all(self):
        html = '<div itemscope><a itemprop="u" href="rel">x</a></div>'
        assert extract_microdata(html)[0]["properties"]["u"] == ["rel"]

    def test_absolute_base_tag_used_without_document_url(self):
        html = (
            '<base href="https://cdn.example.com/assets/page.html">'
            '<div itemscope><img itemprop="image" src="i.png"></div>'
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            item = extract_microdata(html, "/local/base/")[0]
        assert item["properties"]["image"] == ["https://cdn.example.com/assets/i.png"]

    def test_unresolvable_url_keeps_raw_value(self):
        html = '<div itemscope><a itemprop="u" href="rel">x</a></div>'
        item = extract_microdata(html, "data:text/plain,hi")[0]
        assert item["properties"]["u"] == ["rel"]


class TestBytesInput:
    def test_bytes_decoded_with_encoding(self):
        html = ('<div itemscope><span itemprop="name">café</span></div>').encode(
            "latin-1"
        )
        item = extract_microdata(html, BASE, encoding="latin-1")[0]
        assert item["properties"]["name"] == ["café"]

    def test_bytes_default_utf8(self):
        html = "<div itemscope><span itemprop='n'>☃</span></div>".encode()
        assert extract_microdata(html)[0]["properties"]["n"] == ["☃"]


class TestTwoProductsPage:
    def test_every_value_keeps_its_owner(self):
        html = """
        <html><head><base href="https://shop.example.com/catalog/page.html"></head>
        <body>
        <article itemscope itemtype="https://schema.org/Product"
                 itemid="p1" itemref="shared">
          <h1 itemprop="name">Red lamp</h1>
          <span itemprop="name">Desk lamp</span>
          <img itemprop="image" src="/img/lamp.jpg">
          <div itemprop="brand" itemscope
               itemtype="https://schema.org/Organization">
            <span itemprop="name">Bright Co</span>
            <link itemprop="url" href="/brands/bright">
          </div>
        </article>
        <article itemscope itemtype="https://schema.org/Product"
                 itemid="/p/2" itemref="shared">
          <h1 itemprop="name">Blue chair</h1>
        </article>
        <p id="shared" itemprop="shippingDetails" itemscope
           itemtype="https://schema.org/OfferShippingDetails">
          <span itemprop="shippingLabel">Standard</span>
        </p>
        </body></html>
        """
        items: list[MicrodataItem] = extract_microdata(
            html, "https://shop.example.com/catalog/page.html"
        )
        assert len(items) == 2

        first, second = items
        assert first["type"] == ["https://schema.org/Product"]
        assert first["id"] == "https://shop.example.com/catalog/p1"
        assert first["properties"]["name"] == ["Red lamp", "Desk lamp"]
        assert first["properties"]["image"] == ["https://shop.example.com/img/lamp.jpg"]
        brand = first["properties"]["brand"][0]
        assert brand["type"] == ["https://schema.org/Organization"]
        assert brand["properties"]["name"] == ["Bright Co"]
        assert brand["properties"]["url"] == ["https://shop.example.com/brands/bright"]

        assert second["id"] == "https://shop.example.com/p/2"
        assert second["properties"]["name"] == ["Blue chair"]

        for item in items:
            shipping = item["properties"]["shippingDetails"][0]
            assert shipping["type"] == ["https://schema.org/OfferShippingDetails"]
            assert shipping["properties"]["shippingLabel"] == ["Standard"]


@pytest.mark.parametrize(
    "html",
    [
        "<div itemscope><span itemprop='n'>v</span></div>",
        "<DIV ITEMSCOPE><SPAN ITEMPROP='n'>v</SPAN></DIV>",
    ],
)
def test_tag_and_attribute_names_are_case_insensitive(html):
    assert extract_microdata(html)[0]["properties"]["n"] == ["v"]
