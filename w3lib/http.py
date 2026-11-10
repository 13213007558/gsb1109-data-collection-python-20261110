from __future__ import annotations

import re
from base64 import b64encode
from collections.abc import Mapping, MutableMapping, Sequence
from io import BytesIO
from typing import Any, NamedTuple, TypeAlias, overload
from urllib.parse import unquote_to_bytes

from w3lib._util import to_unicode

HeadersDictInput: TypeAlias = Mapping[bytes, Any | Sequence[bytes]]
HeadersDictOutput: TypeAlias = MutableMapping[bytes, list[bytes]]


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


# --- Content-Disposition filename handling (RFC 6266 / RFC 5987) -----------

# One parameter, read from where the previous one ended. The quoted alternative
# consumes the whole quoted value, so a semicolon inside quotes is never taken
# for a separator; an unfinished quote runs to the end instead of failing.
_DISPOSITION_PARAMETER_RE = re.compile(
    r"""[ \t]*
    (?P<name>[!#$%&'*+\-.^_`|~0-9A-Za-z]+)
    [ \t]*=(?:
        [ \t]*"(?P<quoted>(?:[^"\\]|\\.)*)"
        |[ \t]*(?P<bare>[^;]*)
    )""",
    re.VERBOSE,
)

# An extended value (RFC 5987 section 3.2.1): charset "'" [ language ] "'" value-chars.
_EXTENDED_VALUE_RE = re.compile(
    r"^(?P<charset>[!#$%&'*+\-.^_`|~0-9A-Za-z]+)'"
    r"(?P<language>[A-Za-z0-9\-]*?)'"
    r"(?P<value>[\x21-\x7e]*)$"
)

# Continuation parameter names: "filename*<number>*", numbered from 0 with no
# leading zero. A plain extended parameter is "filename*".
_CONTINUED_NAME_RE = re.compile(r"^(?P<base>[^\*]+)\*(?P<index>0|[1-9][0-9]*)\*$")


class ContentDispositionError(ValueError):
    """A malformed Content-Disposition extended filename parameter.

    ``position`` names the parameter or continuation section that is wrong, so
    callers can report *where* the header stopped being usable rather than only
    that it did. It never points at ordinary filename parameters: those are
    returned as candidates instead of raising.
    """

    def __init__(self, message: str, position: str) -> None:
        super().__init__(f"{message} (at {position})")
        self.position = position


class ContentDispositionFilename(NamedTuple):
    """Filename metadata parsed from a Content-Disposition header.

    Only metadata is produced: nothing is written to disk or used as a path.
    """

    #: Chosen filename, or ``None`` when no usable candidate exists.
    filename: str | None
    #: Why ``filename`` was chosen: ``"extended"`` for a single ``filename*``,
    #: ``"extended-continued"`` for joined ``filename*0*`` ... segments,
    #: ``"basic"`` for the ordinary ``filename`` fallback, or ``None``.
    source: str | None
    #: Ordinary ``filename`` candidate, after quote removal, or ``None``.
    basic: str | None
    #: Decoded extended candidate (``filename*`` or the joined segments),
    #: or ``None`` when the extended parameters are unusable.
    extended: str | None
    #: Every distinct ordinary ``filename`` value seen, in header order.
    candidates: tuple[str, ...]
    #: Extended-parameter problems found while parsing, each tagged with the
    #: position of the offending parameter or continuation section.
    errors: tuple[ContentDispositionError, ...]


def _iter_disposition_parameters(header: str) -> list[tuple[str, str]]:
    """Split a header value into ``(name, value)`` pairs.

    The disposition token (``attachment``, ``inline`` ...) has no ``=`` and so
    is skipped rather than parsed. Quoted values are returned without their
    surrounding quotes and with quoted-pair backslashes removed, but otherwise
    intact, so semicolons inside quotes are not treated as separators.
    """
    parameters: list[tuple[str, str]] = []
    pos = 0
    while pos < len(header):
        char = header[pos]
        if char in " \t;":
            pos += 1
            continue
        match = _DISPOSITION_PARAMETER_RE.match(header, pos)
        if match is None:
            # Unparseable text (e.g. the bare disposition token): skip to the
            # next semicolon.
            next_sep = header.find(";", pos)
            if next_sep == -1:
                break
            pos = next_sep
            continue
        quoted = match.group("quoted")
        if quoted is not None:
            value = re.sub(r"\\(.)", r"\1", quoted)
        else:
            value = match.group("bare").strip()
        parameters.append((match.group("name").lower(), value))
        pos = match.end()
    return parameters


def _parse_extended_value(raw: str, position: str) -> tuple[str, str, str]:
    """Split and decode an RFC 5987 extended value.

    Returns ``(charset, language, decoded_text)``. Raises
    :class:`ContentDispositionError` at ``position`` when the shape, charset or
    percent-encoding is invalid.
    """
    match = _EXTENDED_VALUE_RE.match(raw)
    if match is None:
        raise ContentDispositionError(
            "extended value must be charset'language'value-chars", position
        )
    charset = match.group("charset")
    try:
        text = unquote_to_bytes(match.group("value")).decode(charset)
    except (LookupError, UnicodeDecodeError) as exc:
        raise ContentDispositionError(
            f"extended value cannot be decoded as {charset!r}: {exc}", position
        ) from None
    return charset, match.group("language"), text


def _join_continuation(sections: dict[int, tuple[str, str, str]], base: str) -> str:
    """Join ``index -> (charset, language, text)`` continuation sections.

    Numbering must start at 0 with no gap, and every section must declare the
    same charset (language may differ per section, as RFC 5987 attaches it to
    each one). Raises :class:`ContentDispositionError` otherwise, so a truncated
    or conflicting header is never joined into another file's name.
    """
    indexes = sorted(sections)
    if indexes != list(range(len(indexes))):
        missing = sorted(set(range(max(indexes) + 1)) - set(indexes))
        raise ContentDispositionError(
            f"continuation numbering has a gap (missing indexes: {missing})",
            f"{base}*",
        )
    charset = sections[0][0]
    for index in indexes:
        section_charset = sections[index][0]
        if section_charset != charset:
            raise ContentDispositionError(
                f"charset conflict: section {index} declares "
                f"{section_charset!r}, expected {charset!r}",
                f"{base}*{index}*",
            )
    return "".join(sections[index][2] for index in indexes)


def content_disposition_filename(
    header: str | bytes | None,
    *,
    fallback_to_basic: bool = False,
) -> ContentDispositionFilename:
    """Parse the filename out of a Content-Disposition header value.

    Handles the ordinary ``filename`` parameter, the RFC 5987 extended
    ``filename*`` parameter and the continued ``filename*0*``, ``filename*1*``
    ... family, including non-ASCII (e.g. Chinese) names. Only filename
    metadata is returned; no path is touched on disk.

    A valid extended parameter always takes precedence over the ordinary
    ``filename``. When extended parameters are present but unusable, the
    ordinary name is used only if ``fallback_to_basic`` is ``True``; otherwise
    ``filename`` stays ``None`` and the problem is reported in ``errors``. When
    no extended parameter is present, the ordinary ``filename`` is used
    directly.

    Malformed extended parameters never raise: each is recorded in ``errors``
    with its position, covering duplicate continuation numbers, missing
    sections and conflicting charset declarations. Semicolons inside quoted
    values do not split parameters.

    >>> from w3lib.http import content_disposition_filename
    >>> result = content_disposition_filename(
    ...     "attachment; filename*=utf-8''%E6%8A%A5%E5%91%8A.pdf"
    ... )
    >>> result.filename
    '报告.pdf'
    >>> result.source
    'extended'
    """
    if header is None:
        return ContentDispositionFilename(None, None, None, None, (), ())
    if isinstance(header, bytes):
        header = header.decode("latin-1")

    basic: str | None = None
    candidates: list[str] = []
    extended_single: tuple[str, str] | None = None
    # index -> (charset, language, text) for the filename*<n>* family.
    continued: dict[int, tuple[str, str, str]] = {}
    # A duplicate index makes the whole continuation unusable, so two
    # competing values for the same section can never be joined silently.
    continued_valid = True
    extended_present = False
    errors: list[ContentDispositionError] = []

    for name, raw_value in _iter_disposition_parameters(header):
        if name == "filename":
            if basic is None:
                basic = raw_value
            if raw_value not in candidates:
                candidates.append(raw_value)
            continue

        if name == "filename*":
            extended_present = True
            try:
                _charset, _language, text = _parse_extended_value(
                    raw_value, "filename*"
                )
            except ContentDispositionError as exc:
                errors.append(exc)
            else:
                extended_single = ("extended", text)
            continue

        continued_match = _CONTINUED_NAME_RE.match(name)
        if continued_match and continued_match.group("base") == "filename":
            extended_present = True
            index = int(continued_match.group("index"))
            if index in continued:
                errors.append(
                    ContentDispositionError(
                        f"duplicate continuation index {index}", name
                    )
                )
                continued_valid = False
                continue
            try:
                continued[index] = _parse_extended_value(raw_value, name)
            except ContentDispositionError as exc:
                errors.append(exc)

    extended: str | None = None
    source: str | None = None
    if extended_single is not None:
        source, extended = extended_single
    elif continued and continued_valid:
        try:
            extended = _join_continuation(continued, "filename")
        except ContentDispositionError as exc:
            errors.append(exc)
        else:
            source = "extended-continued"

    filename = extended
    if (
        extended is None
        and basic is not None
        and (fallback_to_basic or not extended_present)
    ):
        filename, source = basic, "basic"

    return ContentDispositionFilename(
        filename=filename,
        source=source,
        basic=basic,
        extended=extended,
        candidates=tuple(candidates),
        errors=tuple(errors),
    )
