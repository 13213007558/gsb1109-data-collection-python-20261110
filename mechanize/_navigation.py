"""Unified navigation policy for restricting a browser to approved origins.

A robot is only supposed to operate inside the sites its caller has approved,
but HTTP redirects, ``Refresh`` / ``http-equiv`` refresh headers and ordinary
page links can all take it elsewhere.  This module provides a single policy
interface that is consulted *before* a request is made, regardless of which of
those three mechanisms produced the request.

The policy deliberately does no network address scanning and does not assume
that subdomains of an approved host are trustworthy: an origin is matched
exactly (scheme, host and port).

When no policy is installed, every navigation is allowed, which preserves the
historical behaviour of mechanize.

"""

from __future__ import absolute_import

import time
from collections import namedtuple

from . import _rfc3986
from .polyglot import URLError

# How a navigation was triggered.  Callers may use these constants to apply
# different rules to different navigation mechanisms.
DIRECT = "direct"
LINK = "link"
REDIRECT = "redirect"
REFRESH = "refresh"


class NavigationDenied(URLError):
    """Raised before a request when the navigation policy refuses it.

    The request that would have left the approved set of origins is never
    sent.  :attr:`record` describes the refused navigation and is also kept in
    the user agent's ``blocked_navigations`` log.

    """

    def __init__(self, record):
        URLError.__init__(self, str(record))
        self.record = record


# A single refused navigation, kept for troubleshooting.
BlockedNavigation = namedtuple(
    "BlockedNavigation",
    ["target_url", "target_origin", "source_type", "source_url",
     "source_origin", "reason", "time"])


def origin_of(url):
    """Return the origin (``scheme://host[:port]``) of ``url``, or ``None``.

    The host part is lower-cased so that origins compare independently of the
    casing used in a URL.  A relative reference or an unparseable URL has no
    origin and yields ``None``.

    """
    if url is None:
        return None
    try:
        scheme, authority = _rfc3986.urlsplit(url)[:2]
    except (TypeError, ValueError):
        return None
    if scheme is None or authority is None:
        return None
    return "%s://%s" % (scheme.lower(), authority.lower())


class BaseNavigationPolicy(object):
    """Abstract navigation policy.

    Subclasses implement :meth:`allow`, which is given the target origin, the
    mechanism that triggered the navigation (one of :data:`DIRECT`,
    :data:`LINK`, :data:`REDIRECT` or :data:`REFRESH`) and the URL/origin the
    navigation originates from.  It returns a ``(allowed, reason)`` pair.

    The base implementation denies everything, so an installed policy fails
    closed until a subclass explicitly allows an origin.

    """

    def allow(self, target_origin, target_url, source_type,
              source_origin=None, source_url=None):
        return False, "navigation denied by default-deny policy"


class OriginNavigationPolicy(BaseNavigationPolicy):
    """Allow navigation only to an explicit, exact set of origins.

    ``allowed_origins`` is a sequence of origins (``scheme://host[:port]``) or
    full URLs whose origins are extracted.  Matching is exact: approving
    ``http://example.com`` does not approve ``http://www.example.com`` or
    ``https://example.com``.  No subdomain guessing and no address scanning is
    performed.

    A target whose origin cannot be determined is denied.

    """

    def __init__(self, allowed_origins):
        origins = set()
        for value in allowed_origins:
            origin = origin_of(value)
            if origin is None:
                raise ValueError(
                    "could not determine an origin from %r" % (value,))
            origins.add(origin)
        self.allowed_origins = frozenset(origins)

    def allow(self, target_origin, target_url, source_type,
              source_origin=None, source_url=None):
        if target_origin is None:
            return False, "target URL has no origin"
        if target_origin in self.allowed_origins:
            return True, None
        return False, (
            "origin %s is not in the approved set %s"
            % (target_origin, sorted(self.allowed_origins)))

    def __repr__(self):
        return "OriginNavigationPolicy(%r)" % (sorted(self.allowed_origins),)


# Attributes used to tag a Request with the mechanism that produced it.  The
# opener reads these before sending the request.
SOURCE_TYPE_ATTR = "_nav_source_type"
SOURCE_URL_ATTR = "_nav_source_url"


def tag_request(request, source_type, source_url):
    """Record the mechanism (and originating URL) that produced a request."""
    setattr(request, SOURCE_TYPE_ATTR, source_type)
    setattr(request, SOURCE_URL_ATTR, source_url)
    return request


def request_source(request):
    """Return ``(source_type, source_url)`` tagged on a request.

    Untagged requests are treated as direct navigations with no known source.

    """
    return (getattr(request, SOURCE_TYPE_ATTR, DIRECT),
            getattr(request, SOURCE_URL_ATTR, None))


class NavigationGuardMixin(object):
    """Opener mixin that consults the navigation policy before each request.

    The mixin stores the optional policy and a log of refused navigations.
    All checks go through :meth:`check_navigation`, so direct requests,
    links, HTTP redirects and refreshes are enforced identically.  With no
    policy installed (the default) every request is allowed unchanged.

    """

    def set_navigation_policy(self, policy):
        """Install a navigation policy, or pass ``None`` to disable it."""
        self._navigation_policy = policy

    @property
    def navigation_policy(self):
        """The installed policy, or ``None`` when navigation is unrestricted."""
        return getattr(self, "_navigation_policy", None)

    def check_navigation(self, request, source_type=DIRECT, source_url=None):
        """Raise :class:`NavigationDenied` before a disallowed request.

        The request is inspected and either returned untouched or, when the
        policy refuses it, a :class:`BlockedNavigation` record is appended to
        :attr:`blocked_navigations` and the exception is raised.  No network
        access happens here.

        """
        policy = getattr(self, "_navigation_policy", None)
        if policy is None:
            return request
        target_url = request.get_full_url()
        target_origin = origin_of(target_url)
        source_origin = origin_of(source_url)
        allowed, reason = policy.allow(
            target_origin, target_url, source_type,
            source_origin, source_url)
        if allowed:
            return request
        record = BlockedNavigation(
            target_url=target_url,
            target_origin=target_origin,
            source_type=source_type,
            source_url=source_url,
            source_origin=source_origin,
            reason=reason,
            time=time.time())
        self.blocked_navigations.append(record)
        raise NavigationDenied(record)
