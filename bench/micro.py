from __future__ import annotations

import gc
import statistics
import time
from typing import TYPE_CHECKING

from parsel import Selector

if TYPE_CHECKING:
    from collections.abc import Callable

N_ITEMS = 3000
RUNS = 40

PAGE = (
    "<!DOCTYPE html><html><body><ul>"
    + "".join(
        f'<li class="p" data-i="{i}"><a href="/{i}">P{i}</a>'
        f'<span class="price">{i}.00</span></li>'
        for i in range(N_ITEMS)
    )
    + "</ul></body></html>"
)


def timeit(fn: Callable[[], object]) -> float:
    timings = []
    for _ in range(RUNS):
        gc.collect()
        start = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - start) * 1000)
    return statistics.median(timings)


def chained_css(sel: Selector) -> list[str | None]:
    out: list[str | None] = []
    for item in sel.css("li.p"):
        out.append(item.css("a::attr(href)").get())
        out.append(item.css(".price::text").get())
    return out


def chained_xpath(sel: Selector) -> list[str | None]:
    out: list[str | None] = []
    for item in sel.xpath("//li[@class='p']"):
        out.append(item.xpath("./a/@href").get())
        out.append(item.xpath(".//span/text()").get())
    return out


def main() -> None:
    sel = Selector(text=PAGE)
    print(f"build one selector  : {timeit(lambda: Selector(text=PAGE)):8.2f} ms")
    print(f"chained css x{RUNS}  : {timeit(lambda: chained_css(sel)):8.2f} ms")
    print(f"chained xpath x{RUNS}: {timeit(lambda: chained_xpath(sel)):8.2f} ms")
    print(f"broad css           : {timeit(lambda: sel.css('li.p')):8.2f} ms")


if __name__ == "__main__":
    main()
