from requests import Response, ServerSentEvent, SSEClient


class ByteByByteResponse:
    """A minimal streaming response that serves its payload one byte at
    a time, so multi-byte UTF-8 characters and line terminators are
    split across arbitrary chunk boundaries."""

    def __init__(self, payload):
        self._payload = payload
        self.closed = False
        self.close_calls = 0

    def iter_content(self, chunk_size=1, decode_unicode=False):
        for index in range(len(self._payload)):
            yield self._payload[index : index + 1]

    def close(self):
        self.closed = True
        self.close_calls += 1


def sse_client(payload, **kwargs):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return SSEClient(ByteByByteResponse(payload), **kwargs)


def test_events_decoded_byte_by_byte():
    client = sse_client(
        "\ufeff"  # leading BOM is stripped
        ": keep-alive comment\r\n"
        "event: greeting\n"
        "data: 你好\n"
        "data: 🎉\r\n"
        "id: 42\n"
        "retry: 5000\n"
        "\n"
        "data: second\r"  # CR alone terminates the line
        "\r"  # CR alone dispatches the event
        "id:\n"  # bare id resets the cursor to the empty string
        "data: third\n"
        "\n"
        "data: unterminated"  # no trailing blank line: discarded at EOF
    )

    events = list(client)

    assert events == [
        ServerSentEvent(data="你好\n🎉", event="greeting", id="42"),
        ServerSentEvent(data="second", event="message", id="42"),
        ServerSentEvent(data="third", event="message", id=""),
    ]
    assert client.retry == 5000
    assert client.last_event_id == ""


def test_trailing_incomplete_event_is_not_dispatched():
    client = sse_client("data: complete\n\ndata: incomplete\n")
    assert list(client) == [ServerSentEvent(data="complete")]
    # The incomplete trailing event must not move the cursor either way;
    # only a dispatched event's id field would have.
    assert client.last_event_id is None


def test_id_field_semantics():
    client = sse_client(
        "id: 1\n"
        "data: a\n\n"
        "data: b\n\n"  # no id field: cursor unchanged
        "id:\n"
        "data: c\n\n"  # empty id: cursor reset to ""
        "id: has\x00nul\n"  # NUL: field ignored
        "data: d\n\n"
    )
    events = list(client)
    assert [event.id for event in events] == ["1", "1", "", ""]
    assert client.last_event_id == ""


def test_event_type_reset_between_events():
    client = sse_client("event: ping\ndata: a\n\ndata: b\n\n")
    events = list(client)
    assert [event.event for event in events] == ["ping", "message"]


def test_event_without_data_is_not_dispatched():
    client = sse_client("event: nothing\nid: 7\n\ndata: real\n\n")
    events = list(client)
    assert events == [ServerSentEvent(data="real", id="7")]
    # The id field of the undispatched block still updates the cursor.
    assert client.last_event_id == "7"


def test_invalid_retry_fields_are_ignored():
    client = sse_client(
        "retry: abc\n"
        "retry: \n"
        "retry: 12x\n"
        "retry: ²\n"  # non-ASCII digit
        "retry: 250\n"
        "data: x\n\n"
    )
    assert list(client) == [ServerSentEvent(data="x")]
    assert client.retry == 250


def test_field_without_colon_and_unknown_fields():
    client = sse_client(
        "data\n"  # no colon: value is the empty string
        "unknown: ignored\n"
        "data: \n"  # single leading space is stripped
        "\n"
    )
    assert list(client) == [ServerSentEvent(data="\n")]


def test_crlf_split_across_chunks():
    # "\r\n" split between two chunks must terminate a single line.
    payload = b"data: a\r" + b"\nb: not-a-field\n\n"
    chunks = [payload[:8], payload[8:]]

    class SplitResponse(ByteByByteResponse):
        def iter_content(self, chunk_size=1, decode_unicode=False):
            yield from chunks

    client = SSEClient(SplitResponse(b""))
    assert list(client) == [ServerSentEvent(data="a")]


def test_exhausting_the_stream_releases_the_response():
    response = ByteByByteResponse(b"data: x\n\n")
    client = SSEClient(response)
    assert list(client) == [ServerSentEvent(data="x")]
    assert response.closed
    assert response.close_calls == 1


def test_early_stop_releases_the_response():
    response = ByteByByteResponse(b"data: 1\n\ndata: 2\n\ndata: 3\n\n")
    client = SSEClient(response)
    for event in client:
        assert event.data == "1"
        break
    client.close()
    assert response.closed
    assert response.close_calls == 1
    # Closing twice is harmless.
    client.close()
    assert response.close_calls == 1


def test_context_manager_releases_the_response():
    response = ByteByByteResponse(b"data: 1\n\ndata: 2\n\n")
    with SSEClient(response) as client:
        assert next(client).data == "1"
    assert response.closed


def test_close_before_any_iteration_releases_the_response():
    response = ByteByByteResponse(b"data: 1\n\n")
    SSEClient(response).close()
    assert response.closed


def test_real_response_with_buffered_content():
    response = Response()
    response._content = "data: 你好\n\n".encode()
    response._content_consumed = True
    client = SSEClient(response, chunk_size=1)
    assert list(client) == [ServerSentEvent(data="你好")]


def test_last_event_id_is_none_until_first_id_field():
    client = sse_client("data: a\n\n")
    assert client.last_event_id is None
    (event,) = client
    assert event.id is None
