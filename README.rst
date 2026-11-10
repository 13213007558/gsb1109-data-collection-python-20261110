========
queuelib
========

.. image:: https://img.shields.io/pypi/v/queuelib.svg
   :target: https://pypi.python.org/pypi/queuelib

.. image:: https://img.shields.io/pypi/pyversions/queuelib.svg
   :target: https://pypi.python.org/pypi/queuelib

.. image:: https://github.com/scrapy/queuelib/actions/workflows/tests-ubuntu.yml/badge.svg
   :target: https://github.com/scrapy/queuelib/actions/workflows/tests-ubuntu.yml

.. image:: https://img.shields.io/codecov/c/github/scrapy/queuelib/master.svg
   :target: http://codecov.io/github/scrapy/queuelib?branch=master
   :alt: Coverage report


Queuelib is a Python library that implements object collections which are stored
in memory or persisted to disk, provide a simple API, and run fast.

Queuelib provides collections for queues_ (FIFO), stacks_ (LIFO), queues
sorted by priority, queues that are emptied in a round-robin_ fashion, and a
capacity-bounded queue metered by the declared size of its tasks.

.. note:: Queuelib collections are not thread-safe.

Queuelib supports Python 3.10+ and has no dependencies.

.. _queues: https://en.wikipedia.org/wiki/FIFO_(computing_and_electronics)
.. _round-robin: https://en.wikipedia.org/wiki/Round-robin_scheduling
.. _stacks: https://en.wikipedia.org/wiki/Stack_(abstract_data_type)

Installation
============

You can install Queuelib either via the Python Package Index (PyPI) or from
source.

To install using pip::

    $ pip install queuelib

To install using easy_install::

    $ easy_install queuelib

If you have downloaded a source tarball you can install it by running the
following (as root)::

    # python setup.py install

FIFO/LIFO memory queues
=======================

``FifoMemoryQueue`` and ``LifoMemoryQueue`` hold their items in memory, take no
path, and accept objects of any type::

    >>> from queuelib import FifoMemoryQueue
    >>> q = FifoMemoryQueue()
    >>> q.push({'a': 1})
    >>> q.pop()
    {'a': 1}

FIFO/LIFO disk queues
=====================

Queuelib provides four disk queue classes, all of which store bytes:
``FifoDiskQueue`` and ``LifoDiskQueue``, which use a file format of their own,
and ``FifoSQLiteQueue`` and ``LifoSQLiteQueue``, which use SQLite. See
`Choosing a disk queue class`_ for the trade-off.

Here is an example usage of the FIFO queue::

    >>> from queuelib import FifoDiskQueue
    >>> q = FifoDiskQueue("queuefile")
    >>> q.push(b'a')
    >>> q.push(b'b')
    >>> q.push(b'c')
    >>> q.pop()
    b'a'
    >>> q.close()
    >>> q = FifoDiskQueue("queuefile")
    >>> q.pop()
    b'b'
    >>> q.pop()
    b'c'
    >>> q.pop()
    >>>

The other disk queue classes have the same API, and the LIFO ones pop the item
that was pushed last.

PriorityQueue
=============

A discrete-priority queue implemented by combining multiple FIFO/LIFO queues
(one per priority).

First, select the type of queue to be used per priority (FIFO or LIFO)::

    >>> from queuelib import FifoDiskQueue
    >>> qfactory = lambda priority: FifoDiskQueue('queue-dir-%s' % priority)

Then instantiate the Priority Queue with it::

    >>> from queuelib import PriorityQueue
    >>> pq = PriorityQueue(qfactory)

And use it::

    >>> pq.push(b'a', 3)
    >>> pq.push(b'b', 1)
    >>> pq.push(b'c', 2)
    >>> pq.push(b'd', 2)
    >>> pq.pop()
    b'b'
    >>> pq.pop()
    b'c'
    >>> pq.pop()
    b'd'
    >>> pq.pop()
    b'a'

RoundRobinQueue
===============

Has nearly the same interface and implementation as a Priority Queue except
that each element must be pushed with a (mandatory) key.  Popping from the
queue cycles through the keys "round robin".

Instantiate the Round Robin Queue similarly to the Priority Queue::

    >>> from queuelib import RoundRobinQueue
    >>> rr = RoundRobinQueue(qfactory)

And use it::

    >>> rr.push(b'a', '1')
    >>> rr.push(b'b', '1')
    >>> rr.push(b'c', '2')
    >>> rr.push(b'd', '2')
    >>> rr.pop()
    b'a'
    >>> rr.pop()
    b'c'
    >>> rr.pop()
    b'b'
    >>> rr.pop()
    b'd'

SizedQueue
==========

``SizedQueue`` is a capacity-bounded queue for bursty producers. The other
queues only bound the number of items they hold; this one bounds the total
*declared size* of the waiting tasks, so a sudden flood of large tasks cannot
grow memory or disk usage without limit.

Every task is wrapped in a ``SizedTask`` carrying an exact, caller-declared
``size``. That number is treated as an opaque unit and is never estimated from
the object or its serialized form, so two tasks with the same ``size`` cost
the same capacity regardless of what they contain::

    >>> from queuelib import SizedQueue, SizedQueueConfig, SizedTask
    >>> import pathlib, tempfile
    >>> path = pathlib.Path(tempfile.mkdtemp()) / "sized"
    >>> q = SizedQueue(path, SizedQueueConfig(capacity=10))
    >>> q.push(SizedTask(item="a", size=6)).accepted
    True

Rejecting tasks
---------------

With the default ``OverflowPolicy.REJECT`` a task that does not fit is not
admitted. ``push()`` always returns a ``PushResult`` that hands the rejected
task back to the caller in full, together with a machine-readable ``reason``::

    >>> result = q.push(SizedTask(item="b", size=5))
    >>> result.accepted
    False
    >>> result.reason
    'full'
    >>> result.rejected.item
    'b'
    >>> len(q), q.used
    (1, 6)

Evicting lower-value tasks
--------------------------

With ``OverflowPolicy.EVICT`` the queue removes waiting tasks until the new
one fits. Which tasks go first is fully determined by the configuration:
``eviction_priority`` selects the value level to reclaim, and
``eviction_order`` breaks ties between tasks of the same priority. The
default ``EvictionPriority.LOWEST_VALUE`` treats the smallest task
``priority`` number as the lowest value and evicts it first;
``EvictionPriority.HIGHEST_VALUE`` inverts that, matching the scale of
:class:`~queuelib.pqueue.PriorityQueue`. The default order is
``EvictionOrder.FIFO`` (oldest first), with ``EvictionOrder.LIFO`` available.
A task created with ``pinned=True`` is never evicted::

    >>> from queuelib import OverflowPolicy
    >>> evict_path = pathlib.Path(tempfile.mkdtemp()) / "evicting"
    >>> cfg = SizedQueueConfig(capacity=10, on_full=OverflowPolicy.EVICT)
    >>> q = SizedQueue(evict_path, cfg)
    >>> q.push(SizedTask(item="low", size=4, priority=1)).accepted
    True
    >>> q.push(SizedTask(item="high", size=6, priority=9)).accepted
    True
    >>> result = q.push(SizedTask(item="incoming", size=4, priority=5))
    >>> [task.item for task in result.evicted]
    ['low']
    >>> result.accepted
    True

Every task removed to make room is returned in ``result.evicted``, and
``result.returned`` combines the evicted tasks with the rejected incoming
task (if any), so the caller always receives the complete objects it can no
longer find in the queue.

A task whose declared size exceeds the whole capacity can never be admitted.
It is rejected with the ``"oversized"`` reason and triggers no eviction, so
one oversized task cannot empty the queue through repeated removals::

    >>> result = q.push(SizedTask(item="huge", size=11))
    >>> result.reason
    'oversized'
    >>> result.evicted
    []

Statistics and persistence
--------------------------

The queue keeps cumulative counters in ``stats``: ``submitted``,
``accepted``, ``rejected`` and ``evicted``. They survive ``clear()`` (which
only removes the waiting tasks) and process restarts. The whole state is
stored as JSON under the path given on construction and rewritten atomically
after every change, so reopening the same path restores the tasks, the
configuration and the counters; passing a different configuration for an
existing path raises ``ValueError``::

    >>> q.stats
    SizedQueueStats(submitted=4, accepted=3, rejected=1, evicted=1)
    >>> q.close()
    >>> reopened = SizedQueue(evict_path)
    >>> reopened.stats
    SizedQueueStats(submitted=4, accepted=3, rejected=1, evicted=1)
    >>> [task.item for task in (reopened.pop(), reopened.pop()) if task]
    ['high', 'incoming']

Because state is JSON, task items must be JSON-serializable; a
non-serializable item raises ``TypeError`` before any state changes. The file
layout is an implementation detail that may change between releases, in the
same way as for the disk queues above.

Clearing a queue
================

``clear()`` removes every item from a queue, freeing the disk space that they
used, and leaves the queue open and usable::

    >>> q.clear()
    >>> len(q)
    0

``PriorityQueue`` and ``RoundRobinQueue`` also close their internal queues, the
same way that ``pop()`` does when one of them becomes empty.

Disk persistence
================

Disk queues write their items to the path they get on instantiation, so that a
queue can be resumed later, even by a different process.

Each class uses that path differently:

-   ``FifoDiskQueue`` uses a directory, which it creates, together with any
    missing parent directory. Items go into chunk files (``q00000``,
    ``q00001``, etc.), each holding up to ``chunksize`` items, and the queue
    also keeps an ``info.json`` file there for its own bookkeeping.

-   ``LifoDiskQueue``, ``FifoSQLiteQueue`` and ``LifoSQLiteQueue`` use a single
    file, whose parent directory must already exist.

The layout and the contents of those files, including the database schema of
the SQLite queues, are an implementation detail that may change in any release.
Do not read or write them yourself, and do not expect a queue written by one
version of Queuelib to be readable by a different one.

Choosing a disk queue class
---------------------------

``FifoDiskQueue`` and ``LifoDiskQueue`` are fast and lose data. They keep their
bookkeeping in memory until ``close()``, so a crash costs every item pushed
since the last ``close()`` call, and a failed write leaves the queue corrupt.

``FifoSQLiteQueue`` and ``LifoSQLiteQueue`` commit every ``push()`` and
``pop()`` call, so a crash or a failed write costs nothing. Each commit waits
for a disk flush, which on common storage takes about a millisecond and limits
these queues to a few hundred operations per second, against hundreds of
thousands for the file queues.

Measure that cost next to the work that your code does per item before letting
it decide: a millisecond is most of the budget when items are cheap to produce,
and noise when each one comes from the network. The subsections below cover
each failure mode in detail.

Always close disk queues
------------------------

``FifoDiskQueue`` and ``LifoDiskQueue`` do not survive a crash. Every item
pushed since the last ``close()`` call is lost if the process is killed or the
machine loses power.

While one of those queues is open, its bookkeeping (number of items, read and
write positions) only lives in memory, and ``close()`` is what writes it to
disk. Queuelib never calls ``fsync()`` either, and ``LifoDiskQueue`` writes
items through a buffered file object, so the most recent items may not have
reached the disk at all.

Calling ``close()`` is hence mandatory::

    from contextlib import closing

    with closing(FifoDiskQueue("queuedir")) as q:
        q.push(b'a')

If a process ends without calling ``close()``, the queue on disk keeps the
bookkeeping that the last ``close()`` call wrote, which no longer matches the
files. Items pushed since then become unreachable, and using the queue again
is unsafe: it may report a wrong length, return items that had already been
popped, delete files that still contain items, or raise an exception. Queuelib
offers no way to repair or to recover such a queue.

A SQLite queue that is not closed keeps every item that was pushed. It still
needs ``close()`` to delete the file of an empty queue.

A failed push corrupts a file queue
-----------------------------------

``push()`` on ``FifoDiskQueue`` or ``LifoDiskQueue`` may fail part way through
writing an item, for example with ``OSError`` when the disk is full. Queuelib
does not undo the part that was written, so from that point on the queue is
corrupt: ``pop()`` may return truncated or wrong data, or raise an exception,
and reopening the queue later does not help. Stop using a queue whose
``push()`` call raised.

On a SQLite queue the failed ``push()`` call is rolled back and the queue stays
usable.

Empty queues delete their files
-------------------------------

``close()`` on an empty queue deletes its file, or, in the case of
``FifoDiskQueue``, its chunk files and its ``info.json`` file, and also its
directory if nothing else remains in it. Using that same path again creates a
new, empty queue.

FifoDiskQueue frees disk space one chunk at a time
--------------------------------------------------

``FifoDiskQueue`` deletes a chunk file once every item in it has been popped.
Until then, popped items keep using disk space, so a queue uses up to
``chunksize`` items worth of disk space on top of the items that it holds.

Lower ``chunksize`` to lower that overhead, at the cost of more chunk files and
more file operations. For example, a queue that holds 400 items of 1 MB each
uses about 100 GB of disk space with the default ``chunksize`` of 100000, and
about 800 MB with a ``chunksize`` of 400.

Reopening a FifoDiskQueue keeps its chunk size
----------------------------------------------

``FifoDiskQueue`` stores its ``chunksize`` when creating a queue, and reuses
the stored value when reopening one, ignoring the ``chunksize`` parameter.

Use one queue object per path at a time
---------------------------------------

On top of not being thread-safe, a given path must not be used by more than one
open queue object at a time, in the same process or not.

``FifoDiskQueue`` and ``LifoDiskQueue`` do not lock the files that they use, so
such queue objects overwrite each other's items and bookkeeping; for example,
two ``FifoDiskQueue`` objects on the same directory return the same items, and
their ``close()`` calls may raise ``FileNotFoundError``.

SQLite serializes access to its file, but ``pop()`` on a SQLite queue reads and
deletes an item in separate steps, so concurrent queue objects on the same path
may return the same item more than once.

Persisting a PriorityQueue or a RoundRobinQueue
-----------------------------------------------

``PriorityQueue`` and ``RoundRobinQueue`` do not write anything to disk
themselves; their persistence comes entirely from the queues that ``qfactory``
builds, and it is up to ``qfactory`` to map a priority or a key to a valid
path.

Their ``close()`` method returns the priorities or keys whose underlying queue
was not empty. Storing that value is your responsibility, and so is passing it
back as ``startprios`` or ``start_domains`` on the next run::

    >>> import json
    >>> from queuelib import FifoDiskQueue, PriorityQueue
    >>> qfactory = lambda priority: FifoDiskQueue('queue-dir-%s' % priority)
    >>> pq = PriorityQueue(qfactory)
    >>> pq.push(b'a', 3)
    >>> active = pq.close()
    >>> with open('active.json', 'w') as f:
    ...     json.dump(active, f)
    ...
    >>> with open('active.json') as f:
    ...     startprios = json.load(f)
    ...
    >>> pq = PriorityQueue(qfactory, startprios)
    >>> pq.pop()
    b'a'

Priorities and keys that you do not pass back are not detected, and the items
in their queues stay on disk, unreachable.

Bug tracker
===========

If you have any suggestions, bug reports or annoyances please report them to
our issue tracker at: http://github.com/scrapy/queuelib/issues/

Contributing
============

Development of Queuelib happens at GitHub: http://github.com/scrapy/queuelib

You are highly encouraged to participate in the development. If you don't like
GitHub (for some reason) you're welcome to send regular patches.

All changes require tests to be merged.

Tests
=====

Tests are located in `queuelib/tests` directory. They can be run using
`nosetests`_ with the following command::

    nosetests

The output should be something like the following::

    $ nosetests
    .............................................................................
    ----------------------------------------------------------------------
    Ran 77 tests in 0.145s

    OK

License
=======

This software is licensed under the BSD License. See the LICENSE file in the
top distribution directory for the full license text.

Versioning
==========

This software follows `Semantic Versioning`_

.. _Scrapy framework: http://scrapy.org
.. _scrapy-users: http://groups.google.com/group/scrapy-users
.. _Semantic Versioning: http://semver.org/
.. _nosetests: https://nose.readthedocs.org/en/latest/
