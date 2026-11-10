"""
requests.sse
~~~~~~~~~~~~

Server-Sent Events (SSE, ``text/event-stream``) consumption support.

Simple line-based iteration (e.g. :meth:`Response.iter_lines`) is not
enough to consume an event stream: a single event is built from several
lines, lines may be terminated by ``\\r\\n``, ``\\r`` or ``\\n``, and
multi-byte UTF-8 characters may be split across arbitrary network
chunks. :class:`SSEClient` implements the event stream interpretation
described by the WHATWG HTML standard.

Reconnection is deliberately left to the caller. After the stream ends
(or fails), issue a new request with the ``Last-Event-ID`` header set to
:attr:`SSEClient.last_event_id` (when it is not ``None``) and wait
:attr:`SSEClient.retry` milliseconds (when set) before reconnecting.
"""

from __future__ import annotations

import codecs
from collections.abc import Generator, Iterator
from typing import TYPE_CHECKING, NamedTuple

from .models import ITER_CHUNK_SIZE

if TYPE_CHECKING:
    from .models import Response

__all__ = ["ServerSentEvent", "SSEClient"]


class ServerSentEvent(NamedTuple):
    """A single dispatched Server-Sent Event.

    :ivar data: The event payload. Multiple ``data`` fields are joined
        with ``"\\n"``.
    :ivar event: The event type, ``"message"`` when the event block did
        not carry an ``event`` field.
    :ivar id: The last event ID string at dispatch time. ``None`` when
        the stream never set one; an empty string means the server
        explicitly reset the ID with a bare ``id:`` field.
    """

    data: str
    event: str = "message"
    id: str | None = None


class SSEClient(Iterator[ServerSentEvent]):
    """Iterate over the events of a ``text/event-stream`` response.

    :param response: A streaming :class:`Response <requests.Response>`
        (i.e. obtained with ``stream=True``).
    :param chunk_size: Number of bytes read from the socket per
        iteration. This only affects buffering, not event boundaries.

    The client is an iterator of :class:`ServerSentEvent` and also a
    context manager. Closing it (explicitly, via ``with``, or by
    exhausting the stream) releases the underlying response::

        with requests.get(url, stream=True) as r:
            client = SSEClient(r)
            for event in client:
                print(event.data)
            resume_from = client.last_event_id
            wait_ms = client.retry

    .. attribute:: last_event_id

        The last event ID string delivered by the stream, or ``None``
        if no ``id`` field has been seen. Suitable for the
        ``Last-Event-ID`` request header when reconnecting.

    .. attribute:: retry

        The reconnection delay in milliseconds most recently suggested
        by the server via a ``retry`` field, or ``None``.
    """

    def __init__(self, response: Response, chunk_size: int = ITER_CHUNK_SIZE):
        self._response = response
        self._chunk_size = chunk_size
        self.last_event_id: str | None = None
        self.retry: int | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._bom_checked = False
        self._skip_lf = False
        self._line_buffer: list[str] = []
        self._data_buffer: list[str] = []
        self._event_type = ""
        self._events = self._parse()
        self._closed = False
        self._released = False

    def __iter__(self) -> SSEClient:
        return self

    def __next__(self) -> ServerSentEvent:
        return next(self._events)

    def __enter__(self) -> SSEClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop consuming the stream and release the response."""
        if self._closed:
            return
        self._closed = True
        self._events.close()
        self._release()

    def _release(self) -> None:
        if not self._released:
            self._released = True
            self._response.close()

    def _parse(self) -> Generator[ServerSentEvent, None, None]:
        try:
            for chunk in self._response.iter_content(chunk_size=self._chunk_size):
                if isinstance(chunk, str):
                    text = chunk
                else:
                    text = self._decoder.decode(chunk)
                yield from self._feed(text)
            # Flush any bytes held back by the incremental decoder, then
            # drop whatever is left: a line without a terminator and an
            # event block without its terminating blank line are
            # incomplete and must not be dispatched.
            yield from self._feed(self._decoder.decode(b"", final=True))
        finally:
            self._closed = True
            self._release()

    def _feed(self, text: str) -> Generator[ServerSentEvent, None, None]:
        for char in text:
            if not self._bom_checked:
                self._bom_checked = True
                if char == "\ufeff":
                    continue
            if self._skip_lf:
                self._skip_lf = False
                if char == "\n":
                    continue
            if char == "\r":
                self._skip_lf = True
                event = self._process_line("".join(self._line_buffer))
                self._line_buffer.clear()
            elif char == "\n":
                event = self._process_line("".join(self._line_buffer))
                self._line_buffer.clear()
            else:
                self._line_buffer.append(char)
                continue
            if event is not None:
                yield event

    def _process_line(self, line: str) -> ServerSentEvent | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            # Comment line, ignored.
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_buffer.append(value)
            self._data_buffer.append("\n")
        elif field == "event":
            self._event_type = value
        elif field == "id":
            if "\x00" not in value:
                # A bare "id:" resets the last event ID to the empty
                # string; a block without an id field leaves it alone.
                self.last_event_id = value
        elif field == "retry":
            if value and value.isascii() and value.isdigit():
                self.retry = int(value)
        # Unknown fields are ignored.
        return None

    def _dispatch(self) -> ServerSentEvent | None:
        if not self._data_buffer:
            self._event_type = ""
            return None
        data = "".join(self._data_buffer)
        if data.endswith("\n"):
            data = data[:-1]
        event = ServerSentEvent(
            data=data,
            event=self._event_type or "message",
            id=self.last_event_id,
        )
        self._data_buffer.clear()
        self._event_type = ""
        return event
