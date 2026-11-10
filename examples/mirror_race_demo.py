"""Runnable demonstration of :func:`requests.race_read`.

It needs no network access and no permission to bind ports: every "mirror" is
served in-process over a real :func:`socket.socketpair` behind a custom
transport adapter, so the same code path used against real HTTP mirrors runs
against fully controlled mirrors here.

Run it with::

    python examples/mirror_race_demo.py
"""

from __future__ import annotations

import select
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import urllib3

import requests
from requests.adapters import HTTPAdapter
from requests.race import RaceError, race_read


@dataclass
class MirrorBehavior:
    delay: float
    status: int = 200
    body: bytes = b"mirror-payload"
    hits: int = 0


class LocalMirrorAdapter(HTTPAdapter):
    """Transport adapter emulating mirrors over socket pairs."""

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
        behavior.hits += 1

        read_timeout = timeout[1] if isinstance(timeout, tuple) else timeout
        client_sock, server_sock = socket.socketpair()
        threading.Thread(
            target=self._serve, args=(behavior, server_sock), daemon=True
        ).start()
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
    def _serve(behavior: MirrorBehavior, server_sock: socket.socket) -> None:
        try:
            time.sleep(behavior.delay)
            server_sock.sendall(behavior.body)
        except OSError:
            pass
        finally:
            server_sock.close()


def build_session(behaviors: dict[str, MirrorBehavior]) -> requests.Session:
    session = requests.Session()
    session.mount("mirror://", LocalMirrorAdapter(behaviors))
    return session


def url(name: str) -> str:
    return f"mirror://{name}/data"


def payload_is_valid(response: requests.Response) -> bool:
    return b"mirror-payload" in response.content


def print_attempts(records) -> None:
    print("  actual attempt records:")
    for record in records:
        elapsed = "n/a" if record.elapsed is None else f"{record.elapsed:.3f}s"
        print(
            f"    - {record.name:<8} status={record.status:<11} "
            f"elapsed={elapsed:<7} closed={record.response_closed!s:<5} "
            f"detail={record.detail!r}"
        )


def run_scenario(
    title: str,
    behaviors: dict[str, MirrorBehavior],
    *,
    delay: float,
    timeout: float,
    budget: float | None = None,
) -> None:
    print(f"\n=== {title} ===")
    session = build_session(behaviors)
    try:
        result = race_read(
            "GET",
            [url("primary"), url("backup")],
            idempotent=True,
            delay=delay,
            timeout=timeout,
            budget=budget,
            content_validator=payload_is_valid,
            session=session,
        )
    except RaceError as exc:
        print(f"  result: RaceError (budget_timeout={exc.budget_timeout})")
        print_attempts(exc.attempts)
    else:
        print(f"  result: winner={result.winner}, body={result.response.content!r}")
        print_attempts(result.attempts)
        result.response.close()
    finally:
        session.close()
        for name, behavior in behaviors.items():
            print(f"  {name} received {behavior.hits} request(s)")


def main() -> None:
    run_scenario(
        "1. primary is fast: it wins, the backup response is closed",
        {
            "primary": MirrorBehavior(delay=0.05),
            "backup": MirrorBehavior(delay=0.4),
        },
        delay=0.1,
        timeout=5,
    )

    run_scenario(
        "2. primary is stuck: after the short wait the fast backup wins",
        {
            "primary": MirrorBehavior(delay=0.5),
            "backup": MirrorBehavior(delay=0.05),
        },
        delay=0.1,
        timeout=5,
    )

    run_scenario(
        "3. neither mirror qualifies: error status and bad content",
        {
            "primary": MirrorBehavior(delay=0.05, status=500, body=b"error"),
            "backup": MirrorBehavior(delay=0.05, body=b"unexpected content"),
        },
        delay=0.05,
        timeout=5,
    )


if __name__ == "__main__":
    main()
