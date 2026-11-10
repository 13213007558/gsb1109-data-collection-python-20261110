HTML Forms API
==============

Forms in HTML documents are represented by :class:`mechanize.HTMLForm`. Every
form is a collection of controls. The different types of controls are
represented by the various classes documented below.

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

Refreshing a stale form
-----------------------

A page can change between the time a form was fetched and the time it is
submitted — for example, a hidden anti-CSRF token embedded in the page may
have rotated, so submitting the old form fails.  Re-fetch the page and use
:meth:`mechanize.HTMLForm.refresh` to carry over only the values the caller
actually edited, while keeping the new page's hidden values and untouched
defaults:

.. code-block:: python

    old_form = browser.form
    browser.open(browser.geturl())          # fresh GET of the same page
    browser.select_form(nr=0)
    old_form.refresh(browser.form)          # carry edits onto the new form
    browser.submit()                        # submits the new token

The browser-level :meth:`mechanize.Browser.refresh_form` performs the fetch,
form matching and reapplication in one call.  It always issues a new GET and
never re-sends an old submission request.

If an edited control was deleted, changed type, or no longer offers one of the
selected options, the refresh fails loudly instead of silently filling the
wrong value.  The exception objects expose both the old control and value and
the new control so the conflict can be inspected and resolved.  On conflict the
new form is left untouched.

.. autoclass:: mechanize.FormRefreshError
    :members:

.. autoclass:: mechanize.ControlConflictError
    :members:

.. autoclass:: mechanize.ItemConflictError
    :members:

