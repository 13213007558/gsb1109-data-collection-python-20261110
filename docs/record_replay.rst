Recording and offline replay
================================

mechanize can optionally record the HTTP interactions of a browsing session
and replay them later with no network access.  This is useful when you need a
colleague to reproduce a form-driven flow (for example a login followed by a
query) but must not hand over your account credentials.

A recording is a plain JSON file called a *cassette*.  It keeps, in order:

* the request method, URL, headers and body of every request;
* the response status code, message, headers and body of every page,
  including HTML form pages;
* binary bodies (stored as base64) and repeated form fields.

Sensitive data never reaches the cassette.  A
:class:`mechanize.RedactionPolicy` replaces cookie values, authentication
headers and the values of chosen form fields with placeholders such as
``{{MECH_FIELD_PASSWORD}}``.  A given value is always mapped to the same
placeholder within one session, so the association between, for example, the
cookie set at login and the cookie sent on later requests is preserved.

Recording a session
--------------------

.. code-block:: python

    import mechanize

    br = mechanize.Browser()
    br.set_handle_robots(False)

    policy = mechanize.RedactionPolicy(
        redact_fields=["username", "password"])
    session = br.set_recorder(policy=policy)

    br.open("https://app.example.test/login")
    br.select_form(nr=0)
    br["username"] = "alice"
    br["password"] = "real-secret-password"
    br.submit()                       # login, then follow the redirect
    br.open("https://app.example.test/query?q=order+123")

    br.save_recording("flow.json")
    br.stop_recording()

The resulting ``flow.json`` contains the full page sequence, but neither the
real password, the ``Authorization`` header nor any session cookie value.
You can now share ``flow.json`` freely.

Replaying offline
-----------------

Replay removes the real HTTP and HTTPS handlers, so the browser can only
answer from the cassette: it never opens a socket and never returns an
unrelated response.

.. code-block:: python

    import mechanize

    br = mechanize.Browser()
    br.set_handle_robots(False)
    br.set_replay("flow.json")

    br.open("https://app.example.test/login")
    br.select_form(nr=0)
    # Any value works for redacted fields: they match by field name.
    br["username"] = "replace-me"
    br["password"] = "replace-me"
    br.submit()
    print(br.geturl())                # same landing page as the recording
    print(br.response().read())       # the same query results page
    br.assert_replay_complete()       # every recorded step was replayed

Request matching and mismatches
-------------------------------

Requests are matched strictly in recording order.  Redacted fields and
headers are compared by name (their placeholder value), while everything else
- URL, method, ordinary headers and non-redacted fields - must match exactly.
If an incoming request does not match the next recorded one, mechanize raises
:class:`mechanize.RequestMismatchError` rather than guessing; the exception
exposes an ``index`` and a human readable ``diff``.  Asking for a response
after the cassette is exhausted raises the same error with
``reason="cassette exhausted"``.

Redaction policy reference
--------------------------

:class:`mechanize.RedactionPolicy` accepts:

* ``redact_fields``: form/query field names whose values are secret;
* ``redact_headers``: extra request header names to replace;
* ``redact_cookies`` (default ``True``): replace ``Cookie`` and
  ``Set-Cookie`` values, keeping cookie names and attributes;
* ``redact_auth`` (default ``True``): replace ``Authorization`` and
  ``Proxy-Authorization``.

Sessions can also be created without a :class:`mechanize.Browser` using
:func:`mechanize.new_recording_browser` and
:func:`mechanize.new_replay_browser`, and the lower-level
:class:`mechanize.RecordHandler`, :class:`mechanize.ReplayHandler` and
:class:`mechanize.RecordingSession` classes are available for custom opener
setups.
