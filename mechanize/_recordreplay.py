"""Optional interaction recording and offline replay for mechanize.

The pieces in this module make it possible to record a sequence of real HTTP
interactions (for example a local "log in then run a query" flow), hand the
recording to a colleague, and replay the exact same page transitions without
any network access and without ever sharing the original credentials.

Two :class:`~mechanize._urllib2_fork.BaseHandler` subclasses do the work:

* :class:`RecordHandler` observes the request/response chain, remembers the
  order of requests together with the response status and body of every page,
  and applies a :class:`RedactionPolicy` so that cookies, authentication
  headers and chosen form fields are replaced by session-scoped placeholders.
* :class:`ReplayHandler` answers recorded requests from memory.  Requests are
  matched strictly in order: if an incoming request does not match the next
  recorded one, a :class:`RequestMismatchError` carrying a textual diff is
  raised.  It never falls through to a real socket and never hands back an
  unrelated response.

The recorded material is plain JSON (a "cassette") so it can be inspected,
checked in to a ticket and exchanged safely.
"""

from __future__ import absolute_import

import base64
import copy
import json
import re
from datetime import datetime, timezone
from io import BytesIO

from ._headersutil import normalize_header_name
from ._response import closeable_response, make_headers
from ._urllib2_fork import BaseHandler

try:  # pragma: no cover - import shim for the supported Python runtimes
    from urllib.parse import parse_qsl, urlsplit, urlunsplit, urlencode
except ImportError:  # pragma: no cover
    from urlparse import parse_qsl, urlsplit, urlunsplit
    from urllib import urlencode


CASSETTE_FORMAT_VERSION = 1

#: Headers that always carry credentials.  They are redacted whenever
#: ``redact_auth`` is enabled (the default).
DEFAULT_AUTH_HEADERS = (
    "Authorization",
    "Proxy-Authorization",
)

#: Placeholder token kinds, used both for memoisation and for display.
_FIELD = "FIELD"
_COOKIE = "COOKIE"
_HEADER = "HEADER"

#: Request headers derived from the body or the transport.  Their values are a
#: function of data that is itself redacted (Content-Length), are fully
#: determined by the URL (Host), or are not stable across runs/transports, so
#: comparing them would produce false mismatches.
_IGNORED_REQUEST_HEADERS = frozenset((
    "content-length", "connection", "host", "transfer-encoding"))


class RequestMismatchError(Exception):
    """Raised when a replay request does not match the next recorded request.

    The exception deliberately carries a readable :attr:`diff` instead of
    silently returning another recorded response or opening a network
    connection.

    Public attributes:

    index: zero-based position in the cassette that failed to match
    expected: recorded request fingerprint
    actual: fingerprint of the request seen during replay
    diff: list of human readable difference descriptions
    """

    def __init__(self, index, expected, actual, diff, reason=None):
        self.index = index
        self.expected = expected
        self.actual = actual
        self.diff = list(diff)
        self.reason = reason
        header = "request %d does not match recording" % index
        if reason:
            header += " (%s)" % reason
        message = header + ":\n  " + "\n  ".join(self.diff)
        Exception.__init__(self, message)


class RedactionPolicy(object):
    """Describe which sensitive values must be replaced before export.

    :param redact_fields: form/query field names whose values are secret
        (e.g. ``["username", "password"]``).  Matching field values are
        replaced in both urlencoded request bodies and URL query strings.
    :param redact_headers: additional request header names to replace.
        Authentication headers are always covered when ``redact_auth`` is on.
    :param redact_cookies: replace the values of request ``Cookie`` and
        response ``Set-Cookie`` headers, preserving the cookie names and
        attributes.
    :param redact_auth: replace ``Authorization`` / ``Proxy-Authorization``.
    """

    def __init__(self, redact_fields=None, redact_headers=None,
                 redact_cookies=True, redact_auth=True):
        self.redact_fields = tuple(redact_fields or ())
        self.redact_headers = tuple(
            normalize_header_name(name) for name in (redact_headers or ()))
        self.redact_cookies = bool(redact_cookies)
        self.redact_auth = bool(redact_auth)

    def redacted_header_names(self):
        """Return the normalized set of request headers to replace."""
        names = set(self.redact_headers)
        if self.redact_auth:
            names.update(normalize_header_name(name)
                         for name in DEFAULT_AUTH_HEADERS)
        if self.redact_cookies:
            names.add(normalize_header_name("Cookie"))
        return names

    def to_dict(self):
        return {
            "redact_fields": list(self.redact_fields),
            "redact_headers": list(self.redact_headers),
            "redact_cookies": self.redact_cookies,
            "redact_auth": self.redact_auth,
        }

    @classmethod
    def from_dict(cls, data):
        if not data:
            return cls()
        return cls(
            redact_fields=data.get("redact_fields"),
            redact_headers=data.get("redact_headers"),
            redact_cookies=data.get("redact_cookies", True),
            redact_auth=data.get("redact_auth", True),
        )


class _Vault(object):
    """Memoise placeholder tokens so a value stays associated for a session.

    Token generation is deterministic from the redacted item name, which
    guarantees that the same sensitive item (a form field, a cookie or an
    authentication header) is represented by the very same placeholder
    everywhere it appears in one session.
    """

    def __init__(self, policy):
        self.policy = policy
        self._tokens = {}

    @staticmethod
    def _slug(name):
        return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "VALUE"

    def token(self, kind, name):
        key = (kind, name)
        value = self._tokens.get(key)
        if value is None:
            value = "{{MECH_%s_%s}}" % (kind, self._slug(name))
            self._tokens[key] = value
        return value


def _is_form_urlencoded(request):
    content_type = request.get_header("Content-type", "") or ""
    return content_type.split(";", 1)[0].strip().lower() == \
        "application/x-www-form-urlencoded"


def _redact_field_pairs(pairs, policy, vault):
    redacted = []
    sensitive = set(policy.redact_fields)
    for name, value in pairs:
        if name in sensitive:
            value = vault.token(_FIELD, name)
        redacted.append([name, value])
    return redacted


def _redact_url(url, policy, vault):
    scheme, netloc, path, query, fragment = urlsplit(url)
    if not query:
        return url
    pairs = parse_qsl(query, keep_blank_values=True)
    redacted = _redact_field_pairs(pairs, policy, vault)
    new_query = urlencode([(name, value) for name, value in redacted])
    return urlunsplit((scheme, netloc, path, new_query, fragment))


def _redact_cookie_header(value, vault):
    # "sessionid=secret; theme=dark" -> keep names, replace values.
    parts = []
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, _sep, _value = chunk.partition("=")
            parts.append(name.strip() + "=" + vault.token(_COOKIE,
                                                           name.strip()))
        else:
            parts.append(chunk)
    return "; ".join(parts)


def _redact_set_cookie(value, vault):
    # Only the first name/value pair is the cookie value; attributes survive.
    head, _sep, tail = value.partition(";")
    name, sep, _value = head.partition("=")
    redacted_head = name + sep + vault.token(_COOKIE, name.strip())
    return redacted_head + (_sep + tail if _sep else "")


def _redact_request_headers(header_pairs, policy, vault):
    redact_all = policy.redacted_header_names()
    result = []
    for name, value in header_pairs:
        normalized = normalize_header_name(name)
        low = normalized.lower()
        if low in _IGNORED_REQUEST_HEADERS:
            continue
        if policy.redact_cookies and low == "cookie":
            value = _redact_cookie_header(value, vault)
        elif normalized in redact_all:
            value = vault.token(_HEADER, normalized)
        result.append([normalized, value])
    result.sort(key=lambda pair: pair[0].lower())
    return result


def _encode_request_body(request, policy, vault):
    data = request.data
    if data is None:
        return {"kind": "empty"}
    if isinstance(data, str):
        data = data.encode("iso-8859-1")
    if _is_form_urlencoded(request):
        text = data.decode("iso-8859-1")
        pairs = parse_qsl(text, keep_blank_values=True)
        return {
            "kind": "fields",
            # Repeated field names are preserved element by element.
            "fields": _redact_field_pairs(pairs, policy, vault),
        }
    return _binary_body(data)


def _binary_body(data):
    return {
        "kind": "bytes",
        "encoding": "base64",
        "length": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


def encode_body(data):
    """Encode a response/request body for cassette storage.

    Text is stored verbatim so HTML form pages remain readable in the JSON;
    binary content is stored as base64.
    """
    if data is None or len(data) == 0:
        return {"kind": "empty"}
    if b"\x00" not in data:
        try:
            return {"kind": "text", "data": data.decode("utf-8")}
        except UnicodeDecodeError:
            pass
    return _binary_body(data)


def decode_body(body):
    kind = body.get("kind", "empty")
    if kind == "empty":
        return b""
    if kind == "text":
        return body["data"].encode("utf-8")
    if kind == "base64" or kind == "bytes":
        return base64.b64decode(body["data"])
    raise ValueError("unknown body kind: %r" % kind)


def build_request_fingerprint(request, policy, vault):
    """Return a JSON-serialisable, redacted description of a request."""
    return {
        "method": request.get_method(),
        "url": _redact_url(request.get_full_url(), policy, vault),
        "headers": _redact_request_headers(request.header_items(), policy,
                                           vault),
        "body": _encode_request_body(request, policy, vault),
    }


def apply_default_headers(parent, request):
    """Apply opener-level default headers the way the real transport would.

    ``AbstractHTTPHandler.do_request_`` copies ``parent.addheaders`` onto a
    request just before sending it.  Replay has no real transport, so the same
    step is reproduced here (and on the recording side) to keep request
    fingerprints identical whichever handler ends up talking to the network.
    """
    addheaders = getattr(parent, "addheaders", None)
    if addheaders:
        for name, value in addheaders:
            normalized = normalize_header_name(name)
            if not request.has_header(normalized):
                request.add_unredirected_header(normalized, value)
    return request


def _redact_response_headers(header_pairs, policy, vault):
    redact_all = policy.redacted_header_names()
    result = []
    for name, value in header_pairs:
        normalized = normalize_header_name(name)
        low = normalized.lower()
        if policy.redact_cookies and low == "set-cookie":
            value = _redact_set_cookie(value, vault)
        elif normalized in redact_all:
            value = vault.token(_HEADER, normalized)
        result.append([normalized, value])
    return result


def _header_map(headers):
    # Preserve a display name while indexing case-insensitively.
    mapping = {}
    for name, value in headers:
        bucket = mapping.setdefault(name.lower(), [name, []])
        bucket[1].append(value)
    return mapping


def diff_fingerprints(expected, actual):
    """Return a list of textual differences between two fingerprints."""
    lines = []
    for scalar in ("method", "url"):
        if expected.get(scalar) != actual.get(scalar):
            lines.append("%s: expected %r, got %r" %
                         (scalar, expected.get(scalar), actual.get(scalar)))

    expected_headers = _header_map(expected.get("headers", []))
    actual_headers = _header_map(actual.get("headers", []))
    for key in sorted(set(expected_headers) | set(actual_headers)):
        if key not in actual_headers:
            lines.append("header %r missing from request" %
                         expected_headers[key][0])
        elif key not in expected_headers:
            lines.append("unexpected header %r in request" %
                         actual_headers[key][0])
        else:
            exp_name, exp_values = expected_headers[key]
            _act_name, act_values = actual_headers[key]
            if exp_values != act_values:
                lines.append("header %r: expected %r, got %r" %
                             (exp_name, exp_values, act_values))

    expected_body = expected.get("body", {"kind": "empty"})
    actual_body = actual.get("body", {"kind": "empty"})
    if expected_body.get("kind") != actual_body.get("kind"):
        lines.append("body kind: expected %r, got %r" %
                     (expected_body.get("kind"), actual_body.get("kind")))
    elif expected_body["kind"] == "fields":
        exp_fields = expected_body.get("fields", [])
        act_fields = actual_body.get("fields", [])
        if len(exp_fields) != len(act_fields):
            lines.append("field count: expected %d, got %d" %
                         (len(exp_fields), len(act_fields)))
        for position, (exp_pair, act_pair) in enumerate(
                zip(exp_fields, act_fields)):
            if exp_pair != act_pair:
                lines.append(
                    "field #%d: expected %r, got %r" %
                    (position, exp_pair, act_pair))
    elif expected_body["kind"] in ("bytes", "base64"):
        if expected_body.get("length") != actual_body.get("length"):
            lines.append("body length: expected %d, got %d" %
                         (expected_body.get("length", 0),
                          actual_body.get("length", 0)))
    return lines


class RecordingSession(object):
    """Holds redaction configuration and the ordered recorded entries."""

    def __init__(self, policy=None):
        self.policy = policy or RedactionPolicy()
        self.vault = _Vault(self.policy)
        self.entries = []
        self.created_at = datetime.now(timezone.utc).isoformat()

    def add(self, request, response):
        body = response.read()
        # Make the (possibly non-seekable) response re-readable for the rest
        # of the handler chain after we consumed it.
        if hasattr(response, "_set_fp"):
            response._set_fp(BytesIO(body))
        elif hasattr(response, "set_data"):
            response.set_data(body)
        fingerprint = build_request_fingerprint(request, self.policy,
                                                 self.vault)
        entry = {
            "index": len(self.entries),
            "request": fingerprint,
            "response": {
                "url": response.geturl(),
                "code": getattr(response, "code", 200),
                "msg": getattr(response, "msg", "OK"),
                "headers": _redact_response_headers(
                    list(response.info().items()), self.policy, self.vault),
                "body": encode_body(body),
            },
        }
        self.entries.append(entry)
        return entry

    def to_dict(self):
        return {
            "format_version": CASSETTE_FORMAT_VERSION,
            "created_at": self.created_at,
            "redaction": self.policy.to_dict(),
            "entries": copy.deepcopy(self.entries),
        }

    def save(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, ensure_ascii=False)
        return path

    @classmethod
    def from_dict(cls, data):
        policy = RedactionPolicy.from_dict(data.get("redaction"))
        session = cls(policy=policy)
        session.created_at = data.get("created_at", session.created_at)
        session.entries = copy.deepcopy(data.get("entries", []))
        return session

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def with_policy(self, policy):
        session = RecordingSession(policy=policy)
        session.created_at = self.created_at
        session.entries = copy.deepcopy(self.entries)
        return session

    def assert_fully_played(self, consumed):
        if consumed != len(self.entries):
            raise AssertionError(
                "only %d of %d recorded interactions were replayed" %
                (consumed, len(self.entries)))


class RecordHandler(BaseHandler):
    """Record (redacted) request/response pairs while browsing normally."""

    # Run before the cookie/gzip/error processors so we observe the transport
    # request and the raw, still-encoded response body.
    handler_order = 50

    def __init__(self, session=None, policy=None):
        self.session = session or RecordingSession(policy=policy)

    def __copy__(self):
        # Clones keep recording into the same shared session.
        return self.__class__(self.session)

    def http_request(self, request):
        apply_default_headers(getattr(self, "parent", None), request)
        return request

    def http_response(self, request, response):
        self.session.add(request, response)
        return response

    https_request = http_request
    https_response = http_response


class ReplayHandler(BaseHandler):
    """Serve recorded responses in order, refusing unmatched requests."""

    # Replace the transport entirely: this handler sits ahead of the real
    # HTTP(S) handlers and returns a recorded response, so no socket is ever
    # opened and no unrelated response can ever be returned.
    handler_order = 50

    def __init__(self, session):
        self.session = session
        self.position = 0
        self._vault = _Vault(session.policy)

    def __copy__(self):
        # A clone replays the same cassette from the beginning.
        return self.__class__(self.session)

    @property
    def consumed(self):
        return self.position

    def _replay_response(self, entry):
        recorded = entry["response"]
        body = decode_body(recorded["body"])
        headers = make_headers([(name, value)
                                for name, value in recorded["headers"]])
        return closeable_response(
            BytesIO(body), headers, recorded["url"],
            code=recorded["code"], msg=recorded["msg"])

    def _serve(self, request):
        apply_default_headers(getattr(self, "parent", None), request)
        if self.position >= len(self.session.entries):
            fingerprint = build_request_fingerprint(
                request, self.session.policy, self._vault)
            raise RequestMismatchError(
                self.position,
                {"note": "no further recorded responses"},
                fingerprint,
                ["cassette exhausted: no recorded response remains for "
                 "%s %s" % (request.get_method(), request.get_full_url())],
                reason="cassette exhausted")
        entry = self.session.entries[self.position]
        expected = entry["request"]
        actual = build_request_fingerprint(request, self.session.policy,
                                           self._vault)
        if actual != expected:
            raise RequestMismatchError(
                self.position, expected, actual,
                diff_fingerprints(expected, actual))
        self.position += 1
        return self._replay_response(entry)

    def http_open(self, request):
        return self._serve(request)

    https_open = http_open


def new_recording_browser(policy=None, **browser_kwds):
    """Create a :class:`~mechanize.Browser` wired up for recording.

    Returns ``(browser, session)``.
    """
    from ._mechanize import Browser
    browser = Browser(**browser_kwds)
    session = browser.set_recorder(policy=policy)
    return browser, session


def new_replay_browser(source, policy=None, **browser_kwds):
    """Create a :class:`~mechanize.Browser` that replays a saved cassette.

    ``source`` is either a cassette path or a :class:`RecordingSession`.
    """
    from ._mechanize import Browser
    browser = Browser(**browser_kwds)
    browser.set_replay(source, policy=policy)
    return browser
