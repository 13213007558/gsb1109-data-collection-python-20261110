.. _provenance:

=====================
Provenance tracking
=====================

.. module:: itemloaders.provenance
   :synopsis: Optional provenance (source) tracking for Item Loaders

When someone asks "where did this final, cleaned number come from?", a
regular :class:`~itemloaders.ItemLoader` can only show the cleaned result.
:class:`~itemloaders.provenance.TrackedItemLoader` is an opt-in subclass that
additionally records, for every collected value, which raw inputs it came
from.

Entry points accept optional ``page``, ``selector`` and ``raw`` identifiers;
``add_css``/``add_xpath``/``add_jmes`` fill in ``selector`` automatically::

    from itemloaders.provenance import TrackedItemLoader

    loader = TrackedItemLoader(selector=selector, page="page-a")
    loader.add_css("price", "span.price::text")
    ...
    item = loader.load_item()
    loader.get_field_sources("price")
    # {SourceRef(page='page-a', selector='span.price::text', raw='$10.00')}

Provenance travels with the data through expansion (``re=`` extraction,
one-to-many functions in :class:`~itemloaders.processors.MapCompose`),
filtering and merging, following the actual data flow per element — outputs
are never matched back to their inputs by position. Every processor is still
executed exactly once; tracking never re-runs a processor.

Legacy processors that cannot describe their input→output mapping are
handled conservatively: their outputs are linked to all their inputs and
flagged with ``UNKNOWN_RANGE`` instead of inventing a precise mapping.
Custom processors can provide precise provenance by implementing
``apply_tracked(self, tracked, loader_context=None)``.

Regular :class:`~itemloaders.ItemLoader` is completely unaffected and pays
no tracking overhead.

API reference
=============

.. autoclass:: TrackedItemLoader
   :members: get_field_sources, get_tracked_values

.. autoclass:: SourceRef

.. autoclass:: TrackedValue

.. autodata:: UNKNOWN_RANGE
