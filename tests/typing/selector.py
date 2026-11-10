# Basic usage of the Selector, strongly typed to test the typing of parsel's API.
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from parsel import Selector

if TYPE_CHECKING:
    from parsel.cssmatch import MatchExplanation


def correct() -> None:
    selector = Selector(
        text="<html><body><ul><li>1</li><li>2</li><li>3</li></ul></body></html>"
    )

    li_values: list[str] = selector.css("li").getall()
    selector.re_first(re.compile(r"[32]"), "").strip()
    xpath_values: list[str] = selector.xpath(
        "//somens:a/text()", namespaces={"somens": "http://scrapy.org"}
    ).extract()

    explanation: MatchExplanation = selector.css("li")[0].css_match_explanation(
        "li:first-child"
    )
    explanations: list[MatchExplanation] = selector.css(
        "li"
    ).css_match_explanation("li")
    assert explanation.matched or not explanations

    class MySelector(Selector):
        def my_own_func(self) -> int:
            return 3

    my_selector = MySelector()
    res: int = my_selector.my_own_func()
    sub_res: int = my_selector.xpath("//somens:a/text()")[0].my_own_func()
    jsonpath_res: int = my_selector.jsonpath("$.a")[0].my_own_func()


# Negative checks: all the code lines below have typing errors.
# the "# type: ignore" comment makes sure that mypy identifies them as errors.


def incorrect() -> None:
    selector = Selector(
        text="<html><body><ul><li>1</li><li>2</li><li>3</li></ul></body></html>"
    )

    # Wrong query type in css.
    selector.css(5).getall()  # type: ignore[arg-type]

    # Cannot assign a list of str to an int.
    li_values: int = selector.css("li").getall()  # type: ignore[assignment]

    # Cannot use a string to define namespaces in xpath.
    selector.xpath(
        "//somens:a/text()",
        namespaces='{"somens": "http://scrapy.org"}',  # type: ignore[arg-type]
    ).extract()

    # Typo in the extract method name.
    selector.css("li").extact()  # type: ignore[attr-defined]

    class MySelector(Selector):
        def my_own_func(self) -> int:
            return 3

    my_selector = MySelector()
    res: str = my_selector.my_own_func()  # type: ignore[assignment]
    sub_res: str = my_selector.xpath("//somens:a/text()")[0].my_own_func()  # type: ignore[assignment]
