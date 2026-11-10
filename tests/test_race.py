"""Tests for the explicitly enabled mirror racing entry point.

The sandbox these tests run in forbids binding network ports, so the controlled
"mirror servers" live in-process behind a custom transport adapter. Each mirror
is still served over a real :func:`socket.socketpair`: a server-side thread
applies the configured delay, status and body, while the client side is a real
urllib3 response whose socket is released by :meth:`Response.close`. This keeps
the outcomes deterministic, lets the per-request timeout actually fire, and
retains the real attempt history produced by the race.
"""

from __future__ import annotations

import select
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import pytest
import urllib3

import requests
from requests.adapters import HTTPAdapter
from requests.race import (
    ABANDONED,
    FAILED,
    NOT_STARTED,
    REJECTED,
    WON,
    RaceError,
    race_read,
)


@dataclass
class MirrorBehavior:
    """Configured behavior of one controlled mirror."""

    delay: float
    status: int = 200
    body: bytes = b"mirror-payload"
    hits: int = 0
    hit_lock: threading.Lock = field(default_factory=threading.Lock)
    last_request_headers: dict[str, str] | None = None

    def record_hit(self) -> None:
        with self.hit_lock:
            self.hits += 1


class ControlledMirrorAdapter(HTTPAdapter):
    """Transport adapter that serves mirror URLs from in-process behaviors."""

    def __init__(self, behaviors: dict[str, MirrorBehavior]) -> None:
        super().__init__()
        self._behaviors = behaviors

    def send(
        self,
        request,
        stream=False,
        timeout=None,
        verify=True,
        cert=None,
        proxies=None,
    ):
        host = urlparse(request.url).hostname or ""
        behavior = self._behaviors[host]
        behavior.record_hit()
        behavior.last_request_headers = dict(request.headers)

        read_timeout = self._read_timeout(timeout)
        client_sock, server_sock = socket.socketpair()
        if read_timeout is not None:
            client_sock.settimeout(read_timeout)
        server_thread = threading.Thread(
            target=self._serve,
            args=(behavior, server_sock),
            daemon=True,
        )
        server_thread.start()
        try:
            readable, _, _ = select.select([client_sock], [], [], read_timeout)
            if not readable:
                raise requests.ReadTimeout(
                    f"{host} did not respond within {read_timeout}s"
                )
            raw = urllib3.HTTPResponse(
                body=client_sock.makefile("rb"),
                headers={"Content-Length": str(len(behavior.body))},
                status=behavior.status,
                reason="OK",
                preload_content=False,
            )
            return self.build_response(request, raw)
        except BaseException:
            client_sock.close()
            raise

    @staticmethod
    def _read_timeout(timeout: Any) -> float | None:
        if timeout is None:
            return None
        if isinstance(timeout, tuple):
            timeout = timeout[1]
        if isinstance(timeout, urllib3.Timeout):
            timeout = timeout.read_timeout
        return float(timeout) if timeout is not None else None

    def _serve(
        self,
        behavior: MirrorBehavior,
        server_sock: socket.socket,
    ) -> None:
        try:
            time.sleep(behavior.delay)
            server_sock.sendall(behavior.body)
        except OSError:
            pass
        finally:
            try:
                server_sock.close()
            except OSError:
                pass


@pytest.fixture
def mirror_session():
    behaviors: dict[str, MirrorBehavior] = {}

    def factory(
        **configs: MirrorBehavior,
    ) -> tuple[requests.Session, dict[str, MirrorBehavior]]:
        behaviors.update(configs)
        sess = requests.Session()
        sess.mount("mirror://", ControlledMirrorAdapter(behaviors))
        return sess, behaviors

    return factory


def mirror_url(name: str) -> str:
    return f"mirror://{name}/data"


def has_expected_payload(response: requests.Response) -> bool:
    return b"mirror-payload" in response.content


def test_requires_explicit_idempotent_flag(mirror_session):
    sess, _ = mirror_session(primary=MirrorBehavior(delay=0))
    with pytest.raises(ValueError, match="idempotent=True"):
        race_read("GET", [mirror_url("primary")], idempotent=False, session=sess)


def test_rejects_non_read_methods_even_when_declared_idempotent(mirror_session):
    sess, _ = mirror_session(primary=MirrorBehavior(delay=0))
    with pytest.raises(ValueError, match="safe read methods"):
        race_read("POST", [mirror_url("primary")], idempotent=True, session=sess)


def test_requires_at_least_one_mirror():
    with pytest.raises(ValueError, match="at least one mirror URL"):
        race_read("GET", [], idempotent=True)


def test_primary_fast_wins_and_backup_response_is_closed(mirror_session):
    sess, behaviors = mirror_session(
        primary=MirrorBehavior(delay=0.1),
        backup=MirrorBehavior(delay=0.5),
    )

    result = race_read(
        "GET",
        [mirror_url("primary"), mirror_url("backup")],
        idempotent=True,
        delay=0.05,
        timeout=5,
        content_validator=has_expected_payload,
        session=sess,
    )

    assert result.winner == "primary"
    assert result.response.status_code == 200
    by_name = {record.name: record for record in result.attempts}
    assert by_name["primary"].status == WON
    assert by_name["primary"].response_closed is False
    assert by_name["backup"].status == ABANDONED
    assert by_name["backup"].started is True
    assert by_name["backup"].response_closed is True
    assert behaviors["backup"].hits == 1
    assert by_name["primary"].url == mirror_url("primary")
    assert by_name["backup"].url == mirror_url("backup")


def test_backup_fast_wins_when_primary_is_stuck(mirror_session):
    sess, _ = mirror_session(
        primary=MirrorBehavior(delay=0.6),
        backup=MirrorBehavior(delay=0.1),
    )

    result = race_read(
        "GET",
        [mirror_url("primary"), mirror_url("backup")],
        idempotent=True,
        delay=0.1,
        timeout=5,
        content_validator=has_expected_payload,
        session=sess,
    )

    assert result.winner == "backup"
    by_name = {record.name: record for record in result.attempts}
    assert by_name["backup"].status == WON
    assert by_name["primary"].status == ABANDONED
    assert by_name["primary"].response_closed is True
    assert by_name["primary"].elapsed is not None
    assert by_name["backup"].elapsed is not None


def test_all_mirrors_invalid_raises_race_error_with_full_records(mirror_session):
    sess, _ = mirror_session(
        primary=MirrorBehavior(delay=0.05, status=500, body=b"boom"),
        backup=MirrorBehavior(delay=0.05, status=200, body=b"unexpected content"),
    )

    with pytest.raises(RaceError) as exc_info:
        race_read(
            "GET",
            [mirror_url("primary"), mirror_url("backup")],
            idempotent=True,
            delay=0.02,
            timeout=5,
            content_validator=has_expected_payload,
            session=sess,
        )

    error = exc_info.value
    assert error.budget_timeout is False
    by_name = {record.name: record for record in error.attempts}
    assert by_name["primary"].status == REJECTED
    assert "status check failed" in by_name["primary"].detail
    assert by_name["primary"].response_closed is True
    assert by_name["primary"].response.raw.isclosed() is True
    assert by_name["backup"].status == REJECTED
    assert "content check failed" in by_name["backup"].detail
    assert by_name["backup"].response_closed is True
    assert [record.name for record in error.attempts] == ["primary", "backup"]


def test_early_error_response_cannot_steal_the_result(mirror_session):
    sess, _ = mirror_session(
        primary=MirrorBehavior(delay=0.02, status=503, body=b"down"),
        backup=MirrorBehavior(delay=0.3),
    )

    result = race_read(
        "GET",
        [mirror_url("primary"), mirror_url("backup")],
        idempotent=True,
        delay=0.02,
        timeout=5,
        content_validator=has_expected_payload,
        session=sess,
    )

    assert result.winner == "backup"
    by_name = {record.name: record for record in result.attempts}
    assert by_name["primary"].status == REJECTED
    assert by_name["primary"].response_closed is True
    assert by_name["backup"].status == WON


def test_unscheduled_attempt_is_never_sent(mirror_session):
    sess, behaviors = mirror_session(
        primary=MirrorBehavior(delay=0.03),
        backup=MirrorBehavior(delay=0.03),
    )

    result = race_read(
        "GET",
        [mirror_url("primary"), mirror_url("backup")],
        idempotent=True,
        delay=0.5,
        timeout=5,
        session=sess,
    )

    assert result.winner == "primary"
    by_name = {record.name: record for record in result.attempts}
    assert by_name["backup"].status == NOT_STARTED
    assert by_name["backup"].started is False
    assert behaviors["backup"].hits == 0


def test_per_request_timeout_is_independent_of_total_budget(mirror_session):
    sess, _ = mirror_session(
        primary=MirrorBehavior(delay=10),
        backup=MirrorBehavior(delay=10),
    )

    started = time.monotonic()
    with pytest.raises(RaceError) as exc_info:
        race_read(
            "GET",
            [mirror_url("primary"), mirror_url("backup")],
            idempotent=True,
            delay=0.05,
            timeout=0.2,
            budget=5,
            session=sess,
        )
    elapsed = time.monotonic() - started

    error = exc_info.value
    assert error.budget_timeout is False
    assert all(record.status == FAILED for record in error.attempts)
    assert all(
        isinstance(record.error, (requests.ReadTimeout, requests.Timeout))
        for record in error.attempts
    )
    assert elapsed < 1.5


def test_budget_expiry_abandons_in_flight_and_skips_unscheduled(mirror_session):
    sess, behaviors = mirror_session(
        primary=MirrorBehavior(delay=10),
        backup=MirrorBehavior(delay=10),
        mirror_2=MirrorBehavior(delay=10),
    )

    started = time.monotonic()
    with pytest.raises(RaceError) as exc_info:
        race_read(
            "GET",
            [mirror_url("primary"), mirror_url("backup"), mirror_url("mirror_2")],
            idempotent=True,
            delay=0.1,
            timeout=0.4,
            budget=0.15,
            session=sess,
        )
    elapsed = time.monotonic() - started

    error = exc_info.value
    assert error.budget_timeout is True
    assert elapsed < 1.5
    statuses = {record.name: record.status for record in error.attempts}
    assert statuses == {
        "primary": ABANDONED,
        "backup": ABANDONED,
        "mirror-2": NOT_STARTED,
    }
    assert behaviors["mirror_2"].hits == 0
    for record in error.attempts:
        if record.status == ABANDONED:
            if record.response is not None:
                assert record.response_closed is True
                assert record.response.raw.isclosed() is True
            else:
                assert isinstance(
                    record.error, (requests.ReadTimeout, requests.Timeout)
                )


def test_losing_valid_response_is_closed(mirror_session):
    sess, _ = mirror_session(
        primary=MirrorBehavior(delay=0.4),
        backup=MirrorBehavior(delay=0.05),
    )

    result = race_read(
        "GET",
        [mirror_url("primary"), mirror_url("backup")],
        idempotent=True,
        delay=0.05,
        timeout=5,
        content_validator=has_expected_payload,
        session=sess,
    )

    assert result.winner == "backup"
    primary_record = next(
        record for record in result.attempts if record.name == "primary"
    )
    assert primary_record.status == ABANDONED
    assert primary_record.response_closed is True
    assert primary_record.response.raw.isclosed() is True


def test_attempt_records_capture_urls_and_timing(mirror_session):
    sess, _ = mirror_session(
        primary=MirrorBehavior(delay=0.05),
        backup=MirrorBehavior(delay=0.05),
    )

    result = race_read(
        "GET",
        [mirror_url("primary"), mirror_url("backup")],
        idempotent=True,
        delay=0.02,
        timeout=5,
        session=sess,
    )

    records = result.attempts
    assert [record.url for record in records] == [
        mirror_url("primary"),
        mirror_url("backup"),
    ]
    for record in records:
        assert record.started_at is not None
        assert record.finished_at is not None
        assert record.elapsed is not None
        assert record.finished_at >= record.started_at
        assert record.elapsed >= 0


def test_request_kwargs_are_forwarded_to_every_attempt(mirror_session):
    sess, behaviors = mirror_session(
        primary=MirrorBehavior(delay=0.4),
        backup=MirrorBehavior(delay=0.05),
    )

    race_read(
        "GET",
        [mirror_url("primary"), mirror_url("backup")],
        idempotent=True,
        delay=0.05,
        timeout=5,
        session=sess,
        headers={"X-Trace": "abc123"},
    )

    for behavior in behaviors.values():
        assert behavior.last_request_headers is not None
        assert behavior.last_request_headers["X-Trace"] == "abc123"


@pytest.mark.parametrize("round_", range(8))
def test_no_losing_response_is_left_open(mirror_session, round_):
    delays = [(0.02, 0.3), (0.3, 0.02), (0.05, 0.05)]
    primary_delay, backup_delay = delays[round_ % len(delays)]
    sess, _ = mirror_session(
        primary=MirrorBehavior(delay=primary_delay),
        backup=MirrorBehavior(delay=backup_delay),
    )

    try:
        result = race_read(
            "GET",
            [mirror_url("primary"), mirror_url("backup")],
            idempotent=True,
            delay=0.05,
            timeout=5,
            content_validator=has_expected_payload,
            session=sess,
        )
    except RaceError as exc:
        records = exc.attempts
    else:
        records = result.attempts

    for record in records:
        if record.response is None:
            continue
        if record.status == WON:
            assert record.response_closed is False
        else:
            assert record.response_closed is True
            assert record.response.raw.isclosed() is True
