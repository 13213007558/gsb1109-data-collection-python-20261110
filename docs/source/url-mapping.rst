**********************
Offline URL Mapping
**********************

When a site migrates to a new domain or reorganizes its directories, a Wayback
index typically contains only the *old* URLs, while researchers can identify a
handful of exact old-to-new pairs. The tools on this page apply explicit,
human-authored path rules to derive **candidate** new URLs for the rest of the
index. Everything runs offline; no network requests are made.

Safety model
============

The mapping tools deliberately separate evidence from certainty:

* **Confirmed** mappings come only from exact, researcher-provided pairs.
* **Candidate** mappings come from rules and exceptions. They always require
  human review before use.
* **Unresolved** URLs matched no pair and no rule, so nothing is guessed.
* Capture timestamps and content summaries may be attached as **evidence** for
  reviewers, but they never influence a mapping and can never promote a
  candidate to confirmed.
* One-to-many, many-to-one, and contradictory rule results are *preserved as
  conflicts*. Candidates are never silently merged because their strings look
  similar.
* Only confirmed, unambiguous mappings are returned by
  :meth:`wayback.UrlMappingConfig.safe_targets`. Feed only those to download
  code, so unconfirmed candidates can never overwrite existing files.

Configuration
=============

A configuration is a JSON file (or an equivalent Python mapping) with two
sections:

``known_mappings``
    Exact old-to-new pairs. A list of new URLs for one old URL is retained as
    a one-to-many conflict rather than collapsed.

``rules``
    Ordered path rules. Each rule has a ``match`` pattern, a ``new`` template,
    an optional ``id``, and optional ``exceptions``.

.. code-block:: json

    {
        "known_mappings": {
            "http://old.example.gov/about": "https://new.example.gov/about-us"
        },
        "rules": [
            {
                "id": "domain-move",
                "match": "http://old.example.gov/*",
                "new": "https://new.example.gov/{*}",
                "exceptions": [
                    {
                        "match": "http://old.example.gov/retired/*",
                        "note": "section retired, no replacement"
                    },
                    {
                        "match": "http://old.example.gov/special",
                        "new": "https://new.example.gov/other"
                    }
                ]
            },
            {
                "id": "reports",
                "match": "http://old.example.gov/reports/{report_id}",
                "new": "https://archive.example.gov/reports/{report_id}"
            }
        ]
    }

Patterns and templates
----------------------

* ``{name}`` in a pattern captures a named parameter (any characters except
  ``?`` and ``#``). Reference it in the template as ``{name}``.
* ``*`` in a pattern captures any text. Reference wildcard matches in a
  template in order as ``{*}``.
* A ``match`` beginning with ``/`` is tested against the URL path only. A
  ``new`` template beginning with ``/`` replaces only the path, preserving the
  scheme, host, query, and fragment.
* An exception without a ``new`` key **blocks** its rule for matching URLs
  (the ``note`` is carried into the result). An exception with ``new``
  **overrides** the rule.

Invalid configurations (missing keys, unknown template parameters, mismatched
wildcard counts, duplicate known pairs) raise
:class:`wayback.exceptions.MappingConfigError` when the config is built.

Building a review table
=======================

Apply the configuration to an index of old URLs. Plain URL strings work, and
so do objects exposing an ``original`` attribute, including
:class:`wayback.CdxRecord` results from :meth:`wayback.WaybackClient.search`:

.. code-block:: python

    from wayback import UrlMappingConfig, load_mapping_config, make_evidence

    config = load_mapping_config('url-mapping.json')

    index_urls = [
        'http://old.example.gov/about',
        'http://old.example.gov/reports/r-1',
        'http://old.example.gov/retired/2018-plan',
    ]
    evidence = {
        'http://old.example.gov/reports/r-1': make_evidence(
            captured_at='20200301120000', summary='sha1:abc123, 12 KB text/html'
        )
    }
    table = config.build_table(index_urls, evidence=evidence)

Each :class:`wayback.MappingResult` records the old URL, its
:class:`wayback.MappingStatus`, every :class:`wayback.Candidate` (with its
:class:`wayback.MatchType` and rule id), all :class:`wayback.Conflict`
objects, evidence, and notes.

Conflicts
---------

* ``one_to_many`` -- one old URL has several distinct candidate new URLs.
* ``many_to_one`` -- several distinct old URLs produce the same new URL.
* ``rule_conflict`` -- different rules produce different candidate sets for
  the same old URL.
* ``rule_vs_known`` -- a rule suggests a URL not among the known pairs for an
  old URL. The rule output is retained alongside the confirmed pair rather
  than discarded.

Human review
============

Export the table as CSV or JSON. Both formats list every candidate on its own
row; unresolved URLs appear with an empty candidate column so reviewers can see
what still needs research.

.. code-block:: python

    from wayback import export_review_csv, export_review_json
    import io

    stream = io.StringIO()
    export_review_csv(table, stream)

    export_review_json(table, 'url-mapping-review.json')

In the JSON review file, reviewers fill in ``confirmed_new_urls`` for entries
they have verified. Entries left with an empty list stay unconfirmed. Read the
decisions back with :func:`wayback.read_confirmations`, or drive automation
only from :meth:`wayback.UrlMappingConfig.safe_targets`. ``safe_targets``
excludes every candidate, unresolved, or ambiguously conflicting result (a
result with a one-to-many, rule-conflict, or rule-vs-known conflict has no
single safe download target). A ``many_to_one`` conflict does not disqualify
an otherwise confirmed pair, because each old URL still has one explicitly
provided target; the collision stays visible in the review table:

.. code-block:: python

    from wayback import read_confirmations

    decisions = read_confirmations('url-mapping-review.json')
    safe = config.safe_targets(table)

    # Only `safe` (or independently reviewed `decisions`) should be passed to
    # download code. Candidate URLs must never overwrite existing files.
    for old_url, new_urls in safe.items():
        ...

Reference
=========

.. autoclass:: wayback.UrlMappingConfig
   :members:

.. autoclass:: wayback.MappingResult
   :members:

.. autoclass:: wayback.Candidate
   :members:

.. autoclass:: wayback.Conflict
   :members:

.. autoclass:: wayback.MappingStatus
   :members:

.. autoclass:: wayback.MatchType
   :members:

.. autofunction:: wayback.normalize_url

.. autofunction:: wayback.make_evidence

.. autofunction:: wayback.load_mapping_config

.. autofunction:: wayback.export_review_csv

.. autofunction:: wayback.export_review_json

.. autofunction:: wayback.read_confirmations
