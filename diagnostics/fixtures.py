"""
Local, offline response fixtures for the Wayback error-classification review.

Nothing here performs real network I/O. Every fixture is a plain
``requests.Response``-shaped object (or a requests/urllib3 exception) served by
:class:`requests_mock.Mocker` or raised from a patched session. The bodies and
headers are modelled on the project's own VCR cassettes in
``src/wayback/tests/cassettes/``.

Each scenario deliberately varies *one* signal at a time -- HTTP status code,
response body marker, or the ``X-Archive-Wayback-Runtime-Error`` header -- so
the diagnostic runner can show which signal actually drives the library's
conclusion.
"""

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3.exceptions import (
    ConnectTimeoutError,
    MaxRetryError,
    ReadTimeoutError,
)

TIMESTAMP = '20170929002712'
TARGET_URL = 'https://example.gov/section/page'
MEMENTO_URL = f'https://web.archive.org/web/{TIMESTAMP}id_/{TARGET_URL}'
CDX_URL = 'https://web.archive.org/cdx/search/cdx'

RUNTIME_HEADER = 'X-Archive-Wayback-Runtime-Error'

# Real marker strings, copied verbatim from the shipped VCR cassettes.
ADMIN_EXCEPTION = (
    'org.archive.util.io.RuntimeIOException: '
    'org.archive.wayback.exception.AdministrativeAccessControlException: Blocked Site Error'
)
ROBOT_EXCEPTION = (
    'org.archive.wayback.exception.RobotAccessControlException: '
    'Blocked By Robots Error'
)
EXCLUDED_BODY = (
    '<html><body><h1>Wayback Machine</h1>'
    '<p>This URL has been excluded from the Wayback Machine.</p></body></html>'
)
ROBOTS_BODY = (
    '<html><body><h1>Wayback Machine</h1>'
    '<p>This URL is blocked because of the site\'s robots.txt file.</p></body></html>'
)
GENERIC_NOT_FOUND_BODY = (
    '<html><body><h1>404 Not Found</h1>'
    '<p>The Wayback Machine has not archived that URL.</p></body></html>'
)
GENERIC_503_BODY = (
    '<html><body><h1>503 Service Unavailable</h1>'
    '<p>The Wayback Machine is temporarily unavailable.</p></body></html>'
)
PLAYBACK_RUNTIME_ERROR = (
    'org.archive.wayback.exception.ResourceNotAvailableException: '
    'Resource is not available'
)


def make_response(status_code, body='', headers=None, url=MEMENTO_URL, location=None):
    """Build a requests.Response the same way requests_mock would."""
    response = requests.Response()
    response.status_code = status_code
    if isinstance(body, str):
        body = body.encode('utf-8')
    response._content = body or b''
    response.url = url
    response.headers.update(headers or {})
    response.encoding = 'utf-8'
    if location is not None:
        response.headers['Location'] = location
    # Populate response.next for redirects, exactly as requests does when
    # allow_redirects=False is in effect.
    if location is not None and 300 <= status_code < 400:
        request = requests.Request('GET', location)
        response._next = requests.Session().prepare_request(request)
    return response


def memento_headers():
    """Headers that mark a response as an actual archived memento."""
    return {
        'Memento-Datetime': 'Fri, 29 Sep 2017 00:27:12 GMT',
        'Content-Type': 'text/html',
    }


# ---------------------------------------------------------------------------
# Memento playback scenarios (drives WaybackClient.get_memento)
# ---------------------------------------------------------------------------

def memento_scenarios():
    """
    Return an ordered list of (id, description, builder, expected_signal_note).

    A builder either returns a requests.Response or raises an exception; the
    diagnostic runner knows how to handle both.
    """
    return [
        # --- Category 1: no snapshot --------------------------------------
        (
            'no_snapshot_plain_404',
            '404 with an empty body and no runtime-error header',
            lambda: make_response(404, ''),
            'status 404 alone -> NoMementoError',
        ),
        (
            'no_snapshot_404_generic_body',
            '404 with a generic "not archived" HTML body, no runtime header',
            lambda: make_response(404, GENERIC_NOT_FOUND_BODY),
            'status 404 takes precedence; body text is not inspected here',
        ),
        (
            'no_snapshot_404_but_excluded_marker_in_body',
            '404 whose body contains the exclusion sentence',
            lambda: make_response(404, EXCLUDED_BODY),
            'precedence: body marker is checked BEFORE the 404 branch -> BlockedSiteError',
        ),

        # --- Category 2: site excluded / takedown -------------------------
        (
            'blocked_site_200_excluded_body',
            '200 (cassette-faithful) body: "This URL has been excluded..."',
            lambda: make_response(200, EXCLUDED_BODY),
            'body marker -> BlockedSiteError even though HTTP status is 200',
        ),
        (
            'blocked_site_403_runtime_header',
            '403 with AdministrativeAccessControlException runtime header',
            lambda: make_response(403, ADMIN_EXCEPTION, {RUNTIME_HEADER: ADMIN_EXCEPTION}),
            'runtime header -> BlockedSiteError (header, not body, drives it)',
        ),
        (
            'blocked_site_403_header_only_empty_body',
            '403 with the runtime header but an empty body',
            lambda: make_response(403, '', {RUNTIME_HEADER: ADMIN_EXCEPTION}),
            'proves the header alone is sufficient; body need not match',
        ),
        (
            'blocked_site_200_excluded_body_no_header',
            '200 exclusion body but NO runtime header (mirrors real cassette)',
            lambda: make_response(200, EXCLUDED_BODY),
            'body-only detection; depends on a fixed English sentence',
        ),

        # --- Category 3: single resource restricted by robots.txt ---------
        (
            'robots_403_runtime_header',
            '403 with RobotAccessControlException runtime header',
            lambda: make_response(403, ROBOT_EXCEPTION, {RUNTIME_HEADER: ROBOT_EXCEPTION}),
            'runtime header -> BlockedByRobotsError',
        ),
        (
            'robots_200_body_only',
            '200 body mentioning robots.txt, no runtime header',
            lambda: make_response(200, ROBOTS_BODY),
            'substring "robots.txt" in body -> BlockedByRobotsError',
        ),
        (
            'robots_substring_false_friend',
            '200 benign page that merely mentions robots.txt',
            lambda: make_response(
                200,
                '<html><body>See our robots.txt policy for details.</body></html>',
            ),
            'FALSE FRIEND: any archived content mentioning robots.txt is misclassified',
        ),

        # --- Category 4: transient service errors -------------------------
        (
            'transient_503_no_header',
            '503 with no runtime header and no redirect target',
            lambda: make_response(503, GENERIC_503_BODY),
            'MementoPlaybackError; session layer would have retried this first',
        ),
        (
            'transient_500_no_header',
            '500 with no runtime header',
            lambda: make_response(500, GENERIC_503_BODY),
            'MementoPlaybackError after the session exhausts retries',
        ),
        (
            'transient_503_with_playback_runtime_header',
            '503 with a generic (non-access-control) runtime header',
            lambda: make_response(503, '', {RUNTIME_HEADER: PLAYBACK_RUNTIME_ERROR}),
            'runtime header present but not an access class -> MementoPlaybackError',
        ),
        (
            'transient_connection_reset',
            'connection dropped before any response (RemoteDisconnected family)',
            lambda: (_ for _ in ()).throw(
                RequestsConnectionError(
                    "('Connection aborted.', RemoteDisconnected('Remote end closed "
                    "connection without response'))"
                )
            ),
            'NOT matched by should_retry_error(): re-raised raw on attempt 1, never retried',
        ),
        (
            'transient_connect_timeout',
            'TCP connect timeout before any response',
            lambda: (_ for _ in ()).throw(
                RequestsConnectionError(
                    MaxRetryError(
                        ConnectTimeoutError(None, 'timed out'),
                        MEMENTO_URL,
                    )
                )
            ),
            'should_retry_error() -> retried, then WaybackRetryError',
        ),
        (
            'transient_read_timeout',
            'read timeout while waiting for response headers',
            lambda: (_ for _ in ()).throw(
                RequestsConnectionError(
                    MaxRetryError(
                        ReadTimeoutError(None, MEMENTO_URL, 'read timed out'),
                        MEMENTO_URL,
                    )
                )
            ),
            'retryable error type; WaybackRetryError once budget is spent',
        ),
        (
            'transient_dns_failure',
            'DNS/name resolution failure (NewConnectionError)',
            lambda: (_ for _ in ()).throw(
                RequestsConnectionError(
                    "HTTPSConnectionPool(host='web.archive.org', port=443): "
                    'Max retries exceeded with url: / '
                    "(Caused by NewConnectionError: failed to resolve)"
                )
            ),
            'string-matched as retryable despite being potentially permanent',
        ),

        # --- Category 5: unexpected response shapes -----------------------
        (
            'rate_limit_429_no_memento_header',
            '429 with Retry-After but no Memento-Datetime (live API throttle)',
            lambda: make_response(429, 'too many requests', {'Retry-After': '30'}),
            'raised in the session layer as RateLimitError before classification',
        ),
        (
            'malformed_200_ok_with_no_wayback_signals',
            '200, no Memento-Datetime, no runtime header, plain body',
            lambda: make_response(200, '<html><body>login required</body></html>'),
            'response.ok fallback -> generic MementoPlaybackError',
        ),
        (
            'malformed_403_no_header_no_marker',
            '403 with neither runtime header nor known body marker',
            lambda: make_response(403, 'forbidden'),
            'generic MementoPlaybackError, NOT a block classification',
        ),
        (
            'malformed_404_runtime_header_conflict',
            '404 status but header claims AdministrativeAccessControlException',
            lambda: make_response(404, '', {RUNTIME_HEADER: ADMIN_EXCEPTION}),
            'header wins over status: reported BlockedSiteError, not NoMementoError',
        ),
        (
            'malformed_redirect_loop',
            '302 redirect pointing back at the same memento URL',
            lambda: make_response(302, '', location=MEMENTO_URL),
            'circular-redirect guard -> MementoPlaybackError',
        ),
        (
            'actual_memento_archived_429',
            'Memento-Datetime present with an archived 429 status',
            lambda: make_response(429, 'rate limited', memento_headers()),
            'Memento-Datetime short-circuits: a valid memento, not an API error',
        ),
        (
            'actual_memento_archived_500',
            'Memento-Datetime present with an archived 500 status',
            lambda: make_response(500, 'server error', memento_headers()),
            'archived origin error is data, not a Wayback outage',
        ),
    ]


# ---------------------------------------------------------------------------
# CDX search scenarios (drives WaybackClient.search)
# ---------------------------------------------------------------------------

def search_scenarios():
    """Scenarios for the CDX endpoint, including parse failures and blocks."""
    valid_line = (
        'gov,example)/section/page 20170929002712 https://example.gov/section/page '
        'text/html 200 ABCDEFGHIJKLMNOPQRSTUVWXYZ234567 1234'
    )
    return [
        (
            'search_blocked_admin_403',
            'CDX 403 body containing AdministrativeAccessControlException',
            lambda: make_response(
                403, ADMIN_EXCEPTION, {RUNTIME_HEADER: ADMIN_EXCEPTION}, url=CDX_URL
            ),
            'body substring -> BlockedSiteError (status header alone is not enough)',
        ),
        (
            'search_robots_403',
            'CDX 403 body containing RobotAccessControlException',
            lambda: make_response(
                403, ROBOT_EXCEPTION, {RUNTIME_HEADER: ROBOT_EXCEPTION}, url=CDX_URL
            ),
            'body substring -> BlockedByRobotsError',
        ),
        (
            'search_blocked_marker_only_in_header',
            'CDX 403 with access exception only in the header, empty body',
            lambda: make_response(403, '', {RUNTIME_HEADER: ADMIN_EXCEPTION}, url=CDX_URL),
            'EVIDENCE GAP: search() only inspects the body -> generic WaybackException',
        ),
        (
            'search_blocked_body_only_no_header',
            'CDX 403 with the access exception string only in the body, no header',
            lambda: make_response(403, ADMIN_EXCEPTION, url=CDX_URL),
            'body substring alone is enough -> BlockedSiteError',
        ),
        (
            'search_503_transient',
            'CDX 503 plain error page',
            lambda: make_response(503, GENERIC_503_BODY, url=CDX_URL),
            'generic WaybackException after retries; not classified as transient',
        ),
        (
            'search_malformed_line',
            'CDX 200 with an unparseable line (too few fields)',
            lambda: make_response(200, 'this is not cdx output\n', url=CDX_URL),
            'UnexpectedResponseFormat',
        ),
        (
            'search_robots_marker_inside_data_line',
            'CDX 200 whose malformed line embeds RobotAccessControlException',
            lambda: make_response(
                200, 'something RobotAccessControlException happened\n', url=CDX_URL
            ),
            'parse failure path re-scans the line -> BlockedByRobotsError',
        ),
        (
            'search_empty_body',
            'CDX 200 with a completely empty body',
            lambda: make_response(200, '', url=CDX_URL),
            'empty result set, no error (legitimate zero captures)',
        ),
        (
            'search_valid_line',
            'CDX 200 with one well-formed record',
            lambda: make_response(200, valid_line + '\n', url=CDX_URL),
            'parsed successfully; control case',
        ),
        (
            'search_bad_timestamp_line',
            'CDX 200 whose timestamp cannot be parsed as a date',
            lambda: make_response(
                200,
                'gov,example)/p 99999999999999 https://example.gov/p text/html 200 '
                'DIGEST 10\n',
                url=CDX_URL,
            ),
            'UnexpectedResponseFormat wrapped around the ValueError',
        ),
    ]


__all__ = [
    'MEMENTO_URL',
    'CDX_URL',
    'TARGET_URL',
    'TIMESTAMP',
    'RUNTIME_HEADER',
    'make_response',
    'memento_headers',
    'memento_scenarios',
    'search_scenarios',
]
