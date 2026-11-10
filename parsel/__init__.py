"""
Parsel lets you extract text from XML/HTML documents using XPath
or CSS selectors
"""

__author__ = "Scrapy project"
__email__ = "info@scrapy.org"
__version__ = "1.12.1"
__all__ = [
    "AbsoluteXPathWarning",
    "Selector",
    "SelectorList",
    "UnsupportedSelectorSyntax",
    "css2xpath",
    "xpathfuncs",
]

from parsel import xpathfuncs
from parsel.cssmatch import UnsupportedSelectorSyntax
from parsel.csstranslator import css2xpath
from parsel.selector import AbsoluteXPathWarning, Selector, SelectorList

xpathfuncs.setup()
