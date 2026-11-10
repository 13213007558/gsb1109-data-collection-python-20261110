"""
Parsel lets you extract text from XML/HTML documents using XPath
or CSS selectors
"""

__author__ = "Scrapy project"
__email__ = "info@scrapy.org"
__version__ = "1.12.1"
__all__ = [
    "AbsoluteXPathWarning",
    "MissingValue",
    "MultipleMatches",
    "Row",
    "RowExtractor",
    "RowReport",
    "Selector",
    "SelectorList",
    "Violation",
    "css2xpath",
    "many",
    "optional",
    "required",
    "xpathfuncs",
]

from parsel import xpathfuncs
from parsel.csstranslator import css2xpath
from parsel.rows import (
    MissingValue,
    MultipleMatches,
    Row,
    RowExtractor,
    RowReport,
    Violation,
    many,
    optional,
    required,
)
from parsel.selector import AbsoluteXPathWarning, Selector, SelectorList

xpathfuncs.setup()
