"""
Parsel lets you extract text from XML/HTML documents using XPath
or CSS selectors
"""

__author__ = "Scrapy project"
__email__ = "info@scrapy.org"
__version__ = "1.12.1"
__all__ = [
    "AbsoluteXPathWarning",
    "LimitedQueryError",
    "QueryEvaluationError",
    "QueryInputSizeLimitError",
    "QueryResultLimitError",
    "QueryTimeoutError",
    "Selector",
    "SelectorList",
    "css2xpath",
    "run_limited_xpath",
    "xpathfuncs",
]

from parsel import xpathfuncs
from parsel.csstranslator import css2xpath
from parsel.limited import (
    LimitedQueryError,
    QueryEvaluationError,
    QueryInputSizeLimitError,
    QueryResultLimitError,
    QueryTimeoutError,
    run_limited_xpath,
)
from parsel.selector import AbsoluteXPathWarning, Selector, SelectorList

xpathfuncs.setup()
