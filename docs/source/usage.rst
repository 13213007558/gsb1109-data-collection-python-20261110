*****
Usage
*****

Search for historical mementos (archived copies) of a URL. Download metadata
about the mementos and/or the memento content itself.

Tutorial
========

.. Keep cached HTTP responses for this tutorial so we aren't constantly hitting
.. real Wayback Machine APIs.
.. ipython:: python
   :suppress:

   import vcr
   __cassette = vcr.use_cassette(f'{__cassette_path}/tutorial.yaml')
   __cassette.__enter__()


What is the earliest memento of nasa.gov?
-----------------------------------------

Instantiate a :class:`WaybackClient`.

.. ipython:: python

   from wayback import WaybackClient
   client = WaybackClient()

Search for all Wayback's records for nasa.gov.

.. ipython:: python

   results = client.search('nasa.gov')

This statement should execute fairly quickly because it doesn't actually do
much work. The object we get back, ``results``, is a *generator*, a "lazy"
object from which we can pull results, one at a time. As we pull items
out of it, it loads them as needed from the Wayback Machine in chronological
order. We can see that ``results`` by itself is not informative:

.. ipython:: python

   results

There are couple ways to pull items out of generator like ``results``. One
simple way is to use the built-in Python function :func:`next`, like so:

.. ipython:: python

   record = next(results)

This takes a moment to run because, now that we've asked to see the first item
in the generator, this lazy object goes to fetch a chunk of results from the
Wayback Machine. Looking at the record in detail,

.. ipython:: python

   record

we can find our answer: Wayback's first memento of nasa.gov was in 1996. We
can use dot access on ``record`` to access the timestamp specifically.

.. ipython:: python

   record.timestamp

How many times does the word 'mars' appear on nasa.gov?
-------------------------------------------------------

Above, we access the metadata for the oldest memento on nasa.gov, stored in
the variable ``record``. Starting from where we left off, we'll access the
*content* of the memento and do a very simple analysis.

The Wayback Machine provides multiple *playback modes* to view the data it has
captured. The :attr:`wayback.Mode.view` mode is a copy edited for human viewers
on the web, and the :attr:`wayback.Mode.original` mode is the original copy of
what was captured when the page was scraped. For analysis purposes, we
generally want ``original``. (Check the documentation of :class:`wayback.Mode`
for a few other, less commonly used modes.)

Let's download the original content using ``WaybackClient``. (You could
download the content directly with an HTTP library like ``requests``, but
``WaybackClient`` adds extra tools for dealing with Wayback Machine servers.)

.. ipython:: python

   from wayback import Mode

   # `Mode.original` is the default and doesn't need to be explicitly set;
   # we've set it here to show how you might choose other modes.
   response = client.get_memento(record, mode=Mode.original)
   content = response.content.decode()

We can use the built-in method ``count`` on strings to count the number of
times that ``'mars'`` appears in the content.

.. ipython:: python

   content.count('mars')

This is case-sensitive, so to be more accurate we should convert the content to
lowercase first.

.. ipython:: python

   content.lower().count('mars')

We picked up a couple additional occurrences that the original count missed.

.. Close out cached HTTP response recording.
.. ipython:: python
   :suppress:

   __cassette.__exit__()


Choosing a small, well-spread sample of snapshots
==================================================

A URL can have tens of thousands of captures, but you may only have the budget
to download a handful of mementos. Picking the first 20 results would often
concentrate all of them in a single day. :meth:`WaybackClient.sample_mementos`
chooses a time-aware sample using only the CDX index (timestamps and content
digests), so no memento bodies are downloaded until after the selection is
made:

.. code-block:: python

   from wayback import DuplicatePolicy

   selection = client.sample_mementos(
       'http://example.com',
       budget=20,
       from_date='20100101',
       to_date='20200101',
       duplicate_policy=DuplicatePolicy.representative,
   )

Selection prioritizes the first and last captures in the range, then content
change points after the longest empty windows, and finally time representatives
of runs where the digest repeats. Pass ``duplicate_policy='drop'`` to
deduplicate repeated content instead. When the budget cannot cover every
candidate, ``selection.gaps`` lists the omitted time spans and how many
captures each one contains. :meth:`SampleSelection.report` prints the
selection list and its coverage rationale:

.. code-block:: python

   print(selection.report())
   for record in selection.records:
       memento = client.get_memento(record)


API Documentation
=================

The Wayback Machine exposes its data through two different mechanisms,
implementing two different standards for archival data, the CDX API and the
Memento API. We implement a Python client that can speak both.

.. autoclass:: wayback.WaybackClient

    .. automethod:: search
    .. automethod:: sample_mementos
    .. automethod:: get_memento

.. autofunction:: wayback.sample_snapshots

.. autoclass:: wayback.SampleSelection

    .. automethod:: report

.. autoclass:: wayback.SelectionReason

.. autoclass:: wayback.CoverageGap

.. autoclass:: wayback.DuplicatePolicy

.. autoclass:: wayback.CdxRecord

.. autoclass:: wayback.Memento

    .. automethod:: close
    .. automethod:: parse_memento_headers

.. autoclass:: wayback.WaybackSession

    .. automethod:: reset

Utilities
---------

.. autofunction:: wayback.memento_url_data

.. autoclass:: wayback.Mode

.. autoclass:: wayback.RateLimit

Exception Classes
-----------------

.. autoclass:: wayback.exceptions.WaybackException

.. autoclass:: wayback.exceptions.UnexpectedResponseFormat

.. autoclass:: wayback.exceptions.BlockedByRobotsError

.. autoclass:: wayback.exceptions.BlockedSiteError

.. autoclass:: wayback.exceptions.MementoPlaybackError

.. autoclass:: wayback.exceptions.NoMementoError

.. autoclass:: wayback.exceptions.RateLimitError

.. autoclass:: wayback.exceptions.WaybackRetryError

.. autoclass:: wayback.exceptions.SessionClosedError
