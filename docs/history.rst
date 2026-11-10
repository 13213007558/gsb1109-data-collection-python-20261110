Offline snapshot comparison
===========================

.. contents:: **Contents**
    :backlinks: none


A page that is crawled repeatedly can be edited between observations: its
publication date may be corrected, its "last modified" value may move
forwards or backwards, and a declaration present in one snapshot may be
missing from the next one. Taking the newest snapshot alone therefore loses
the history of what the page actually claimed.

The :func:`~htmldate.history.compare_snapshots` entry point works offline on
several HTML snapshots of the same page, each paired with the time at which
it was observed. It does not overwrite earlier observations with a single
latest date: every result can be traced back to the snapshot(s) supporting it.

Input ordering and conflicts
----------------------------

Snapshots may be supplied out of order; they are sorted by observation time.
When several snapshots share an observation time their input order is
preserved, and conflicting date statements at that same time are all retained
and reported instead of being silently merged.

Declarations versus inferred dates
----------------------------------

Two layers are kept strictly separate:

- **Declarations** are the date statements literally found in a snapshot
  (``meta`` elements, JSON-LD, ``time`` elements or a clearly labeled
  full-document heuristic). Each one stores its role (``original``,
  ``updated`` or ``unknown``), its normalized value, its raw expression and
  the markup location it comes from.
- **Trusted dates** are inferred per role across the whole history. A value
  seen repeatedly is ``confirmed``; a backward move to a first-time value is
  flagged ``corrected``; the return of a previously seen value is flagged
  ``rolled_back``; same-time contradictions are flagged ``conflict``; and
  unsupported forward moves stay ``undetermined``.

The event timeline states when each declaration ``appeared``, ``changed`` or
``disappeared``, citing the snapshot documenting every step. An unparseable
snapshot is treated as missing evidence: it never generates a disappearance
event and makes the content comparison inconclusive rather than negative.

Article updates
---------------

A changing date statement is not proof that the article body changed. The
visible text of each snapshot is fingerprinted, so an update is only reported
(``article_updated`` set to ``yes``) when the body text is known to differ.
Identical bodies yield ``no`` even if a modified date was rolled back, and
unparseable snapshots yield ``undetermined``.

Example
-------

.. code-block:: python

    >>> from datetime import datetime
    >>> from htmldate import compare_snapshots
    >>> snapshots = [
    ...     ("s3", datetime(2024, 3, 1), page_v3),
    ...     ("s1", datetime(2024, 1, 1), page_v1),
    ...     ("s2", datetime(2024, 2, 1), page_v2),
    ... ]
    >>> history = compare_snapshots(snapshots)
    >>> history.article_updated
    'yes'
    >>> history.trusted_dates["original"].status
    'corrected'
    >>> [event.event for event in history.events]
    ['appeared', 'changed', 'appeared']
    >>> history.as_dict()  # JSON-serializable, every entry cites a snapshot

On the command line, ``--compare`` takes a JSON file (or ``-`` for standard
input) describing the snapshots:

.. code-block:: json

    [
      {
        "id": "s1",
        "observed_at": "2024-01-01T00:00:00",
        "file": "snapshots/s1.html"
      },
      {
        "id": "s2",
        "observed_at": "2024-02-01T00:00:00",
        "html": "<html>...</html>"
      }
    ]

.. code-block:: bash

    $ htmldate --compare snapshots.json


Reference
---------

.. autofunction:: htmldate.history.compare_snapshots

.. autofunction:: htmldate.history.analyze_snapshot

.. autoclass:: htmldate.history.SnapshotHistory
    :members:

.. autoclass:: htmldate.history.SnapshotAnalysis
    :members:

.. autoclass:: htmldate.history.DateDeclaration
    :members:

.. autoclass:: htmldate.history.DateEvent
    :members:

.. autoclass:: htmldate.history.TrustedDate
    :members:
