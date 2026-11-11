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


Source policies
~~~~~~~~~~~~~~~

A URL date can be a section archive date rather than the article date, while on
other sites the URL is the only place where a date is available. The
``source_policy`` parameter lets each call decide which evidence channels may,
must not, or must provide the date. The five channels are:

- ``structured``: structured data such as JSON-LD (``datePublished``/``dateModified``),
- ``meta``: ``meta`` elements in the page header,
- ``time``: HTML ``time`` elements,
- ``content``: dates found in the page body and markup text,
- ``url``: dates read from the page URL (including canonical and image URLs).

Policies are attached to a single call. They never modify the default
configuration, so different policies can be used interleaved safely. A disabled
channel is not queried at all: the value is never extracted and then discarded.

.. code-block:: python

    >>> from htmldate import Source, SourcePolicy
    # a URL date must not be trusted on this site
    >>> policy = SourcePolicy(disable=["url"])
    >>> find_date(htmlstring, url=myurl, source_policy=policy)

    # or through a plain mapping / JSON string (e.g. from configuration)
    >>> find_date(htmlstring, source_policy={"disable": ["url"]})
    >>> find_date(htmlstring, source_policy='{"disable": ["url"]}')

Restrict the search to a subset of channels with ``allow``:

.. code-block:: python

    >>> SourcePolicy(allow=["structured", "meta"])

Require several channels to corroborate each other. When a required channel is
missing, or when the required channels do not agree on one date, the result is
unknown (``None``) instead of a guess:

.. code-block:: python

    >>> policy = SourcePolicy(require=["url", "meta"])
    >>> find_date(htmlstring, url=myurl, source_policy=policy)

Set ``explain=True`` to obtain an audit dictionary instead of a plain date. It
lists the adopted channels (``used``), every value that was considered, the
channels excluded by the policy with the reason, and any requirement that could
not be satisfied:

.. code-block:: python

    >>> find_date(htmlstring, source_policy=SourcePolicy(disable=["url"]), explain=True)
    {'result': '2017-01-02', 'used': [{'source': 'meta', ...}],
     'considered': {...}, 'excluded': {'url': 'disabled by policy'},
     'missing_required': [], 'disagreement': [], 'policy': {...}}

On the command line, pass inline JSON or a path to a JSON file with
``--source-policy`` and add ``--explain`` for the audit output:

.. code-block:: bash

    $ htmldate --source-policy '{"disable": ["url"]}' --explain < page.html

.. autoclass:: htmldate.sources.SourcePolicy
   :members:
   :show-inheritance:

.. autoclass:: htmldate.sources.Source
   :members:
   :undoc-members:


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
