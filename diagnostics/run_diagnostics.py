"""
Runnable, fully offline diagnostic for Wayback error classification.

It drives the unmodified ``wayback`` package (the source in ``src/``) through
its real :class:`WaybackClient` and :class:`WaybackSession` code paths while
requests are intercepted locally. No default retry policy is changed.

Run:
    .venv/bin/python diagnostics/run_diagnostics.py
"""

from collections import Counter
import json
import sys
from pathlib import Path

import requests_mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

import requests  # noqa: E402

from wayback import WaybackClient, WaybackSession  # noqa: E402
from wayback._utils import RateLimit  # noqa: E402
from wayback.exceptions import WaybackRetryError  # noqa: E402

from fixtures import (  # noqa: E402
    CDX_URL,
    MEMENTO_URL,
    TARGET_URL,
    TIMESTAMP,
    memento_scenarios,
    search_scenarios,
)

OUTPUT_DIR = Path(__file__).resolve().parent / 'output'

BUSINESS_CATEGORY = {
    'no_snapshot': 'no snapshot',
    'blocked_site': 'site excluded',
    'robots': 'single resource restricted (robots.txt)',
    'transient': 'transient service error',
    'malformed': 'unexpected response format/shape',
    'rate_limit': 'live API throttling (429)',
    'actual_memento': 'valid archived capture (not an API failure)',
    'control': 'control / not an error',
}

# Explicit business categories for scenarios whose ids do not share the
# memento-scenario prefix convention.
SCENARIO_CATEGORY = {
    'search_blocked_admin_403': 'blocked_site',
    'search_blocked_marker_only_in_header': 'blocked_site',
    'search_blocked_body_only_no_header': 'blocked_site',
    'search_robots_403': 'robots',
    'search_robots_marker_inside_data_line': 'robots',
    'search_503_transient': 'transient',
    'search_malformed_line': 'malformed',
    'search_bad_timestamp_line': 'malformed',
    'search_empty_body': 'control',
    'search_valid_line': 'control',
}


# Scenarios that need non-default get_memento() arguments to reach the code
# path under test.
MEMENTO_KWARGS = {
    'malformed_redirect_loop': {'exact': False},
}


def category_for(scenario_id):
    if scenario_id in SCENARIO_CATEGORY:
        return BUSINESS_CATEGORY[SCENARIO_CATEGORY[scenario_id]]
    if scenario_id.startswith('no_snapshot'):
        prefix = 'no_snapshot'
    elif scenario_id.startswith('blocked_site'):
        prefix = 'blocked_site'
    elif scenario_id.startswith('robots'):
        prefix = 'robots'
    elif scenario_id.startswith('transient'):
        prefix = 'transient'
    elif scenario_id.startswith('rate_limit'):
        prefix = 'rate_limit'
    elif scenario_id.startswith('actual_memento'):
        prefix = 'actual_memento'
    else:
        prefix = 'malformed'
    return BUSINESS_CATEGORY[prefix]


def build_session(retries=6):
    # The retry budget keeps its library default (6). Only the sleep backoff
    # is zeroed so the diagnostic runs instantly; the classification code is
    # exercised exactly as shipped.
    return WaybackSession(
        retries=retries,
        backoff=0,
        search_calls_per_second=RateLimit(0),
        memento_calls_per_second=RateLimit(0),
    )

def describe_input(builder):
    """Peek at what the fixture would produce without consuming an exception."""
    try:
        response = builder()
    except Exception as error:  # noqa: BLE001 - diagnostic must capture everything
        return {
            'kind': 'exception before response',
            'error_type': type(error).__name__,
            'status': None,
            'headers': {},
            'body_snippet': str(error)[:120],
        }
    headers = {k: v for k, v in response.headers.items() if k.lower() in (
        'x-archive-wayback-runtime-error',
        'memento-datetime',
        'retry-after',
        'location',
    )}
    return {
        'kind': 'http response',
        'error_type': None,
        'status': response.status_code,
        'headers': headers,
        'body_snippet': response.text[:100].replace('\n', ' '),
    }


def would_session_retry(session, builder):
    """Read-only check of the session's own retry predicates."""
    try:
        response = builder()
    except Exception as error:  # noqa: BLE001
        retryable = session.should_retry_error(error)
        return retryable, f'should_retry_error({type(error).__name__}) -> {retryable}'
    if 'Memento-Datetime' in response.headers:
        return False, 'memento responses bypass the retry check entirely'
    retryable = session.should_retry(response)
    return retryable, f'status {response.status_code} in retryable_statuses -> {retryable}'


def run_memento_scenario(scenario_id, description, builder, note):
    session = build_session()
    client = WaybackClient(session)
    input_summary = describe_input(builder)
    retryable, retry_detail = would_session_retry(session, builder)

    with requests_mock.Mocker() as mocker:
        if input_summary['kind'] == 'exception before response':
            try:
                builder()
            except Exception as error:  # noqa: BLE001
                mocker.get(MEMENTO_URL, exc=error)
        else:
            response = builder()
            mocker.get(MEMENTO_URL, headers=dict(response.headers), text=response.text,
                       status_code=response.status_code)
        try:
            memento = client.get_memento(
                TARGET_URL, timestamp=TIMESTAMP, **MEMENTO_KWARGS.get(scenario_id, {})
            )
            outcome = 'Memento returned'
            message = f"archived status={memento.status_code}"
            exception_name = None
        except Exception as error:  # noqa: BLE001
            exception_name = type(error).__name__
            outcome = exception_name
            if isinstance(error, WaybackRetryError):
                message = f'{type(error.cause).__name__}: {error.cause}'
            else:
                message = str(error)[:160]
    client.close()

    return {
        'api': 'get_memento',
        'scenario': scenario_id,
        'description': description,
        'business_category': category_for(scenario_id),
        'input': input_summary,
        'outcome': outcome,
        'exception': exception_name,
        'message': message,
        'would_retry': retryable,
        'retry_basis': retry_detail,
        'evidence_note': note,
    }


def run_search_scenario(scenario_id, description, builder, note):
    session = build_session()
    client = WaybackClient(session)
    input_summary = describe_input(builder)
    retryable, retry_detail = would_session_retry(session, builder)

    with requests_mock.Mocker() as mocker:
        if input_summary['kind'] == 'exception before response':
            try:
                builder()
            except Exception as error:  # noqa: BLE001
                mocker.get(CDX_URL, exc=error)
        else:
            response = builder()
            mocker.get(CDX_URL, headers=dict(response.headers), text=response.text,
                       status_code=response.status_code)
        try:
            records = list(client.search(TARGET_URL))
            outcome = f'{len(records)} record(s) yielded'
            message = ''
            exception_name = None
        except Exception as error:  # noqa: BLE001
            exception_name = type(error).__name__
            outcome = exception_name
            message = str(error)[:160]
    client.close()

    return {
        'api': 'search (CDX)',
        'scenario': scenario_id,
        'description': description,
        'business_category': category_for(scenario_id),
        'input': input_summary,
        'outcome': outcome,
        'exception': exception_name,
        'message': message,
        'would_retry': retryable,
        'retry_basis': retry_detail,
        'evidence_note': note,
    }


def demonstrate_retry_paths():
    """Show what happens across multiple attempts; defaults are not modified."""
    findings = []

    # 1. Recoverable 503s followed by a real memento: retries until success.
    session = build_session(retries=2)
    attempts = Counter()

    def recovering_handler(request, context):
        attempts['memento'] += 1
        if attempts['memento'] <= 2:
            context.status_code = 503
            return 'temporarily unavailable'
        context.status_code = 200
        context.headers['Memento-Datetime'] = 'Fri, 29 Sep 2017 00:27:12 GMT'
        return '<html>archived content</html>'

    with requests_mock.Mocker() as mocker:
        mocker.get(MEMENTO_URL, text=recovering_handler)
        client = WaybackClient(session)
        memento = client.get_memento(TARGET_URL, timestamp=TIMESTAMP)
        client.close()
    findings.append({
        'case': 'two 503s then a memento, retries=2',
        'attempts': attempts['memento'],
        'result': f'success on attempt {attempts["memento"]}, archived status {memento.status_code}',
    })
    session.close()

    # 2. Persistent 503: budget exhausted, the last response reaches classification.
    session = build_session(retries=2)
    attempts = Counter()

    def persistent_503(request, context):
        attempts['memento'] += 1
        context.status_code = 503
        return 'unavailable'

    with requests_mock.Mocker() as mocker:
        mocker.get(MEMENTO_URL, text=persistent_503)
        client = WaybackClient(session)
        try:
            client.get_memento(TARGET_URL, timestamp=TIMESTAMP)
            result = 'unexpected success'
        except Exception as error:  # noqa: BLE001
            result = f'{type(error).__name__} after {attempts["memento"]} attempts'
        client.close()
    findings.append({'case': 'persistent 503, retries=2', 'attempts': attempts['memento'], 'result': result})
    session.close()

    # 3. Connection dropped every time: WaybackRetryError wraps the cause.
    session = build_session(retries=2)
    connection_error = requests.exceptions.ConnectionError(
        "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
    )

    with requests_mock.Mocker() as mocker:
        mocker.get(MEMENTO_URL, exc=connection_error)
        client = WaybackClient(session)
        try:
            client.get_memento(TARGET_URL, timestamp=TIMESTAMP)
            result = 'unexpected success'
        except WaybackRetryError as error:
            result = (
                f'WaybackRetryError(retries={error.retries}) wrapping '
                f'{type(error.cause).__name__}'
            )
        except Exception as error:  # noqa: BLE001
            result = f'{type(error).__name__}: {error} (note: not treated as retryable)'
        client.close()
    findings.append({
        'case': 'RemoteDisconnected on every attempt, retries=2',
        'attempts': len(mocker.request_history),
        'result': result,
    })
    session.close()

    return findings


def print_table(title, rows):
    print(f'\n### {title}\n')
    header = f'{"scenario":<48} {"category":<38} {"observed outcome":<28} retry?'
    print(header)
    print('-' * len(header))
    for row in rows:
        print(
            f'{row["scenario"]:<48} {row["business_category"]:<38} '
            f'{row["outcome"]:<28} {str(row["would_retry"]):<5}'
        )


def main():
    print('# Wayback error-classification diagnostic (offline)\n')
    print('Library retryable_statuses default:', sorted(WaybackSession.retryable_statuses))
    print('Retry budget: library default (6); backoff sleep zeroed for speed')

    memento_results = [
        run_memento_scenario(*scenario) for scenario in memento_scenarios()
    ]
    search_results = [
        run_search_scenario(*scenario) for scenario in search_scenarios()
    ]

    print_table('get_memento scenarios', memento_results)
    print_table('CDX search scenarios', search_results)

    print('\n### Retry-path demonstrations\n')
    retry_findings = demonstrate_retry_paths()
    for finding in retry_findings:
        print(f'- {finding["case"]}: {finding["result"]} (HTTP attempts: {finding["attempts"]})')

    print('\n### Detailed evidence\n')
    for row in memento_results + search_results:
        inp = row['input']
        status = inp['status'] if inp['status'] is not None else f'<{inp["error_type"]}>'
        print(f'- {row["scenario"]} [{row["api"]}]')
        print(f'    input   : status={status}; headers={inp["headers"]}; body="{inp["body_snippet"]}"')
        print(f'    outcome : {row["outcome"]} :: {row["message"]}')
        print(f'    retry   : {row["retry_basis"]}')
        print(f'    note    : {row["evidence_note"]}')

    OUTPUT_DIR.mkdir(exist_ok=True)
    payload = {
        'retryable_statuses': sorted(WaybackSession.retryable_statuses),
        'memento_results': memento_results,
        'search_results': search_results,
        'retry_demonstrations': retry_findings,
    }
    output_path = OUTPUT_DIR / 'results.json'
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f'\nStructured results written to {output_path.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
