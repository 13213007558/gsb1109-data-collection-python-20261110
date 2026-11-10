import io

import pytest

from requests.exceptions import MultipartError, MultipartTruncatedError
from requests.models import Response
from requests.multipart import MultipartReader

BOUNDARY = "batch-boundary"


def build_multipart(parts, boundary=BOUNDARY, preamble=b"", epilogue=b""):
    out = [preamble]
    for headers, body in parts:
        out.append(b"--" + boundary.encode("ascii") + b"\r\n")
        for name, value in headers:
            out.append(f"{name}: {value}\r\n".encode("latin-1"))
        out.append(b"\r\n")
        out.append(body)
        out.append(b"\r\n")
    out.append(b"--" + boundary.encode("ascii") + b"--\r\n")
    out.append(epilogue)
    return b"".join(out)


def chunked(data, size):
    return [data[i : i + size] for i in range(0, len(data), size)]


def read_all(reader):
    return [(part.raw_headers, part.read()) for part in reader]


SAMPLE_PARTS = [
    (
        [
            ("Content-Type", "application/json"),
            ("X-Track", "1"),
            ("X-Track", "2"),
        ],
        b'{"id": 1}',
    ),
    ([], b""),
    (
        [("Content-Type", "text/plain")],
        b"not a delimiter: --batch-boundaryX\r\n"
        b"--batch-boundary= neither\r\n"
        b"nor is --batch-boundary mid-line\r\n"
        b"--batch-boundar\r\n",
    ),
]


@pytest.mark.parametrize("chunk_size", [1, 3, 7, 64, 4096])
def test_parts_streamed_with_small_chunks(chunk_size):
    body = build_multipart(SAMPLE_PARTS)
    reader = MultipartReader(chunked(body, chunk_size), BOUNDARY)

    parts = read_all(reader)

    assert len(parts) == 3
    headers, content = parts[0]
    assert content == b'{"id": 1}'
    # duplicate headers are preserved in raw_headers
    assert headers == (
        ("Content-Type", "application/json"),
        ("X-Track", "1"),
        ("X-Track", "2"),
    )
    assert parts[1] == ((), b"")
    assert parts[2][1] == SAMPLE_PARTS[2][1]


def test_duplicate_headers_case_insensitive_view():
    body = build_multipart(SAMPLE_PARTS)
    reader = MultipartReader([body], BOUNDARY)

    part = next(reader)

    assert part.headers["x-track"] == "2"  # last value wins in the view
    assert part.headers["Content-Type"] == "application/json"
    assert [v for n, v in part.raw_headers if n == "X-Track"] == ["1", "2"]
    part.skip()


def test_folded_and_malformed_headers():
    raw = (
        b"--batch-boundary\r\n"
        b"X-Folded: one\r\n"
        b"\ttwo\r\n"
        b"garbage-line-without-colon\r\n"
        b"\r\n"
        b"x\r\n"
        b"--batch-boundary--\r\n"
    )
    (part,) = read_all(MultipartReader([raw], BOUNDARY))
    assert part[0] == (("X-Folded", "one two"),)
    assert part[1] == b"x"


def test_skip_part_keeps_next_part_aligned():
    body = build_multipart(SAMPLE_PARTS)
    reader = MultipartReader(chunked(body, 5), BOUNDARY)

    iterator = iter(reader)
    first = next(iterator)
    assert first.headers["Content-Type"] == "application/json"
    # do not read the first body at all
    second = next(iterator)
    assert second.read() == b""
    third = next(iterator)
    assert third.read() == SAMPLE_PARTS[2][1]
    with pytest.raises(StopIteration):
        next(iterator)


def test_partial_read_then_advance():
    body = build_multipart(SAMPLE_PARTS)
    reader = MultipartReader(chunked(body, 4), BOUNDARY)

    iterator = iter(reader)
    first = next(iterator)
    assert first.read(4) == b'{"id'
    second = next(iterator)  # implicitly skips the rest of part one
    assert second.read() == b""


def test_preamble_and_epilogue_ignored():
    body = build_multipart(
        SAMPLE_PARTS[:1],
        preamble=b"junk --batch-boundary not at line start\r\n"
        b"--batch-boundaryX looks wrong\r\n",
        epilogue=b"trailing junk --batch-boundary\r\n",
    )
    parts = read_all(MultipartReader(chunked(body, 6), BOUNDARY))
    assert len(parts) == 1
    assert parts[0][1] == b'{"id": 1}'


def test_empty_message_has_no_parts():
    body = b"--batch-boundary--\r\n"
    assert read_all(MultipartReader([body], BOUNDARY)) == []


def test_truncated_stream_is_distinguished_from_clean_end():
    body = build_multipart(SAMPLE_PARTS)
    clean = read_all(MultipartReader([body], BOUNDARY))
    assert len(clean) == 3

    truncated = body[: -len(b"--batch-boundary--\r\n")]
    with pytest.raises(MultipartTruncatedError):
        read_all(MultipartReader(chunked(truncated, 3), BOUNDARY))


def test_truncated_mid_headers():
    body = b"--batch-boundary\r\nContent-Type: application/json\r\nX-Track: 1"
    with pytest.raises(MultipartTruncatedError):
        read_all(MultipartReader(chunked(body, 2), BOUNDARY))


def test_truncated_right_after_boundary():
    body = b"--batch-boundary\r\n\r\nx\r\n--batch-boundary"
    with pytest.raises(MultipartTruncatedError):
        read_all(MultipartReader(chunked(body, 2), BOUNDARY))


def test_header_size_limit():
    parts = [([("X-Big", "a" * 2048)], b"x")]
    body = build_multipart(parts)
    with pytest.raises(MultipartError, match="headers exceed"):
        read_all(MultipartReader([body], BOUNDARY, max_header_size=1024))

    # the same part parses fine with a sufficient limit
    (part,) = read_all(MultipartReader([body], BOUNDARY, max_header_size=4096))
    assert part[1] == b"x"


def test_invalid_boundary_rejected():
    with pytest.raises(MultipartError):
        MultipartReader([], "")
    with pytest.raises(MultipartError):
        MultipartReader([], "x" * 71)
    with pytest.raises(MultipartError):
        MultipartReader([], "bad\r\nboundary")


def test_close_releases_underlying_stream():
    closed = False

    def gen():
        nonlocal closed
        try:
            yield from chunked(build_multipart(SAMPLE_PARTS), 8)
        finally:
            closed = True

    reader = MultipartReader(gen(), BOUNDARY)
    part = next(reader)
    assert part.headers["Content-Type"] == "application/json"
    reader.close()
    assert closed
    with pytest.raises(StopIteration):
        next(reader)


def test_exhaustion_releases_underlying_stream():
    closed = False

    def gen():
        nonlocal closed
        try:
            yield from chunked(build_multipart(SAMPLE_PARTS), 8)
        finally:
            closed = True

    read_all(MultipartReader(gen(), BOUNDARY))
    assert closed


def test_context_manager_closes():
    closed = False

    def gen():
        nonlocal closed
        try:
            yield from chunked(build_multipart(SAMPLE_PARTS), 8)
        finally:
            closed = True

    with MultipartReader(gen(), BOUNDARY) as reader:
        next(reader)
    assert closed


def test_nested_multipart_is_opaque_by_default():
    inner = build_multipart(
        [([("Content-Type", "application/json")], b"{}")], boundary="inner-b"
    )
    body = build_multipart(
        [([("Content-Type", 'multipart/related; boundary="inner-b"')], inner)]
    )
    part = next(MultipartReader([body], BOUNDARY))
    assert part.read() == inner


def test_nested_multipart_depth_must_be_declared():
    inner = build_multipart(
        [([("Content-Type", "application/json")], b"{}")], boundary="inner-b"
    )
    body = build_multipart(
        [([("Content-Type", 'multipart/related; boundary="inner-b"')], inner)]
    )

    part = next(MultipartReader([body], BOUNDARY))
    with pytest.raises(MultipartError, match="max_depth"):
        part.iter_parts()

    part = next(MultipartReader([body], BOUNDARY, max_depth=2))
    sub_parts = read_all(part.iter_parts())
    assert len(sub_parts) == 1
    assert sub_parts[0][1] == b"{}"


def test_part_text_uses_its_own_charset():
    parts = [
        ([("Content-Type", "text/plain; charset=utf-16")], "héllo".encode("utf-16")),
        ([("Content-Type", "text/plain")], "héllo".encode("latin-1")),
    ]
    body = build_multipart(parts)
    reader = MultipartReader(chunked(body, 3), BOUNDARY)

    first = next(reader)
    assert first.encoding == "utf-16"
    assert first.text == "héllo"
    second = next(reader)
    assert second.text == "héllo"


def make_response(body, content_type=f'multipart/mixed; boundary="{BOUNDARY}"'):
    response = Response()
    response.status_code = 200
    response.headers["Content-Type"] = content_type
    response.raw = io.BytesIO(body)
    response._content = False
    return response


def test_response_iter_parts():
    response = make_response(build_multipart(SAMPLE_PARTS))

    reader = response.iter_parts(chunk_size=8)
    parts = [(p.headers.get("Content-Type"), p.read()) for p in reader]

    assert parts == [
        ("application/json", b'{"id": 1}'),
        (None, b""),
        ("text/plain", SAMPLE_PARTS[2][1]),
    ]


def test_response_iter_parts_early_close_releases_connection():
    response = make_response(build_multipart(SAMPLE_PARTS))

    reader = response.iter_parts(chunk_size=8)
    part = next(reader)
    assert part.read() == b'{"id": 1}'
    reader.close()

    assert response.raw.closed


def test_response_iter_parts_requires_multipart():
    response = make_response(b"{}", content_type="application/json")
    with pytest.raises(MultipartError, match="not multipart"):
        response.iter_parts()

    response = make_response(b"{}", content_type="multipart/mixed")
    with pytest.raises(MultipartError, match="boundary"):
        response.iter_parts()
