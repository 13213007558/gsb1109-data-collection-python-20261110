from datetime import datetime, timedelta, timezone
import email.utils
import re
from .._client import CDX_SEARCH_URL
from .._utils import format_timestamp, memento_url_data


UTC = timezone.utc

# A small, entirely local "historical web". Each entry maps an original URL
# to its captures: {wayback_timestamp_string: html_body}.
ROOT_URL = 'http://example.com/'
CAPTURES = {
    ROOT_URL: {
        '20200601120000': """
            <html><body>
              <a href="/a">Article A</a>
              <a href="b">Article B</a>
              <a href="http://other.com/x">External</a>
              <a href="/missing">Never archived</a>
              <a href="/old">Only archived long ago</a>
              <a href="#top">Fragment</a>
            </body></html>""",
    },
    'http://example.com/a': {
        '20200602000000': """
            <html><body>
              <a href="/">Home</a>
              <a href="https://web.archive.org/web/20220601000000/http://example.com/b">
                B (Wayback-rewritten link with an out-of-window timestamp)
              </a>
            </body></html>""",
    },
    'http://example.com/b': {
        '20200531000000': '<html><body>B (in-window capture)</body></html>',
        '20220601000000': '<html><body>B (contemporary capture, off limits)</body></html>',
    },
    'http://example.com/old': {
        '20180101000000': '<html><body>Old</body></html>',
    },
    'http://other.com/x': {
        '20200601000000': '<html><body>External</body></html>',
    },
}

CENTER = datetime(2020, 6, 1, 12, 0, 0, tzinfo=UTC)
WINDOW = timedelta(days=3)
WINDOW_START = CENTER - WINDOW
WINDOW_END = CENTER + WINDOW

MEMENTO_URL_PATTERN = re.compile(r'^https://web\.archive\.org/web/\d+id_/')


def cdx_callback(request, context):
    "Serve CDX records from the local archive, honoring `url`/`from`/`to`."
    query = request.qs
    url = query['url'][0]
    start = query.get('from', [''])[0]
    end = query.get('to', [''])[0]
    lines = []
    for timestamp in sorted(CAPTURES.get(url, {})):
        if (not start or timestamp >= start) and (not end or timestamp <= end):
            lines.append(f'com,example)/ {timestamp} {url} text/html 200 {"X" * 32} 1234')
    context.status_code = 200
    return '\n'.join(lines)


def make_memento_callback(fetch_counts):
    "Serve mementos from the local archive, counting fetches per URL."

    def memento_callback(request, context):
        original, date, _mode = memento_url_data(request.url)
        fetch_counts[original] = fetch_counts.get(original, 0) + 1
        body = CAPTURES[original][format_timestamp(date)]
        context.status_code = 200
        context.headers['Memento-Datetime'] = email.utils.format_datetime(date, usegmt=True)
        context.headers['Content-Type'] = 'text/html'
        return body

    return memento_callback


def mock_archive(requests_mock):
    "Register the local archive with requests_mock. Returns a fetch counter."
    fetch_counts = {}
    requests_mock.get(CDX_SEARCH_URL, text=cdx_callback)
    requests_mock.get(MEMENTO_URL_PATTERN, text=make_memento_callback(fetch_counts))
    return fetch_counts


def test_crawl_fetches_only_captures_within_window(test_client, requests_mock):
    fetch_counts = mock_archive(requests_mock)
    result = test_client.crawl(ROOT_URL, CENTER, window=WINDOW)

    assert set(result.pages) == {ROOT_URL, 'http://example.com/a', 'http://example.com/b'}
    for page in result.pages.values():
        assert WINDOW_START <= page.timestamp <= WINDOW_END

    # The Wayback-rewritten link on /a embeds a 2022 timestamp, but the
    # crawl must select the in-window capture of /b instead.
    assert result.pages['http://example.com/b'].timestamp == datetime(2020, 5, 31, tzinfo=UTC)

    # Cycles (e.g. /a linking back to /) must not cause repeat fetches.
    assert fetch_counts == {ROOT_URL: 1, 'http://example.com/a': 1, 'http://example.com/b': 1}

    # Every request went to the archive -- no link was resolved against the
    # live web.
    for request in requests_mock.request_history:
        assert request.hostname == 'web.archive.org'


def test_crawl_records_skips_by_category(test_client, requests_mock):
    mock_archive(requests_mock)
    result = test_client.crawl(ROOT_URL, CENTER, window=WINDOW)

    skips = {(skip.url, skip.reason) for skip in result.skipped}
    assert ('http://other.com/x', 'offsite') in skips
    assert ('http://example.com/old', 'out_of_window') in skips
    assert ('http://example.com/missing', 'no_memento') in skips


def test_crawl_keeps_edges_with_capture_times(test_client, requests_mock):
    mock_archive(requests_mock)
    result = test_client.crawl(ROOT_URL, CENTER, window=WINDOW)

    edges = {(edge.source, edge.target): edge.captured_at for edge in result.edges}
    assert edges[(ROOT_URL, 'http://example.com/a')] == datetime(2020, 6, 2, tzinfo=UTC)
    assert edges[(ROOT_URL, 'http://example.com/b')] == datetime(2020, 5, 31, tzinfo=UTC)
    # Edges to pages that were not fetched have no capture time.
    assert edges[(ROOT_URL, 'http://other.com/x')] is None
    assert edges[(ROOT_URL, 'http://example.com/old')] is None
    assert edges[(ROOT_URL, 'http://example.com/missing')] is None
    # The cycle back to the root is kept as an edge, with the root's
    # capture time.
    assert edges[('http://example.com/a', ROOT_URL)] == datetime(2020, 6, 1, 12, tzinfo=UTC)


def test_crawl_can_cross_sites_when_allowed(test_client, requests_mock):
    mock_archive(requests_mock)
    result = test_client.crawl(ROOT_URL, CENTER, window=WINDOW, same_site=False)

    assert 'http://other.com/x' in result.pages
    assert WINDOW_START <= result.pages['http://other.com/x'].timestamp <= WINDOW_END
    assert not [skip for skip in result.skipped if skip.reason == 'offsite']


def test_crawl_respects_max_pages(test_client, requests_mock):
    fetch_counts = mock_archive(requests_mock)
    result = test_client.crawl(ROOT_URL, CENTER, window=WINDOW, max_pages=1)

    assert set(result.pages) == {ROOT_URL}
    assert fetch_counts == {ROOT_URL: 1}
