"""
Parsel lets you extract text from XML/HTML documents using XPath
or CSS selectors
"""

__author__ = "Scrapy project"
__email__ = "info@scrapy.org"
__version__ = "1.12.1"
__all__ = [
    "AbsoluteXPathWarning",
    "ExtractionError",
    "Recipe",
    "RecipeError",
    "Record",
    "Selector",
    "SelectorList",
    "StageError",
    "css2xpath",
    "xpathfuncs",
]

from parsel import xpathfuncs
from parsel.csstranslator import css2xpath
from parsel.recipe import ExtractionError, Recipe, RecipeError, Record, StageError
from parsel.selector import AbsoluteXPathWarning, Selector, SelectorList

xpathfuncs.setup()
