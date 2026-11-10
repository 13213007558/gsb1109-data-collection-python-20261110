"""
requests.multipart
~~~~~~~~~~~~~~~~~~

This module provides a streaming decoder for ``multipart/*`` response
bodies, such as the ``multipart/mixed`` batches some APIs use to pack
many small documents into a single response.

Parts are produced one at a time from an iterable of byte chunks (for
example ``Response.iter_content()``), so the whole response never has to
be held in memory. Only the nesting levels declared via ``max_depth``
are parsed; deeper multipart bodies are exposed as opaque part content.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from .exceptions import MultipartError, MultipartTruncatedError
from .structures import CaseInsensitiveDict
from .utils import _parse_content_type_header, get_encoding_from_headers

#: Default upper bound for a single part's header block, in bytes.
DEFAULT_MAX_HEADER_SIZE = 64 * 1024

#: Bound on RFC 2046 transport padding after a delimiter, in bytes.
_MAX_TRANSPORT_PADDING = 128

#: Bound on a boundary delimiter, per RFC 2046 (1..70 characters).
_MAX_BOUNDARY_LENGTH = 70

_CRLF = b"\r\n"


def _parse_part_headers(lines: list[bytes]) -> list[tuple[str, str]]:
    """Parses a part header block into an ordered list of name/value pairs.

    Duplicate header names are preserved. Obsolete line folding is
    unfolded; malformed lines without a colon are ignored.
    """
    headers: list[tuple[str, str]] = []
    for line in lines:
        if line[:1] in (b" ", b"\t") and headers:
            name, value = headers[-1]
            headers[-1] = (name, f"{value} {line.decode('latin-1').strip()}")
            continue
        name, sep, value = line.partition(b":")
        if not sep:
            continue
        headers.append(
            (name.decode("latin-1").strip(), value.decode("latin-1").strip())
        )
    return headers


class MultipartPart:
    """A single part of a multipart stream.

    The body is read lazily from the underlying stream: it must be
    consumed (or :meth:`skip`ped) before the owning
    :class:`MultipartReader` is advanced to the next part or closed.
    """

    def __init__(
        self, reader: MultipartReader, raw_headers: list[tuple[str, str]]
    ) -> None:
        self._reader = reader
        #: Ordered ``(name, value)`` pairs exactly as received; duplicate
        #: header names are preserved here.
        self.raw_headers: tuple[tuple[str, str], ...] = tuple(raw_headers)
        #: Case-insensitive view of the part headers. When a header is
        #: repeated, the last value wins; use :attr:`raw_headers` to
        #: access every occurrence.
        self.headers: CaseInsensitiveDict[str] = CaseInsensitiveDict(raw_headers)
        self._body = reader._iter_body()
        self._buf = bytearray()
        self._exhausted = False

    def __repr__(self) -> str:
        return f"<MultipartPart [{self.headers.get('Content-Type', 'unknown')!r}]>"

    @property
    def encoding(self) -> str | None:
        """Charset declared by this part's own ``Content-Type`` header."""
        return get_encoding_from_headers(self.headers)

    def read(self, size: int = -1) -> bytes:
        """Reads up to ``size`` bytes of the part body (all of it by default)."""
        if size is None or size < 0:
            head = bytes(self._buf)
            self._buf.clear()
            return head + b"".join(self._drain())
        if size == 0:
            return b""
        while len(self._buf) < size and not self._exhausted:
            try:
                self._buf += next(self._body)
            except StopIteration:
                self._exhausted = True
        data = bytes(self._buf[:size])
        del self._buf[:size]
        return data

    def iter_chunks(self, chunk_size: int = 8192) -> Iterator[bytes]:
        """Yields the part body in chunks of at most ``chunk_size`` bytes."""
        while True:
            chunk = self.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def skip(self) -> None:
        """Discards the remainder of the part body.

        The reader does this automatically when it is advanced, so the
        next part is always parsed from the correct position.
        """
        for _ in self._drain():
            pass

    @property
    def content(self) -> bytes:
        """The remaining unread body of the part, buffered into memory."""
        return self.read()

    @property
    def text(self) -> str:
        """The remaining unread body, decoded with the part's own charset."""
        content = self.content
        encoding = self.encoding
        if encoding is not None:
            try:
                return content.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                pass
        return content.decode("utf-8", errors="replace")

    def iter_parts(
        self, *, max_header_size: int = DEFAULT_MAX_HEADER_SIZE
    ) -> MultipartReader:
        """Iterates the sub-parts of a nested multipart body.

        Only nesting levels declared by the reader's ``max_depth`` are
        supported; anything deeper is exposed as opaque part content.
        """
        reader = self._reader
        if reader._depth >= reader._max_depth:
            raise MultipartError(
                "nested multipart body exceeds the declared "
                f"max_depth of {reader._max_depth}"
            )
        content_type = self.headers.get("Content-Type", "")
        media_type, params = _parse_content_type_header(content_type)
        boundary = params.get("boundary")
        if not media_type.lower().startswith("multipart/") or boundary is None:
            raise MultipartError(
                f"part is not a multipart body with a boundary: {content_type!r}"
            )
        return MultipartReader(
            self.iter_chunks(),
            boundary,
            max_header_size=max_header_size,
            max_depth=reader._max_depth,
            _depth=reader._depth + 1,
        )

    def _drain(self) -> Iterator[bytes]:
        while not self._exhausted:
            try:
                yield next(self._body)
            except StopIteration:
                self._exhausted = True


class MultipartReader(Iterator[MultipartPart]):
    """Incrementally decodes a ``multipart/*`` byte stream part by part.

    :param stream: iterable of ``bytes`` chunks, e.g. the result of
        ``Response.iter_content()``.
    :param boundary: the delimiter boundary, without the leading ``"--"``.
    :param max_header_size: maximum size in bytes of a single part's
        header block; larger blocks raise :class:`MultipartError`.
    :param max_depth: how many multipart nesting levels may be parsed
        (see :meth:`MultipartPart.iter_parts`).

    The reader is a single-use iterator. Closing it (explicitly, via a
    ``with`` block, or by exhausting it) closes the underlying stream.
    """

    def __init__(
        self,
        stream: Iterable[bytes],
        boundary: str | bytes,
        *,
        max_header_size: int = DEFAULT_MAX_HEADER_SIZE,
        max_depth: int = 1,
        _depth: int = 1,
    ) -> None:
        if isinstance(boundary, str):
            try:
                boundary = boundary.encode("ascii")
            except UnicodeEncodeError:
                raise MultipartError(
                    f"multipart boundary is not ASCII: {boundary!r}"
                ) from None
        if (
            not boundary
            or len(boundary) > _MAX_BOUNDARY_LENGTH
            or b"\r" in boundary
            or b"\n" in boundary
        ):
            raise MultipartError(f"invalid multipart boundary: {boundary!r}")
        if max_header_size <= 0:
            raise ValueError("max_header_size must be a positive integer")
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        self._stream = iter(stream)
        self._delimiter = b"--" + boundary
        self._max_header_size = max_header_size
        self._max_depth = max_depth
        self._depth = _depth
        self._buf = bytearray()
        self._eof = False
        self._closed = False
        self._state = "preamble"  # preamble -> headers -> body -> done
        self._current: MultipartPart | None = None

    def __iter__(self) -> MultipartReader:
        return self

    def __next__(self) -> MultipartPart:
        if self._closed:
            raise StopIteration
        try:
            return self._advance()
        except BaseException:
            # Normal end, truncation, and header errors all finish the
            # stream; release the underlying chunks in every case.
            self.close()
            raise

    def __enter__(self) -> MultipartReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Stops decoding and closes the underlying chunk stream."""
        if self._closed:
            return
        self._closed = True
        self._state = "done"
        stream, self._stream = self._stream, iter(())
        close = getattr(stream, "close", None)
        if close is not None:
            close()

    def _advance(self) -> MultipartPart:
        if self._state == "preamble":
            self._skip_preamble()
        elif self._state == "body" and self._current is not None:
            # The previous part was not fully consumed; discard its
            # remainder so the next part starts at the right offset.
            self._current.skip()
        if self._state == "done":
            raise StopIteration
        raw_headers = self._read_headers()
        part = MultipartPart(self, raw_headers)
        self._current = part
        self._state = "body"
        return part

    def _fill(self) -> bool:
        """Pulls one more chunk into the buffer; returns False at EOF."""
        if self._closed:
            raise MultipartError("multipart reader is closed")
        while not self._eof:
            try:
                chunk = next(self._stream)
            except StopIteration:
                self._eof = True
                return False
            if chunk:
                self._buf += bytes(chunk)
                return True
        return False

    def _classify_delimiter(self, start: int) -> tuple[str | None, int]:
        """Classifies a candidate delimiter starting at ``start``.

        The buffer is known to hold ``self._delimiter`` at ``start``.
        Returns ``("part", end)`` or ``("final", end)`` where ``end`` is
        the offset just past the delimiter line, or ``(None, 0)`` when
        the candidate is boundary-like body text rather than a real
        delimiter.
        """
        pos = start + len(self._delimiter)
        while len(self._buf) - pos < 2 and not self._eof:
            self._fill()
        rest = bytes(self._buf[pos : pos + 2])
        if len(rest) < 2:
            # EOF directly after the boundary. Only a close-delimiter
            # could legally end the stream here, and this is not one.
            if rest in (b"", b"-", b"\r", b" ", b"\t"):
                raise MultipartTruncatedError(
                    "multipart stream ended inside a boundary delimiter"
                )
            return None, 0
        if rest == b"--":
            # Close-delimiter; anything after it is epilogue.
            return "final", pos + 2
        # Transport padding: zero or more SP/TAB, then CRLF.
        padding = pos
        while True:
            while padding < len(self._buf) and self._buf[padding] in (0x20, 0x09):
                padding += 1
                if padding - pos > _MAX_TRANSPORT_PADDING:
                    return None, 0
            if padding < len(self._buf) or self._eof:
                break
            self._fill()
        if padding >= len(self._buf):
            raise MultipartTruncatedError(
                "multipart stream ended inside a boundary delimiter"
            )
        if self._buf[padding : padding + 2] == _CRLF:
            return "part", padding + 2
        return None, 0

    def _skip_preamble(self) -> None:
        """Discards everything up to and including the first delimiter."""
        delimiter = self._delimiter
        marker = _CRLF + delimiter
        # A delimiter at the very start of the stream needs no leading CRLF.
        while len(self._buf) < len(delimiter) and self._fill():
            pass
        if self._buf[: len(delimiter)] == delimiter:
            kind, end = self._classify_delimiter(0)
            if kind is not None:
                del self._buf[:end]
                self._state = "headers" if kind == "part" else "done"
                return
            # Boundary-like junk at the stream start; drop a byte and
            # fall through to the general preamble scan.
            del self._buf[:1]
        while True:
            idx = self._buf.find(marker)
            if idx != -1:
                kind, end = self._classify_delimiter(idx + 2)
                if kind is None:
                    del self._buf[: idx + 2]
                    continue
                del self._buf[:end]
                self._state = "headers" if kind == "part" else "done"
                return
            if self._eof:
                raise MultipartTruncatedError(
                    "multipart stream ended before any boundary was found"
                )
            # Keep a tail long enough for a marker split across chunks.
            keep = len(marker) - 1
            if len(self._buf) > keep:
                del self._buf[:-keep]
            self._fill()

    def _read_headers(self) -> list[tuple[str, str]]:
        """Reads one part's header block, bounded by max_header_size."""
        lines: list[bytes] = []
        size = 0
        while True:
            idx = self._buf.find(_CRLF)
            while idx == -1:
                if len(self._buf) + size > self._max_header_size:
                    raise MultipartError(
                        f"multipart part headers exceed {self._max_header_size} bytes"
                    )
                if self._eof:
                    raise MultipartTruncatedError(
                        "multipart stream ended inside a part header block"
                    )
                self._fill()
                idx = self._buf.find(_CRLF)
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 2]
            if not line:
                return _parse_part_headers(lines)
            size += len(line) + 2
            if size > self._max_header_size:
                raise MultipartError(
                    f"multipart part headers exceed {self._max_header_size} bytes"
                )
            lines.append(line)

    def _iter_body(self) -> Iterator[bytes]:
        """Yields one part's body, stopping at its closing delimiter."""
        marker = _CRLF + self._delimiter
        while True:
            idx = self._buf.find(marker)
            if idx == -1:
                if self._eof:
                    raise MultipartTruncatedError(
                        "multipart stream ended without a closing boundary"
                    )
                # Emit everything except a tail that could still grow
                # into a delimiter split across chunks.
                keep = len(marker) - 1
                if len(self._buf) > keep:
                    chunk = bytes(self._buf[:-keep])
                    del self._buf[:-keep]
                    yield chunk
                self._fill()
                continue
            kind, end = self._classify_delimiter(idx + 2)
            if kind is None:
                # Boundary-like text inside the body, not a delimiter.
                chunk = bytes(self._buf[: idx + 2])
                del self._buf[: idx + 2]
                yield chunk
                continue
            if idx:
                yield bytes(self._buf[:idx])
            del self._buf[:end]
            self._state = "headers" if kind == "part" else "done"
            return
