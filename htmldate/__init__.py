"""
Htmldate extracts original and updated publication dates from URLs and web pages.
"""

# meta
__title__ = "htmldate"
__author__ = "Adrien Barbaresi"
__license__ = "Apache-2.0"
__copyright__ = "Copyright 2017-present, Adrien Barbaresi"
__version__ = "1.11.0"


import logging

from .core import find_date
from .history import SnapshotHistory, compare_snapshots

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = ["find_date", "compare_snapshots", "SnapshotHistory"]
