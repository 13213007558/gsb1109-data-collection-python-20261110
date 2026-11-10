"""Tests for the pluggable request signing flow."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import io
import socket
import threading
from urllib.parse import urlsplit

import pytest

import requests
from requests.adapters import BaseAdapter
from requests.exceptions import SigningError, UnsignedBodyError
from requests.models import Response
from requests.signing import build_signature_input, sign_request
from requests.structures import CaseInsensitiveDict
from tests.testserver.server import Server, consume_socket_content

SECRET = b"super-secret-signing-material"
SIGNATURE_HEADER = "X-Signature"
DIGEST_HEADER = "X-Requests-Content-Digest"

#: Transport-level headers that are never part of the signed header block.
SERVER_EXCLUDED = {
    "host",
    "content-length",
    "transfer-encoding",
    "x-signature",
}


def _canonical(method, target, headers, body_digest):
    """Canonical byte string shared by the client signer and the receiver."""
    if body_digest is None:
        body_digest = hashlib.sha256(b"").hexdigest().encode("ascii")
    lines = [method.encode("ascii"), target.encode("ascii")]
    for name in sorted(headers, key=lambda item: item.lower()):
        if name.lower() in SERVER_EXCLUDED:
            continue
        lines.append(f"{name}: {headers[name]}".encode())
    lines.append(b"sha256=" + body_digest)
    return b"\n".join(lines)


def hmac_signer(signature_input):
    """Client-side signer operating on the post-encoding SignatureInput."""
    canonical = _canonical(
        signature_input["method"],
        signature_input["path_url"],
        signature_input["headers"],
        signature_input["body_digest"],
    )
    return {SIGNATURE_HEADER: hmac.new(SECRET, canonical, hashlib.sha256).hexdigest()}


def _receiver_signature(method, target, headers, body):
    """Recompute the signature from raw bytes observed on the wire."""
    digest = hashlib.sha256(body).hexdigest().encode("ascii")
    canonical = _canonical(method, target, headers, digest)
    return hmac.new(SECRET, canonical, hashlib.sha256).hexdigest()


def _parse_raw_request(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    method, target, _version = lines[0].decode("ascii").split(" ")
    headers = CaseInsensitiveDict()
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.decode("ascii").strip()] = value.decode("utf-8").strip()
    return method, target, headers, body


def _verify_handler(sock):
    raw = consume_socket_content(sock, timeout=0.5)
    method, target, headers, body = _parse_raw_request(raw)
    expected = _receiver_signature(method, target, headers, body)
    return {
        "method": method,
        "target": target,
        "received": headers.get(SIGNATURE_HEADER),
        "expected": expected,
        "body": body,
    }


def test_signature_input_reflects_fully_encoded_request():
    request = requests.Request(
        "POST",
        "http://example.com/path?existing=1",
        params=[("a", "1"), ("a", "2"), ("q", "hello world")],
        data={"k": "v x"},
        headers={"X-Custom": "abc"},
        signer=hmac_signer,
    )
    prepared = request.prepare()
    signature_input = build_signature_input(prepared)

    assert signature_input["url"] == (
        "http://example.com/path?existing=1&a=1&a=2&q=hello+world"
    )
    # Duplicate query parameters keep their order and encoded form.
    assert signature_input["path_url"] == ("/path?existing=1&a=1&a=2&q=hello+world")
    assert signature_input["method"] == "POST"
    assert signature_input["headers"]["X-Custom"] == "abc"
    # Transport-generated framing headers are not selected headers.
    assert "Content-Length" not in signature_input["headers"]
    # The digest is computed from the actual form-encoded wire body.
    wire_body = prepared.body.encode("utf-8")
    assert prepared.body == "k=v+x"
    assert signature_input["body"] == wire_body
    assert signature_input["body_digest"] == (
        hashlib.sha256(wire_body).hexdigest().encode("ascii")
    )
    assert SIGNATURE_HEADER in prepared.headers


def test_pre_encoding_signature_diverges_from_wire_content():
    """The original failure mode: signing before prepare cannot match."""
    naive_body = "k=v x"  # what the caller has before form encoding
    naive_digest = hashlib.sha256(naive_body.encode("utf-8")).hexdigest()

    prepared = requests.Request(
        "POST",
        "http://example.com/path",
        data={"k": "v x"},
        signer=hmac_signer,
    ).prepare()

    actual = build_signature_input(prepared)["body_digest"].decode("ascii")
    assert actual != naive_digest
    assert actual == hashlib.sha256(b"k=v+x").hexdigest()


def test_reprepare_recomputes_signature_for_new_content():
    prepared = requests.Request(
        "POST", "http://example.com/a", data=b"hello", signer=hmac_signer
    ).prepare()
    first_signature = prepared.headers[SIGNATURE_HEADER]

    prepared.url = "http://example.com/b"
    prepared.body = b"world"
    sign_request(prepared, prepared.signer)

    assert prepared.headers[SIGNATURE_HEADER] != first_signature
    signature_input = build_signature_input(prepared)
    assert signature_input["body_digest"] == (
        hashlib.sha256(b"world").hexdigest().encode("ascii")
    )


def test_identical_reprepare_is_deterministic():
    request = requests.Request("GET", "http://example.com/", signer=hmac_signer)
    first = request.prepare().headers[SIGNATURE_HEADER]
    second = request.prepare().headers[SIGNATURE_HEADER]
    assert first == second


def test_copy_preserves_signer_and_signed_headers():
    prepared = requests.Request(
        "GET", "http://example.com/", signer=hmac_signer
    ).prepare()
    clone = prepared.copy()

    assert clone.signer is prepared.signer
    assert clone._signed_headers == (SIGNATURE_HEADER,)
    assert clone.headers[SIGNATURE_HEADER] == prepared.headers[SIGNATURE_HEADER]


def test_oneshot_generator_body_is_refused():
    def stream():
        yield b"abc"

    with pytest.raises(UnsignedBodyError) as exc_info:
        requests.Request(
            "POST", "http://example.com/upload", data=stream(), signer=hmac_signer
        ).prepare()

    message = str(exc_info.value)
    assert SECRET.decode() not in message
    assert SIGNATURE_HEADER not in message


def test_chunked_body_rejects_caller_digest_without_length():
    class Chunked:
        def __iter__(self):
            return iter((b"abc",))

    digest = hashlib.sha256(b"abc").hexdigest()
    with pytest.raises(UnsignedBodyError):
        requests.Request(
            "POST",
            "http://example.com/upload",
            data=Chunked(),
            headers={DIGEST_HEADER: digest},
            signer=hmac_signer,
        ).prepare()


def test_provided_digest_accepted_for_fixed_length_stream():
    digest = hashlib.sha256(b"abc").hexdigest()

    class FixedLengthStream:
        def __iter__(self):
            return iter((b"abc",))

        def __len__(self):
            return 3

    captured = []

    def capturing_signer(signature_input):
        captured.append(signature_input)
        return hmac_signer(signature_input)

    prepared = requests.Request(
        "POST",
        "http://example.com/upload",
        data=FixedLengthStream(),
        headers={DIGEST_HEADER: digest},
        signer=capturing_signer,
    ).prepare()

    # The internal digest channel is consumed, never sent or signed.
    assert DIGEST_HEADER not in prepared.headers
    assert captured[0]["body"] is None
    assert captured[0]["body_digest"] == digest.encode("ascii")
    assert SIGNATURE_HEADER in prepared.headers


def test_seekable_body_is_hashed_and_rewound():
    body_file = io.BytesIO(b"x" * 100 + b"y" * 37)
    expected_digest = hashlib.sha256(body_file.getvalue()).hexdigest()

    prepared = requests.Request(
        "POST", "http://example.com/upload", data=body_file, signer=hmac_signer
    ).prepare()
    signature_input = build_signature_input(prepared)

    assert body_file.tell() == 0
    assert signature_input["body"] is None
    assert signature_input["body_digest"] == expected_digest.encode("ascii")
    # The file object is still the body and still holds every byte.
    assert prepared.body is body_file
    assert body_file.read() == b"x" * 100 + b"y" * 37


def test_signer_returning_none_adds_no_header():
    prepared = requests.Request(
        "GET", "http://example.com/", signer=lambda signature_input: None
    ).prepare()
    assert SIGNATURE_HEADER not in prepared.headers


def test_non_mapping_signer_result_is_rejected():
    with pytest.raises(SigningError, match="mapping"):
        requests.Request(
            "GET", "http://example.com/", signer=lambda signature_input: "nope"
        ).prepare()


def test_invalid_signer_header_never_leaks_value():
    secret_value = f"leak-{SECRET.decode()}"

    def bad_signer(signature_input):
        return {"X-Bad-Signature": f"{secret_value}\r\n"}

    with pytest.raises(SigningError) as exc_info:
        requests.Request("GET", "http://example.com/", signer=bad_signer).prepare()

    message = str(exc_info.value)
    assert secret_value not in message
    assert SECRET.decode() not in message
    assert "X-Bad-Signature" in message


def test_signer_exception_propagates_without_wrapping():
    class VendorSigningFailure(Exception):
        pass

    def failing_signer(signature_input):
        raise VendorSigningFailure("key unavailable")

    with pytest.raises(VendorSigningFailure):
        requests.Request("GET", "http://example.com/", signer=failing_signer).prepare()


def test_explicit_host_header_is_part_of_signature_input():
    prepared = requests.Request(
        "GET",
        "http://example.com/",
        headers={"Host": "special.example"},
        signer=hmac_signer,
    ).prepare()
    signature_input = build_signature_input(prepared)
    assert signature_input["headers"]["Host"] == "special.example"


def test_empty_body_digest():
    prepared = requests.Request(
        "GET", "http://example.com/", signer=hmac_signer
    ).prepare()
    signature_input = build_signature_input(prepared)
    assert signature_input["body"] is None
    assert signature_input["body_digest"] is None


def test_session_signer_and_per_request_override():
    def other_signer(signature_input):
        return {"X-Other-Signature": "abc"}

    session = requests.Session()
    session.signer = hmac_signer

    session_signed = session.prepare_request(
        requests.Request("GET", "http://example.com/")
    )
    assert SIGNATURE_HEADER in session_signed.headers

    overridden = session.prepare_request(
        requests.Request("GET", "http://example.com/", signer=other_signer)
    )
    assert overridden.headers.get("X-Other-Signature") == "abc"
    assert SIGNATURE_HEADER not in overridden.headers


def test_session_pickle_does_not_carry_signer():
    import pickle

    session = requests.Session()
    session.signer = hmac_signer
    restored = pickle.loads(pickle.dumps(session))
    assert restored.signer is None


def test_digest_header_without_signer_is_left_untouched():
    prepared = requests.Request(
        "POST",
        "http://example.com/",
        data=b"abc",
        headers={DIGEST_HEADER: "whatever"},
    ).prepare()
    assert prepared.headers[DIGEST_HEADER] == "whatever"


def _can_bind_loopback():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


requires_loopback = pytest.mark.skipif(
    not _can_bind_loopback(),
    reason="loopback socket binding is prohibited in this environment",
)


class _RecordingSocket:
    """Socket stand-in capturing the exact bytes http.client writes."""

    def __init__(self):
        self.sent = b""

    def sendall(self, data):
        self.sent += data

    def send(self, data):
        if not isinstance(data, bytes):
            data = data.encode("ascii")
        self.sent += data
        return len(data)

    def close(self):
        pass


class _SocketBackedFile:
    """Minimal socket wrapper exposing the makefile API HTTPResponse needs."""

    def __init__(self, buffer):
        self._buffer = buffer

    def makefile(self, mode="rb"):
        return self._buffer

    def close(self):
        self._buffer.close()


class _RawResponse:
    def __init__(self, original):
        self._original_response = original
        self.status = original.status
        self.reason = original.reason
        self.headers = CaseInsensitiveDict(original.getheaders())

    def stream(self, chunk_size, decode_content=True):
        return iter(())

    def read(self, *args, **kwargs):
        return b""

    def close(self):
        self._original_response.close()


class LocalReceiverAdapter(BaseAdapter):
    """Materializes real HTTP/1.1 wire bytes without binding a port.

    The request is encoded by the standard library ``http.client`` against an
    in-memory socket, then parsed back and verified exactly as a network
    receiver would do. Responses are configurable per request target so the
    normal Session redirect machinery can be exercised end to end.
    """

    def __init__(self, responses=None):
        self.captured = []
        self._responses = responses or {}

    def send(
        self,
        request,
        stream=False,
        timeout=None,
        verify=True,
        cert=None,
        proxies=None,
    ):
        parsed = urlsplit(request.url)
        wire = self._render_wire(request, parsed)
        method, target, headers, body = _parse_raw_request(wire)
        self.captured.append(
            {
                "method": method,
                "target": target,
                "headers": headers,
                "body": body,
                "received": headers.get(SIGNATURE_HEADER),
                "expected": _receiver_signature(method, target, headers, body),
            }
        )
        raw_response = self._responses.get(
            target, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        )
        return self._build_response(request, raw_response)

    def close(self):
        pass

    def _render_wire(self, request, parsed):
        recording = _RecordingSocket()
        connection = http.client.HTTPConnection(parsed.hostname or "local")
        connection.sock = recording
        chunked = not (request.body is None or "Content-Length" in request.headers)
        connection.request(
            request.method,
            request.path_url,
            body=request.body,
            headers=dict(request.headers),
            encode_chunked=chunked,
        )
        return recording.sent

    def _build_response(self, request, raw_response):
        response_socket = io.BytesIO(raw_response)
        parsed_response = http.client.HTTPResponse(_SocketBackedFile(response_socket))
        parsed_response.begin()
        response = Response()
        response.status_code = parsed_response.status
        response.reason = parsed_response.reason
        response.headers = CaseInsensitiveDict(parsed_response.getheaders())
        response.encoding = "utf-8"
        response.raw = _RawResponse(parsed_response)
        response.url = request.url
        response.request = request
        response.connection = self
        return response


def _mounted_session(adapter):
    session = requests.Session()
    session.mount("http://", adapter)
    return session


def test_receiver_recomputes_signature_over_encoded_wire_bytes():
    adapter = LocalReceiverAdapter()
    session = _mounted_session(adapter)

    # A signature over the caller's pre-encoding representation, for contrast.
    naive_canonical = _canonical(
        "POST",
        "/path",
        CaseInsensitiveDict(),
        hashlib.sha256(b"k=v x").hexdigest().encode("ascii"),
    )
    naive_signature = hmac.new(SECRET, naive_canonical, hashlib.sha256).hexdigest()

    response = session.request(
        "POST",
        "http://local.test/path",
        params=[("a", "1"), ("a", "2"), ("q", "hello world")],
        data={"k": "v x"},
        signer=hmac_signer,
    )

    assert response.status_code == 200
    observed = adapter.captured[0]
    # Percent-encoding and duplicate query ordering survive onto the wire.
    assert observed["target"] == "/path?a=1&a=2&q=hello+world"
    assert observed["body"] == b"k=v+x"
    # The receiver independently recomputes the HMAC from the raw bytes.
    assert observed["received"] == observed["expected"]
    assert observed["received"] != naive_signature


def test_receiver_confirms_fixed_length_stream_signature_without_consuming_it():
    digest = hashlib.sha256(b"abc").hexdigest()

    class FixedLengthStream:
        def __iter__(self):
            return iter((b"abc",))

        def __len__(self):
            return 3

    adapter = LocalReceiverAdapter()
    session = _mounted_session(adapter)
    response = session.request(
        "POST",
        "http://local.test/stream",
        data=FixedLengthStream(),
        headers={DIGEST_HEADER: digest},
        signer=hmac_signer,
    )

    assert response.status_code == 200
    observed = adapter.captured[0]
    assert observed["body"] == b"abc"
    assert observed["received"] == observed["expected"]
    assert DIGEST_HEADER not in observed["headers"]


def test_redirect_re_signs_each_request_against_its_own_content():
    adapter = LocalReceiverAdapter(
        responses={
            "/start": (
                b"HTTP/1.1 302 FOUND\r\n"
                b"Content-Length: 0\r\n"
                b"Location: /redirected\r\n\r\n"
            )
        }
    )
    session = _mounted_session(adapter)

    response = session.request("GET", "http://local.test/start", signer=hmac_signer)

    assert response.status_code == 200
    first, second = adapter.captured
    assert first["target"] == "/start"
    assert second["target"] == "/redirected"
    # Every hop verifies against the bytes observed on that specific hop.
    assert first["received"] == first["expected"]
    assert second["received"] == second["expected"]
    # The original signature is stale for the redirected request.
    assert first["received"] != second["received"]


def test_temporary_redirect_rehashes_rewound_file_body():
    adapter = LocalReceiverAdapter(
        responses={
            "/start": (
                b"HTTP/1.1 307 TEMPORARY REDIRECT\r\n"
                b"Content-Length: 0\r\n"
                b"Location: /moved\r\n\r\n"
            )
        }
    )
    session = _mounted_session(adapter)
    body_file = io.BytesIO(b"redirect-me")

    response = session.request(
        "POST",
        "http://local.test/start",
        data=body_file,
        signer=hmac_signer,
    )

    assert response.status_code == 200
    first, second = adapter.captured
    # 307 preserves both the method and the full body on the second hop.
    assert first["method"] == second["method"] == "POST"
    assert first["body"] == second["body"] == b"redirect-me"
    # The rewound body is re-digested and every hop independently verifies.
    assert first["received"] == first["expected"]
    assert second["received"] == second["expected"]
    assert first["received"] != second["received"]
    assert body_file.tell() == len(b"redirect-me")


def test_resend_keeps_a_signature_that_still_verifies():
    adapter = LocalReceiverAdapter()
    session = _mounted_session(adapter)
    prepared = session.prepare_request(
        requests.Request(
            "POST",
            "http://local.test/resend",
            data=b"replay-body",
            signer=hmac_signer,
        )
    )

    first = session.send(prepared, allow_redirects=False)
    second = session.send(prepared, allow_redirects=False)

    assert first.status_code == second.status_code == 200
    first_result, second_result = adapter.captured
    assert first_result["received"] == first_result["expected"]
    assert second_result["received"] == second_result["expected"]
    # Identical wire content deterministically yields the same signature.
    assert first_result["received"] == second_result["received"]


def _ok_handler(sock):
    result = _verify_handler(sock)
    sock.send(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    return result


@requires_loopback
def test_socket_receiver_verifies_encoding_and_duplicate_query():
    close_server = threading.Event()
    server = Server(_ok_handler, wait_to_close_event=close_server)

    with server as (host, port):
        url = f"http://{host}:{port}/path"
        response = requests.post(
            url,
            params=[("a", "1"), ("a", "2"), ("q", "hello world")],
            data={"k": "v x"},
            signer=hmac_signer,
        )
        close_server.set()

    assert response.status_code == 200
    result = server.handler_results[0]
    assert result["target"] == "/path?a=1&a=2&q=hello+world"
    assert result["body"] == b"k=v+x"
    assert result["received"] == result["expected"]


@requires_loopback
def test_socket_redirect_re_signs_each_request():
    def redirect_handler(sock):
        result = _verify_handler(sock)
        if result["target"] == "/start":
            sock.send(
                b"HTTP/1.1 302 FOUND\r\n"
                b"Content-Length: 0\r\n"
                b"Location: /redirected\r\n\r\n"
            )
        else:
            sock.send(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        return result

    close_server = threading.Event()
    server = Server(
        redirect_handler,
        requests_to_handle=2,
        wait_to_close_event=close_server,
    )

    with server as (host, port):
        response = requests.get(
            f"http://{host}:{port}/start",
            signer=hmac_signer,
        )
        close_server.set()

    assert response.status_code == 200
    first, second = server.handler_results
    assert first["received"] == first["expected"]
    assert second["received"] == second["expected"]
    assert first["received"] != second["received"]


@requires_loopback
def test_socket_resend_keeps_a_signature_that_still_verifies():
    close_server = threading.Event()
    server = Server(
        _verify_handler,
        requests_to_handle=2,
        wait_to_close_event=close_server,
    )

    with server as (host, port):
        session = requests.Session()
        prepared = session.prepare_request(
            requests.Request(
                "POST",
                f"http://{host}:{port}/resend",
                data=b"replay-body",
                signer=hmac_signer,
            )
        )
        first = session.send(prepared, allow_redirects=False)
        second = session.send(prepared, allow_redirects=False)
        close_server.set()

    assert first.status_code == second.status_code == 200
    first_result, second_result = server.handler_results
    assert first_result["received"] == first_result["expected"]
    assert second_result["received"] == second_result["expected"]
    assert first_result["received"] == second_result["received"]
