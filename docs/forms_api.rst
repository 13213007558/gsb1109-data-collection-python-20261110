HTML Forms API
==============

Forms in HTML documents are represented by :class:`mechanize.HTMLForm`. Every
form is a collection of controls. The different types of controls are
represented by the various classes documented below.

Filling controls by visible label
-----------------------------------

Hard-coding the ``name`` attribute of a field is fragile: when a site is
reworked, field names change even though the caption shown to the user (the
visible label) stays the same.  :meth:`mechanize.HTMLForm.find_control_by_label`
locates a control by that visible label instead, and
:meth:`mechanize.HTMLForm.set_by_label` fills it in one call and returns the
selected :class:`mechanize.Control` so you can inspect its real ``name``,
``id`` and ``type``.

Both kinds of real ``<label>`` association are supported: the explicit
``<label for="id">`` reference and a ``<label>`` that wraps the control.  When
a label is attached to more than one eligible control,
:class:`mechanize.AmbiguityError` is raised (the message lists every candidate);
pass ``nr=`` to disambiguate -- the first match is never picked silently.
Hidden and disabled controls are excluded unless ``include_hidden`` or
``include_disabled`` is set.

.. code-block:: python

    control = form.find_control_by_label("企业名称")
    form.set_by_label("联系人", "张三")

Many pages render captions as plain text (for example the left cell of a
table row) or in attributes such as ``aria-label`` rather than as real
``<label>`` elements.  Configure auxiliary label sources on the
:class:`mechanize.Browser` -- a bare tag name means the element's visible
text, a ``(tag, attribute)`` pair means an attribute value, and a callable
``(elem, parent_map) -> str`` allows fully custom sources:

.. code-block:: python

    browser = mechanize.Browser(
        label_sources=("td", ("input", "aria-label")))
    # or, later at runtime:
    browser.set_label_sources(("td",))

The same label-driven script then keeps working across two versions of a page
whose field names were renamed.  Auxiliary label matching is exact after
whitespace compression, and is switched off unless label sources are
configured, so default behaviour is unchanged.

.. autoclass:: mechanize.HTMLForm
    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.Control
    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.ScalarControl
    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.TextControl
    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.FileControl
    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.IgnoreControl
    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.ListControl
    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.RadioControl
    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.CheckboxControl
    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.SelectControl
    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.SubmitControl

    :members:
    :inherited-members:
    :show-inheritance:

.. autoclass:: mechanize.ImageControl
    :members:
    :inherited-members:
    :show-inheritance:

