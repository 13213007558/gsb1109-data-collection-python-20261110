from __future__ import annotations

from base64 import b64encode
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from io import BytesIO
from typing import Any, TypeAlias, overload

from w3lib._util import to_unicode

HeadersDictInput: TypeAlias = Mapping[bytes, Any | Sequence[bytes]]
HeadersDictOutput: TypeAlias = MutableMapping[bytes, list[bytes]]

# RFC 9110, Section 5.6.2 (token characters)
_TOKEN_CHARS = frozenset(
    b"!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


def _is_valid_header_name(name: bytes) -> bool:
    return bool(name) and all(char in _TOKEN_CHARS for char in name)


@overload
def headers_raw_to_dict(headers_raw: bytes) -> HeadersDictOutput: ...


@overload
def headers_raw_to_dict(headers_raw: None) -> None: ...


def headers_raw_to_dict(headers_raw: bytes | None) -> HeadersDictOutput | None:
    r"""
    Convert raw headers (single multi-line bytestring)
    to a dictionary.

    For example:

    >>> import w3lib.http
    >>> w3lib.http.headers_raw_to_dict(b"Content-type: text/html\n\rAccept: gzip\n\n")   # doctest: +SKIP
    {'Content-type': ['text/html'], 'Accept': ['gzip']}

    Incorrect input:

    >>> w3lib.http.headers_raw_to_dict(b"Content-typt gzip\n\n")
    {}
    >>>

    Argument is ``None`` (return ``None``):

    >>> w3lib.http.headers_raw_to_dict(None)
    >>>

    """

    if headers_raw is None:
        return None

    if not headers_raw:
        return {}

    result_dict: HeadersDictOutput = {}

    for header in BytesIO(headers_raw):
        key, sep, value = header.partition(b":")
        if not sep:
            continue

        key, value = key.strip(), value.strip()

        if key in result_dict:
            result_dict[key].append(value)
        else:
            result_dict[key] = [value]

    return result_dict


@overload
def headers_dict_to_raw(headers_dict: HeadersDictInput) -> bytes: ...


@overload
def headers_dict_to_raw(headers_dict: None) -> None: ...


def headers_dict_to_raw(headers_dict: HeadersDictInput | None) -> bytes | None:
    r"""
    Returns a raw HTTP headers representation of headers

    For example:

    >>> import w3lib.http
    >>> w3lib.http.headers_dict_to_raw({b'Content-type': b'text/html', b'Accept': b'gzip'}) # doctest: +SKIP
    'Content-type: text/html\\r\\nAccept: gzip'
    >>>

    Note that keys and values must be bytes.

    Argument is ``None`` (returns ``None``):

    >>> w3lib.http.headers_dict_to_raw(None)
    >>>

    """

    if headers_dict is None:
        return None

    if not headers_dict:
        return b""

    parts = bytearray()

    for key, value in headers_dict.items():
        if isinstance(value, bytes):
            if parts:
                parts.extend(b"\r\n")
            parts.extend(key + b": " + value)

        elif isinstance(value, (list, tuple)):
            for v in value:
                if parts:
                    parts.extend(b"\r\n")
                parts.extend(key + b": " + v)

    return bytes(parts)


def basic_auth_header(
    username: str | bytes, password: str | bytes, encoding: str = "ISO-8859-1"
) -> bytes:
    """
    Return an `Authorization` header field value for `HTTP Basic Access Authentication (RFC 2617)`_

    >>> import w3lib.http
    >>> w3lib.http.basic_auth_header('someuser', 'somepass')
    b'Basic c29tZXVzZXI6c29tZXBhc3M='

    .. _HTTP Basic Access Authentication (RFC 2617): http://www.ietf.org/rfc/rfc2617.txt

    """

    auth = f"{to_unicode(username)}:{to_unicode(password)}"
    # RFC 2617 doesn't define encoding, but ISO-8859-1 seems to be the
    # most widely used encoding here. See also:
    # http://greenbytes.de/tech/webdav/draft-ietf-httpauth-basicauth-enc-latest.html
    return b"Basic " + b64encode(auth.encode(encoding=encoding))


class _HeaderField:
    """One parsed header field, keeping its original raw lines."""

    __slots__ = ("_new_value", "ending", "folded", "lines", "name", "value")

    def __init__(
        self,
        lines: list[bytes],
        name: bytes,
        value: bytes,
        *,
        folded: bool = False,
        ending: bytes = b"\r\n",
    ) -> None:
        self.lines = lines
        self.name = name
        self.value = value
        self.folded = folded
        self.ending = ending
        self._new_value: bytes | None = None

    @property
    def edited(self) -> bool:
        return self._new_value is not None

    def replace(self, value: bytes) -> None:
        self._new_value = value

    def render(self) -> bytes:
        if self._new_value is None:
            return b"".join(self.lines)
        return self.name + b": " + self._new_value + self.ending


def _line_ending(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return b"\r\n"
    if line.endswith(b"\n"):
        return b"\n"
    if line.endswith(b"\r"):
        return b"\r"
    return b""


class RawHeaders:
    r"""Lossless view of a raw HTTP header block with surgical editing.

    Unlike :func:`headers_raw_to_dict`, this class keeps the original
    bytes of every line, so duplicate fields (e.g. ``Set-Cookie``), the
    original field-name spelling, field order, whitespace and line
    endings are all preserved. Field-name lookup is case-insensitive.

    Only fields touched through :meth:`set` or :meth:`delete` change
    their bytes when exporting with :meth:`to_bytes`; everything else is
    emitted verbatim:

    >>> raw = b"Server: nginx\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\n\r\n"
    >>> headers = RawHeaders(raw)
    >>> headers.get_all(b"set-cookie")
    [b'a=1', b'b=2']
    >>> headers.set(b"Set-Cookie", b"session=xyz")
    >>> headers.to_bytes()
    b'Server: nginx\r\nSet-Cookie: session=xyz\r\n\r\n'

    Obsolete line folding (obs-fold) is readable but flagged: the value
    is unfolded and the field is reported by :meth:`folded_fields`.
    Lines that cannot be parsed as a header field are never merged into
    the previous field; they are kept verbatim and reported by
    :meth:`invalid_lines`.
    """

    def __init__(self, data: bytes = b"") -> None:
        # Each entry is either a _HeaderField or a raw bytes chunk
        # (invalid line, blank separator or trailing data) kept verbatim.
        self._entries: list[_HeaderField | bytes] = []
        self._parse(data)

    def _parse(self, data: bytes) -> None:
        if not data:
            return
        chunks = data.split(b"\n")
        lines = [chunk + b"\n" for chunk in chunks[:-1]]
        if chunks[-1]:
            lines.append(chunks[-1])
        current: _HeaderField | None = None
        in_tail = False
        for line in lines:
            if in_tail:
                self._entries.append(line)
                continue
            content = line.rstrip(b"\r\n")
            if not content:
                # Blank line: end of the header section.
                in_tail = True
                current = None
                self._entries.append(line)
                continue
            if content[:1] in (b" ", b"\t"):
                if current is None:
                    # Orphan continuation line: keep verbatim, do not
                    # attach it to anything.
                    self._entries.append(line)
                    continue
                current.lines.append(line)
                current.folded = True
                current.value += b" " + content.strip()
                continue
            name, sep, value = content.partition(b":")
            name = name.strip()
            if not sep or not _is_valid_header_name(name):
                # Invalid line: keep verbatim, never merge it into the
                # previous field.
                current = None
                self._entries.append(line)
                continue
            current = _HeaderField(
                [line], name, value.strip(), ending=_line_ending(line)
            )
            self._entries.append(current)

    def _fields(self, name: bytes) -> list[_HeaderField]:
        key = name.lower()
        return [
            entry
            for entry in self._entries
            if isinstance(entry, _HeaderField) and entry.name.lower() == key
        ]

    def __contains__(self, name: bytes) -> bool:
        return bool(self._fields(name))

    def __len__(self) -> int:
        return sum(isinstance(entry, _HeaderField) for entry in self._entries)

    def __iter__(self) -> Iterator[tuple[bytes, bytes]]:
        return iter(self.items())

    def items(self) -> list[tuple[bytes, bytes]]:
        """Return ``(name, value)`` pairs in order, keeping duplicates and
        the original field-name spelling."""
        return [
            (entry.name, entry.value)
            for entry in self._entries
            if isinstance(entry, _HeaderField)
        ]

    def get(self, name: bytes, default: bytes | None = None) -> bytes | None:
        """Return the first value for ``name`` (case-insensitive)."""
        fields = self._fields(name)
        return fields[0].value if fields else default

    def get_all(self, name: bytes) -> list[bytes]:
        """Return all values for ``name`` (case-insensitive)."""
        return [field.value for field in self._fields(name)]

    def set(self, name: bytes, value: bytes) -> None:
        """Replace every occurrence of ``name`` (case-insensitive) with a
        single field holding ``value``, at the position of the first
        occurrence. Append the field if absent.

        The original field-name spelling and line ending of the first
        occurrence are kept. ``value`` must not contain CR or LF, to
        prevent header injection.
        """
        if not _is_valid_header_name(name):
            msg = f"Invalid header name: {name!r}"
            raise ValueError(msg)
        if b"\r" in value or b"\n" in value:
            msg = f"Header value must not contain CR or LF: {value!r}"
            raise ValueError(msg)
        fields = self._fields(name)
        if fields:
            fields[0].replace(value)
            for field in fields[1:]:
                self._entries.remove(field)
            return
        ending = b"\r\n"
        for entry in reversed(self._entries):
            if isinstance(entry, _HeaderField) and entry.ending:
                ending = entry.ending
                break
        field = _HeaderField([], name, value, ending=ending)
        field.replace(value)
        self._entries.append(field)

    def delete(self, name: bytes) -> None:
        """Remove every occurrence of ``name`` (case-insensitive)."""
        for field in self._fields(name):
            self._entries.remove(field)

    @property
    def folded_fields(self) -> list[bytes]:
        """Names of fields that used obsolete line folding (obs-fold)."""
        return [
            entry.name
            for entry in self._entries
            if isinstance(entry, _HeaderField) and entry.folded
        ]

    @property
    def invalid_lines(self) -> list[bytes]:
        """Raw lines that could not be parsed as header fields. They are
        kept verbatim in the output and never merged into other fields."""
        return [entry for entry in self._entries if isinstance(entry, bytes)]

    def to_bytes(self) -> bytes:
        """Export the header block. Unedited parts are byte-identical to
        the original input."""
        return b"".join(
            entry.render() if isinstance(entry, _HeaderField) else entry
            for entry in self._entries
        )

    def to_dict(self) -> HeadersDictOutput:
        """Return a dictionary view compatible with
        :func:`headers_raw_to_dict`."""
        result: HeadersDictOutput = {}
        for entry in self._entries:
            if isinstance(entry, _HeaderField):
                result.setdefault(entry.name, []).append(entry.value)
        return result
