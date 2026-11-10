Options
=======

Configuration
-------------


Input format
~~~~~~~~~~~~

The module expects strings as shown above, it is also possible to use already parsed HTML (i.e. a LXML tree object):

.. code-block:: python

    >>> from htmldate import find_date
    >>> from lxml import html
    >>> mytree = html.fromstring('<html><body><span class="entry-date">July 12th, 2016</span></body></html>')
    >>> find_date(mytree)
    '2016-07-12'

An external module can be used for download, as described in versions anterior to 0.3. This example uses the legacy mode with `requests <http://docs.python-requests.org/>`_ as external module.

.. code-block:: python

    >>> from htmldate.core import find_date
    # using requests
    >>> import requests
    >>> r = requests.get('https://creativecommons.org/about/')
    >>> find_date(r.text)
    '2017-11-28' # may change
    # using htmldate's own fetch_url function
    >>> from htmldate.utils import fetch_url
    >>> htmldoc = fetch_url('https://blog.wikimedia.org/2018/06/28/interactive-maps-now-in-your-language/')
    >>> find_date(htmldoc)
    '2018-06-28' # may change
    # or simply
    >>> find_date('https://blog.wikimedia.org/2018/06/28/interactive-maps-now-in-your-language/') # URL detected
    '2018-06-28' # may change


Date format
~~~~~~~~~~~

Change the output to a format known to Python's ``datetime`` module, the default being ``%Y-%m-%d``:

.. code-block:: python

    >>> find_date('https://www.gnu.org/licenses/gpl-3.0.en.html', outputformat='%d %B %Y')
    '18 November 2016' # may change
    >>> find_date('http://blog.python.org/2016/12/python-360-is-now-available.html', outputformat='%Y-%m-%dT%H:%M:%S%z')
    '2016-12-23T05:11:00-0500'


.. autofunction:: htmldate.validators.is_valid_format


Original date
~~~~~~~~~~~~~

Although the time delta between the original publication and the "last modified" statement is usually a matter of hours or days at most, it can be useful in some contexts to prioritize the original publication date during extraction:

.. code-block:: python

    >>> find_date('https://netzpolitik.org/2016/die-cider-connection-abmahnungen-gegen-nutzer-von-creative-commons-bildern/') # default setting
    '2019-06-24' # may change
    >>> find_date('https://netzpolitik.org/2016/die-cider-connection-abmahnungen-gegen-nutzer-von-creative-commons-bildern/', original_date=True) # modified behavior
    '2016-06-23'


Relative dates
~~~~~~~~~~~~~~

Cached or archived pages can contain relative expressions like "yesterday 23:30" whose meaning depends on when they are analyzed. The relative date mode makes the observation moment an explicit input instead of silently relying on the current time:

.. code-block:: python

    >>> find_date(
    ...     htmldoc,
    ...     relative_base="2026-11-10T08:00:00",  # observation moment
    ...     site_timezone="Europe/Berlin",        # time zone of the website
    ...     max_relative_days=7,                  # allowed expression range
    ... )

All three parameters are supplied by the caller: ``relative_base`` (a datetime or ISO 8601 string, naive values are read as local time in the site zone), ``site_timezone`` (an IANA name, required in this mode — the local system time zone is never used as a fallback), and ``max_relative_days`` (expressions further back are rejected, default: 7). Supported expressions include English, German and French day words ("yesterday", "gestern", "hier") with an optional clock time, as well as "ago"-style expressions ("2 hours ago", "vor 3 Tagen", "il y a une semaine").

The result depends only on this context, so repeated runs on different machines and in different system time zones are consistent. Day-level expressions are resolved as calendar days in the site time zone, sub-day expressions as absolute elapsed time computed in UTC; both are logged at INFO level. Wall times rendered ambiguous or nonexistent by DST transitions are not silently resolved: the ambiguity is logged and the expression is only used if all interpretations fall on the same calendar day. Absolute dates on a page are extracted as before and take precedence.


Settings
--------

See ``settings.py`` file:

.. automodule:: htmldate.settings
   :members:
   :show-inheritance:
   :undoc-members:


Clearing caches
~~~~~~~~~~~~~~~

.. code-block:: python

    >>> from htmldate.meta import reset_caches
    # at a given point in time
    >>> reset_caches()


Tests
-----

A series of HTML pages and patterns triggering different structural and content patterns is included for testing purposes:

.. code-block:: bash

    $ python3 -m pip install pytest
    $ pytest
