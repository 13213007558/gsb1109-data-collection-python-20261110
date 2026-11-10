Advanced topics
==================

.. _threading:

Thread safety
---------------

The global :func:`mechanize.urlopen()` and :func:`mechanize.urlretrieve()` functions are
thread safe. However, mechanize browser instances **are not** thread safe. If
you want to use a mechanize Browser instance in multiple threads, clone it,
using `copy.copy(browser_object)` method. The clone will share the same,
thread safe cookie jar, and have the same settings/handlers as the original,
but all other state is not shared, making the clone safe to use in a different
thread.

Using custom CA certificates
-------------------------------

mechanize supports the same mechanism for using custom CA certificates as
python >= 2.7.9. To change the certificates a mechanize browser instance uses,
call the :meth:`mechanize.Browser.set_ca_data()` method on it.

.. _navigation_policy:

Restricting navigation to approved origins
------------------------------------------

A robot is usually only meant to operate inside a specific set of sites, but
HTTP redirects, ``Refresh`` / ``http-equiv`` refreshes and ordinary page links
can all take the browser somewhere else. A navigation policy lets a caller
approve or refuse each request based on its **target origin**, the **mechanism**
that triggered it and its **source**, before the request is sent.

Call :meth:`mechanize.Browser.set_navigation_policy()` with an
:class:`mechanize.OriginNavigationPolicy` to allow only an explicit, exact set
of origins.  Origin matching is exact: approving ``http://example.com`` does
not approve ``http://www.example.com`` (subdomains are never assumed
trustworthy), ``https://example.com`` (different scheme) or a different port.
The policy performs no network address scanning.

.. code-block:: python

    import mechanize

    browser = mechanize.Browser()
    browser.set_navigation_policy(
        mechanize.OriginNavigationPolicy([
            "http://example.com",
            "https://example.com",
        ]))

The same rule is applied identically no matter how a URL is reached:

* direct calls such as :meth:`~mechanize.Browser.open()`,
  :meth:`~mechanize.Browser.submit()` and
  :meth:`~mechanize.Browser.open_novisit()` (mechanism
  :data:`mechanize.DIRECT`),
* links followed with :meth:`~mechanize.Browser.follow_link()`
  (:data:`mechanize.LINK`),
* HTTP 30x redirections (:data:`mechanize.REDIRECT`),
* ``Refresh`` headers and HTML ``http-equiv`` meta refreshes
  (:data:`mechanize.REFRESH`).

Every check happens before the request is made, so a refused URL is never
fetched.  A refused navigation raises :class:`mechanize.NavigationDenied` (a
subclass of :class:`mechanize.URLError`) whose ``record`` attribute, and the
browser's ``blocked_navigations`` list, hold a
:class:`mechanize.BlockedNavigation` describing the target, the mechanism and
the source for troubleshooting.  The page currently on display is left in
place, including when a redirect or refresh several hops into a navigation is
refused.

For finer control, subclass :class:`mechanize.BaseNavigationPolicy` and
implement ``allow(target_origin, target_url, source_type, source_origin,
source_url)`` returning an ``(allowed, reason)`` pair -- for example to permit
user-driven links while denying automatic redirects.

Pass ``None`` to :meth:`~mechanize.Browser.set_navigation_policy()` to remove
the restriction.  With no policy installed (the default) every navigation is
allowed, preserving the historical behaviour.

.. _debugging:

Debugging
--------------

Hints for debugging programs that use mechanize.

.. _cookies:

Cookies
^^^^^^^^^^

A common mistake is to use :func:`mechanize.urlopen()`, *and* the
`.extract_cookies()` and `.add_cookie_header()` methods on a cookie object
themselves.  If you use `mechanize.urlopen()` (or `OpenerDirector.open()`), the
module handles extraction and adding of cookies by itself, so you should not
call `.extract_cookies()` or `.add_cookie_header()`.

Are you sure the server is sending you any cookies in the first place?  Maybe
the server is keeping track of state in some other way (`HIDDEN` HTML form
entries (possibly in a separate page referenced by a frame), URL-encoded
session keys, IP address, HTTP `Referer` headers)?  Perhaps some embedded
script in the HTML is setting cookies (see below)?  Turn on :ref:`logging`.

When you `.save()` to or `.load()`/`.revert()` from a file, single-session
cookies will expire unless you explicitly request otherwise with the
`ignore_discard` argument.  This may be your problem if you find cookies are
going away after saving and loading.

.. code-block:: python

    import mechanize
    cj = mechanize.LWPCookieJar()
    opener = mechanize.build_opener(mechanize.HTTPCookieProcessor(cj))
    mechanize.install_opener(opener)
    r = mechanize.urlopen("http://foobar.com/")
    cj.save("/some/file", ignore_discard=True, ignore_expires=True)

JavaScript code can set cookies; mechanize does not support this.  See
:ref:`jsfaq`.


General
^^^^^^^^^

Enable :ref:`logging`.

Sometimes, a server wants particular HTTP headers set to the values it expects.
For example, the `User-Agent` header may need to be set
(:meth:`mechanize.Browser.set_header()`) to a value like that of a popular
browser.

Check that the browser is able to do manually what you're trying to achieve
programmatically.  Make sure that what you do manually is *exactly* the same as
what you're trying to do from Python -- you may simply be hitting a server bug
that only gets revealed if you view pages in a particular order, for example.

Try comparing the headers and data that your program sends with those that a
browser sends.  Often this will give you the clue you need.  You can use
the developer tools in any browser to see exactly what the browser sends and
receives.

If nothing is obviously wrong with the requests your program is sending and
you're out of ideas, you can reliably locate the problem by copying the headers
that a browser sends, and then changing headers until your program stops
working again.  Temporarily switch to explicitly sending individual HTTP
headers (by calling `.add_header()`, or by using `httplib` directly).  Start by
sending exactly the headers that Firefox or Chrome send.  You may need to make sure
that a valid session ID is sent -- the one you got from your browser may no
longer be valid.  If that works, you can begin the tedious process of changing
your headers and data until they match what your original code was sending.
You should end up with a minimal set of changes.  If you think that reveals a
bug in mechanize, please report it.


.. _logging:

Logging
^^^^^^^^^

To enable logging to stdout:

.. code-block:: python
    
    import sys, logging
    logger = logging.getLogger("mechanize")
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.DEBUG)

You can reduce the amount of information shown by setting the level to
`logging.INFO` instead of `logging.DEBUG`, or by only enabling logging for one
of the following logger names instead of `"mechanize"`:

  * `"mechanize"`: Everything.

  * `"mechanize.cookies"`: Why particular cookies are accepted or rejected and why
    they are or are not returned.  Requires logging enabled at the `DEBUG` level.

  * `"mechanize.http_responses"`: HTTP response body data.

  * `"mechanize.http_redirects"`: HTTP redirect information.


HTTP headers
^^^^^^^^^^^^^

An example showing how to enable printing of HTTP headers to stdout, logging of
HTTP response bodies, and logging of information about redirections:

.. code-block:: python

    import sys, logging
    import mechanize

    logger = logging.getLogger("mechanize")
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.DEBUG)

    browser = mechanize.Browser()
    browser.set_debug_http(True)
    browser.set_debug_responses(True)
    browser.set_debug_redirects(True)
    response = browser.open("http://python.org/")

Alternatively, you can examine request and response objects to see what's going
on.  Note that requests may involve "sub-requests" in cases such as
redirection, in which case you will not see everything that's going on just by
examining the original request and final response.  
