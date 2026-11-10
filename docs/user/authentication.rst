.. _authentication:

Authentication
==============

This document discusses using various kinds of authentication with Requests.

Many web services require authentication, and there are many different types.
Below, we outline various forms of authentication available in Requests, from
the simple to the complex.


Basic Authentication
--------------------

Many web services that require authentication accept HTTP Basic Auth. This is
the simplest kind, and Requests supports it straight out of the box.

Making requests with HTTP Basic Auth is very simple::

    >>> from requests.auth import HTTPBasicAuth
    >>> basic = HTTPBasicAuth('user', 'pass')
    >>> requests.get('https://httpbin.org/basic-auth/user/pass', auth=basic)
    <Response [200]>

In fact, HTTP Basic Auth is so common that Requests provides a handy shorthand
for using it::

    >>> requests.get('https://httpbin.org/basic-auth/user/pass', auth=('user', 'pass'))
    <Response [200]>

Providing the credentials in a tuple like this is exactly the same as the
``HTTPBasicAuth`` example above.


netrc Authentication
~~~~~~~~~~~~~~~~~~~~

If no authentication method is given with the ``auth`` argument, Requests will
attempt to get the authentication credentials for the URL's hostname from the
user's netrc file. The netrc file overrides raw HTTP authentication headers
set with `headers=`.

If credentials for the hostname are found, the request is sent with HTTP Basic
Auth.

Requests will search for the netrc file at `~/.netrc`, `~/_netrc`, or at the path
specified by the `NETRC` environment variable. `~` denotes the user's home
directory, which is `$HOME` on Unix based systems and `%USERPROFILE%` on Windows.

Usage of netrc file can be disabled by setting `trust_env` to `False` in the
Requests session::

    >>> s = requests.Session()
    >>> s.trust_env = False
    >>> s.get('https://httpbin.org/basic-auth/user/pass')

Digest Authentication
---------------------

Another very popular form of HTTP Authentication is Digest Authentication,
and Requests supports this out of the box as well::

    >>> from requests.auth import HTTPDigestAuth
    >>> url = 'https://httpbin.org/digest-auth/auth/user/pass'
    >>> requests.get(url, auth=HTTPDigestAuth('user', 'pass'))
    <Response [200]>


OAuth 1 Authentication
----------------------

A common form of authentication for several web APIs is OAuth. The ``requests-oauthlib``
library allows Requests users to easily make OAuth 1 authenticated requests::

    >>> import requests
    >>> from requests_oauthlib import OAuth1

    >>> url = 'https://api.twitter.com/1.1/account/verify_credentials.json'
    >>> auth = OAuth1('YOUR_APP_KEY', 'YOUR_APP_SECRET',
    ...               'USER_OAUTH_TOKEN', 'USER_OAUTH_TOKEN_SECRET')

    >>> requests.get(url, auth=auth)
    <Response [200]>

For more information on how to OAuth flow works, please see the official `OAuth`_ website.
For examples and documentation on requests-oauthlib, please see the `requests_oauthlib`_
repository on GitHub

Request Signing
---------------

Some vendors require a signature over the exact request being sent: the final
request target, a chosen set of headers and a digest of the body. Computing
that signature *before* preparing the request is unreliable, because
preparation percent-encodes the URL, merges headers and encodes the body, so
the signed bytes no longer match the transmitted bytes.

Requests lets you plug in a ``signer`` callable that runs at the end of
request preparation, once the exact wire representation is settled::

    import hashlib
    import hmac
    import requests

    secret = b"shared-secret"

    def sign(signature_input):
        canonical = b"\n".join([
            signature_input["method"].encode(),
            signature_input["path_url"].encode(),
            signature_input["body_digest"] or b"",
        ])
        signature = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
        return {"X-Signature": signature}

    requests.post(
        "https://api.example.com/items",
        params=[("a", "1"), ("a", "2")],
        data={"key": "value with spaces"},
        signer=sign,
    )

The ``signature_input`` mapping contains ``method``, ``url``, ``path_url``,
the selected ``headers``, the exact in-memory ``body`` bytes (or ``None`` for
streams) and a SHA-256 ``body_digest``. Because it is captured after all
encoding, it always corresponds to what will actually be transmitted.

A signer may be attached to a whole :class:`~requests.Session` through
``session.signer``; a per-request ``signer`` overrides it. The signature is
recomputed whenever the request is prepared again, so redirects, retries and
manual re-preparation never reuse a stale signature.

Signing a body that can only be read once (a generator or other non-seekable
stream) would consume the bytes before they could be sent. Requests refuses
these with :class:`~requests.exceptions.UnsignedBodyError`. Seekable
file-like bodies are digested automatically and rewound; for genuinely
one-shot, fixed-length streams you can instead pass a precomputed digest in an
``X-Requests-Content-Digest`` header, which is consumed during signing and
never transmitted.

Signing errors never include signature material in their message text, so
secrets cannot leak into logs through an exception.

OAuth 2 and OpenID Connect Authentication
-----------------------------------------

The ``requests-oauthlib`` library also handles OAuth 2, the authentication mechanism
underpinning OpenID Connect. See the `requests-oauthlib OAuth2 documentation`_ for
details of the various OAuth 2 credential management flows:

* `Web Application Flow`_
* `Mobile Application Flow`_
* `Legacy Application Flow`_
* `Backend Application Flow`_

Other Authentication
--------------------

Requests is designed to allow other forms of authentication to be easily and
quickly plugged in. Members of the open-source community frequently write
authentication handlers for more complicated or less commonly-used forms of
authentication. Some of the best have been brought together under the
`Requests organization`_, including:

- Kerberos_
- NTLM_

If you want to use any of these forms of authentication, go straight to their
GitHub page and follow the instructions.


New Forms of Authentication
---------------------------

If you can't find a good implementation of the form of authentication you
want, you can implement it yourself. Requests makes it easy to add your own
forms of authentication.

To do so, subclass :class:`AuthBase <requests.auth.AuthBase>` and implement the
``__call__()`` method::

    >>> import requests
    >>> class MyAuth(requests.auth.AuthBase):
    ...     def __call__(self, r):
    ...         # Implement my authentication
    ...         return r
    ...
    >>> url = 'https://httpbin.org/get'
    >>> requests.get(url, auth=MyAuth())
    <Response [200]>

When an authentication handler is attached to a request,
it is called during request setup. The ``__call__`` method must therefore do
whatever is required to make the authentication work. Some forms of
authentication will additionally add hooks to provide further functionality.

Further examples can be found under the `Requests organization`_ and in the
``auth.py`` file.

.. _OAuth: https://oauth.net/
.. _requests_oauthlib: https://github.com/requests/requests-oauthlib
.. _requests-oauthlib OAuth2 documentation: https://requests-oauthlib.readthedocs.io/en/latest/oauth2_workflow.html
.. _Web Application Flow: https://requests-oauthlib.readthedocs.io/en/latest/oauth2_workflow.html#web-application-flow
.. _Mobile Application Flow: https://requests-oauthlib.readthedocs.io/en/latest/oauth2_workflow.html#mobile-application-flow
.. _Legacy Application Flow: https://requests-oauthlib.readthedocs.io/en/latest/oauth2_workflow.html#legacy-application-flow
.. _Backend Application Flow: https://requests-oauthlib.readthedocs.io/en/latest/oauth2_workflow.html#backend-application-flow
.. _Kerberos: https://github.com/requests/requests-kerberos
.. _NTLM: https://github.com/requests/requests-ntlm
.. _Requests organization: https://github.com/requests
