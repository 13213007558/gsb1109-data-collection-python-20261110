"""
客服场景：一个商品价格到底来自哪里？

两个页面的价格经过 抽取 → 清洗 → 过滤 → 合并 得到一个最终数字，
get_field_sources 仍能指出它依赖哪些原始输入。
"""

import re

from parsel import Selector

from itemloaders import ItemLoader
from itemloaders.processors import Identity, MapCompose, TakeFirst
from itemloaders.provenance import (
    UNKNOWN_RANGE,
    SourceRef,
    TrackedItemLoader,
    TrackedValue,
)

PAGE_A_HTML = """
<html><body>
  <span class="price">$10.00</span>
  <span class="price">售罄</span>
</body></html>
"""

PAGE_B_HTML = """
<html><body>
  <span class="price">$12.00</span>
  <span class="price">$15.00</span>
</body></html>
"""

CSS_PRICE = "span.price::text"


def parse_price(text):
    """'$10.00' -> 10.0；无法解析的文本（如 '售罄'）返回 None，被过滤。"""
    match = re.fullmatch(r"\$([\d.]+)", text.strip())
    return float(match.group(1)) if match else None


class PageLoader(TrackedItemLoader):
    """单页装载：CSS 抽取 → MapCompose 清洗并过滤。"""

    default_input_processor = MapCompose(parse_price)
    default_output_processor = TakeFirst()


class LowestPrice:
    """来源感知的处理器：取最低价，并精确指出它来自哪条输入。

    apply_tracked 就是这一次计算本身，不会为了追踪再执行一遍 __call__。
    """

    def __call__(self, values):
        return min(values)

    def apply_tracked(self, tracked, loader_context=None):
        elements = [tracked] if isinstance(tracked, TrackedValue) else list(tracked)
        return min(elements, key=lambda element: element.value)


class MergeLoader(TrackedItemLoader):
    """合并装载：输入已是清洗后的数字（Identity），输出取最低价。"""

    price_in = Identity()
    price_out = LowestPrice()


def collect_page(html, page_id):
    loader = PageLoader(selector=Selector(text=html), page=page_id)
    loader.add_css("price", CSS_PRICE)
    return loader.get_tracked_values("price")


def test_two_pages_filtered_then_merged_into_one_price():
    # 抽取 + 过滤：'售罄' 被 MapCompose 丢弃，page-a 只剩 $10.00
    tracked_a = collect_page(PAGE_A_HTML, "page-a")
    tracked_b = collect_page(PAGE_B_HTML, "page-b")
    assert [t.value for t in tracked_a] == [10.0]
    assert [t.value for t in tracked_b] == [12.0, 15.0]
    assert tracked_a[0].sources == {
        SourceRef(page="page-a", selector=CSS_PRICE, raw="$10.00")
    }

    # 合并：两页的清洗结果合成一个最终价格（最低价）
    loader = MergeLoader()
    loader.add_value("price", tracked_a)
    loader.add_value("price", tracked_b)
    item = loader.load_item()
    assert item["price"] == 10.0

    # 客服提问：这个 10.0 是从哪来的？—— 精确指向 page-a 的 $10.00
    assert loader.get_field_sources("price") == {
        SourceRef(page="page-a", selector=CSS_PRICE, raw="$10.00")
    }


def test_legacy_processor_marks_range_unknown_instead_of_guessing():
    class LegacyMergeLoader(TrackedItemLoader):
        price_in = Identity()
        price_out = min  # 旧函数，无法说明输入→输出映射

    loader = LegacyMergeLoader()
    loader.add_value("price", collect_page(PAGE_A_HTML, "page-a"))
    loader.add_value("price", collect_page(PAGE_B_HTML, "page-b"))
    assert loader.load_item()["price"] == 10.0

    sources = loader.get_field_sources("price")
    # 不编造精确来源：候选输入全部列出，并明确标注范围未知
    assert UNKNOWN_RANGE in sources
    assert sources >= {
        SourceRef(page="page-a", selector=CSS_PRICE, raw="$10.00"),
        SourceRef(page="page-b", selector=CSS_PRICE, raw="$12.00"),
        SourceRef(page="page-b", selector=CSS_PRICE, raw="$15.00"),
    }


def test_one_to_many_expansion_tracks_elements_not_indexes():
    class SplitLoader(TrackedItemLoader):
        default_input_processor = MapCompose(lambda s: s.split(","))
        default_output_processor = Identity()

    loader = SplitLoader()
    loader.add_value("tag", "a,b", page="page-a")
    loader.add_value("tag", "c", page="page-b")
    values = loader.get_tracked_values("tag")
    assert [t.value for t in values] == ["a", "b", "c"]
    # 'b' 来自 page-a 的 "a,b"，而不是按下标硬配到 page-b 的输入
    assert values[1].sources == {SourceRef(page="page-a", raw="a,b")}
    assert values[2].sources == {SourceRef(page="page-b", raw="c")}


def test_re_extraction_expands_values_keeping_their_source():
    loader = TrackedItemLoader()
    loader.add_value("price", "prices: $1 and $2", page="page-a", re=r"\$(\d+)")
    values = loader.get_tracked_values("price")
    assert [t.value for t in values] == ["1", "2"]
    for t in values:
        assert t.sources == {SourceRef(page="page-a", raw="prices: $1 and $2")}


def test_plain_loader_is_untouched_by_tracking():
    loader = ItemLoader()
    loader.add_value("price", ["$10.00"], MapCompose(parse_price))
    assert loader.get_collected_values("price") == [10.0]
    assert not hasattr(loader, "get_field_sources")
    assert not hasattr(loader, "get_tracked_values")
