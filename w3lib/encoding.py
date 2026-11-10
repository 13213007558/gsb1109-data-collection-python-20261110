"""
Functions for handling encoding of web pages
"""

from __future__ import annotations

import codecs
import re
import warnings
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from frostwork.encoding import (
    _SINGLE_BYTE,
    _single_byte_table,
    autodetect_encoding,
    bom_encoding,
    codec_name,
    declared_encoding,
    decode,
    detect_encoding,
    resolve_label,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# The value ends at whitespace, ";", "," or the end of the header. Comma is
# not parameter syntax; stopping the value at "," approximates Fetch's
# "extract a MIME type" for comma-joined duplicate headers, keeping the first
# charset rather than the last valid MIME type's.
_HEADER_ENCODING_RE = re.compile(
    r"(?:^|;)[ \t]*charset="
    r'(?:"([\w-]+)"|([\w-]+))'
    r"(?![^\s;,])",
    re.IGNORECASE,
)
# https://mimesniff.spec.whatwg.org/commit-snapshots/39aa53511b13953d84fef8d4131d6f61d0ccbde6/#parse-a-mime-type
# Parameters are ";"-separated name=value pairs whose value is either a token
# or a quoted-string, and a quoted-string is opaque
# (https://fetch.spec.whatwg.org/commit-snapshots/586cd2a44c2a865b37c166dc0740f3fb8bb220d6/#collect-an-http-quoted-string),
# so it has to be consumed as a whole: a "charset=" written inside one belongs
# to that value and is not a parameter of its own.
_HEADER_PARAMETER_RE = re.compile(
    r"(?:^|;)[ \t]*(?P<name>[^\s;=]+)="
    r'(?:"(?P<quoted>[^"\\]*(?:\\.[^"\\]*)*)"(?![^\s;,])'
    r"|(?P<token>[^;,\s]*))"
)
_ENCODING_LABEL_RE = re.compile(r"[\w-]+")


def _http_content_type_label(content_type: str) -> str | None:
    """The first charset parameter of *content_type* as spelled in the header.

    The first charset whose value is a well-formed label wins, exactly as
    :func:`http_content_type_encoding` picks it. The spelling is kept verbatim
    so diagnostics can show what the server sent, even when the label names no
    encoding of the Encoding Standard; resolution happens at the call site.
    """
    if not content_type:
        return None
    match = _HEADER_ENCODING_RE.search(content_type)
    if not match:
        return None
    # A match inside a quoted-string must be preceded by that string's
    # opening quote, so if no '"' precedes it the fast answer is correct; only
    # otherwise walk the parameters.
    if content_type.find('"', 0, match.start()) < 0:
        return match.group(1) or match.group(2)
    for parameter in _HEADER_PARAMETER_RE.finditer(content_type):
        if parameter.group("name").lower() != "charset":
            continue
        label = parameter.group("quoted")
        if label is None:
            label = parameter.group("token")
        if _ENCODING_LABEL_RE.fullmatch(label):
            return label
    return None


def http_content_type_encoding(content_type: str | None) -> str | None:
    """Extract the encoding in the content-type header

    >>> import w3lib.encoding
    >>> w3lib.encoding.http_content_type_encoding("Content-Type: text/html; charset=ISO-8859-4")
    'iso8859-4'

    """

    if content_type:
        label = _http_content_type_label(content_type)
        if label is not None:
            return resolve_encoding(label)

    return None


def html_body_declared_encoding(html_body_str: str | bytes) -> str | None:
    '''Return the encoding specified in meta tags in the html body,
    or ``None`` if no suitable encoding was found

    >>> import w3lib.encoding
    >>> w3lib.encoding.html_body_declared_encoding(
    ... """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"
    ...      "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
    ... <html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
    ... <head>
    ...     <title>Some title</title>
    ...     <meta http-equiv="content-type" content="text/html;charset=utf-8" />
    ... </head>
    ... <body>
    ... ...
    ... </body>
    ... </html>""")
    'utf-8'
    >>>

    '''

    if isinstance(html_body_str, str):
        html_body_str = html_body_str.encode("utf-8", "replace")
    name = declared_encoding(html_body_str)
    return None if name is None else codec_name(name)


_DEPRECATED_ENCODING_TRANSLATION = {
    "ascii": "cp1252",
    "big5": "big5hkscs",
    "euc_kr": "cp949",
    "gb2312": "gb18030",
    "gb_2312_80": "gb18030",
    "gbk": "gb18030",
    "iso8859_11": "cp874",
    "iso8859_9": "cp1254",
    "latin_1": "cp1252",
    "macintosh": "mac_roman",
    "shift_jis": "cp932",
    "tis_620": "cp874",
    "win_1251": "cp1251",
    "windows_31j": "cp932",
    "win_31j": "cp932",
    "windows_874": "cp874",
    "win_874": "cp874",
    "x_sjis": "cp932",
    "zh_cn": "gb18030",
}


def __getattr__(name: str) -> Any:
    if name == "DEFAULT_ENCODING_TRANSLATION":
        warnings.warn(
            "w3lib.encoding.DEFAULT_ENCODING_TRANSLATION is deprecated.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _DEPRECATED_ENCODING_TRANSLATION
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@lru_cache(maxsize=256)
def resolve_encoding(encoding_alias: str) -> str | None:
    """Return the Python codec name of the encoding of the `Encoding
    Standard`_ that `encoding_alias` maps to, or ``None`` if there is none,
    e.g. for ``utf-32``.

    .. _Encoding Standard: https://encoding.spec.whatwg.org/

    >>> import w3lib.encoding
    >>> w3lib.encoding.resolve_encoding('latin1')
    'cp1252'
    >>> w3lib.encoding.resolve_encoding('gb_2312-80')
    'gbk'
    >>> w3lib.encoding.resolve_encoding('base64')
    >>>

    """
    return codec_name(encoding_alias)


def read_bom(data: bytes) -> tuple[None, None] | tuple[str, bytes]:
    r"""Read the byte order mark in the text, if present, and
    return the encoding represented by the BOM and the BOM.

    If no BOM can be detected, ``(None, None)`` is returned.

    As in browsers, a UTF-32LE BOM reads as a UTF-16LE one, and a UTF-16 body
    that starts with an XML declaration and no BOM gives an empty BOM.

    >>> import w3lib.encoding
    >>> w3lib.encoding.read_bom(b'\xfe\xff\x6c\x34')
    ('utf-16-be', b'\xfe\xff')
    >>> w3lib.encoding.read_bom(b'\xff\xfe\x34\x6c')
    ('utf-16-le', b'\xff\xfe')
    >>> w3lib.encoding.read_bom(b'\x01\x02\x03\x04')
    (None, None)
    >>>

    """
    bom = bom_encoding(data)
    if bom is None:
        return None, None
    name, length = bom
    return cast("str", codec_name(name)), data[:length]


def to_unicode(data_str: bytes, encoding: str) -> str:
    r"""Convert a str object to unicode using the encoding given

    If *encoding* names an encoding of the `Encoding Standard`_, *data_str* is
    decoded the way browsers decode it, e.g. ``utf-16`` is UTF-16LE.

    Characters that cannot be converted will be converted to ``\ufffd`` (the
    unicode replacement character).

    .. _Encoding Standard: https://encoding.spec.whatwg.org/
    """
    try:
        return decode(data_str, encoding)
    except LookupError:
        return data_str.decode(encoding, "replace")


# Encoding diagnostics ----------------------------------------------------

_ENCODING_SOURCE_ORDER: tuple[str, ...] = ("bom", "http", "meta", "autodetect")

_RECORDING_ERROR_HANDLER = "w3lib_encoding_diagnostics"
_recorded_decode_ranges: ContextVar[list[tuple[int, int]] | None] = ContextVar(
    "_recorded_decode_ranges", default=None
)


def _record_decode_error(error: UnicodeError) -> tuple[str, int]:
    decode_error = cast("UnicodeDecodeError", error)
    records = _recorded_decode_ranges.get()
    if records is not None:
        records.append((decode_error.start, decode_error.end))
    return "\ufffd", decode_error.end


codecs.register_error(_RECORDING_ERROR_HANDLER, _record_decode_error)


@dataclass(frozen=True)
class EncodingCandidate:
    """One encoding a detection source offered.

    A source that found nothing is still listed, with ``label`` and
    ``encoding`` set to ``None``.

    Attributes:
        source: ``"bom"``, ``"http"``, ``"meta"`` or ``"autodetect"``.
        label: The spelling the source used (a BOM or autodetection use the
            WHATWG name), or ``None`` when the source offered nothing.
        encoding: The Python codec name the label resolves to, or ``None``
            when the label names no encoding of the Encoding Standard.
        position: The ``(start, end)`` byte range of the source in the body;
            only the BOM has one, everything else is ``(0, 0)``.
        chosen: Whether :func:`html_to_unicode` decoded with this candidate.
    """

    source: str
    label: str | None
    encoding: str | None
    position: tuple[int, int]
    chosen: bool


@dataclass(frozen=True)
class EncodingConflict:
    """A present candidate that lost to the chosen encoding."""

    source: str
    label: str
    encoding: str


@dataclass(frozen=True)
class DecodeError:
    """A byte range the chosen decoder turned into U+FFFD.

    A U+FFFD the input bytes encode legitimately is never reported here.
    """

    start: int
    end: int
    data: bytes

    @property
    def byte_range(self) -> tuple[int, int]:
        """The half-open ``(start, end)`` byte range in the original body."""
        return self.start, self.end


@dataclass(frozen=True)
class HtmlEncodingDiagnostics:
    """How the encoding of a body was decided, and what decoding could not read.

    Attributes:
        encoding: The Python codec name the body was actually decoded with.
            On the normal detection paths this equals the first element of the
            :func:`html_to_unicode` tuple; on the deprecated
            ``default_encoding``/``auto_detect_fun`` paths it is the codec
            that label resolves to, which the legacy tuple keeps verbatim.
        bom_length: Number of BOM bytes stripped before decoding (``0`` when
            there was no BOM).
        candidates: Every detection source in :attr:`order`, present or not.
        order: The sources in the precedence order they are tried in.
        conflicts: Present candidates whose encoding differs from
            :attr:`encoding`.
        errors: Byte ranges of the input that became U+FFFD, with offsets
            into the original body (after the BOM, when one was stripped).
            Byte-exact for UTF-8, UTF-16, every single-byte encoding and the
            GBK/gb18030 family; for the remaining legacy multi-byte encodings
            the boundaries follow the equivalent Python codec, because the
            underlying browser decoder reports no byte offsets. A U+FFFD the
            bytes themselves encode is never included.
    """

    encoding: str
    bom_length: int
    candidates: tuple[EncodingCandidate, ...]
    order: tuple[str, ...]
    conflicts: tuple[EncodingConflict, ...]
    errors: tuple[DecodeError, ...]


def _single_byte_error_ranges(
    body: bytes, whatwg_name: str
) -> list[tuple[int, int]]:
    """Ranges of *body* undefined in the WHATWG single-byte decoder.

    The same table :func:`frostwork.encoding.decode` decodes with, so every
    reported byte is a U+FFFD in the returned text and nothing else is.
    """
    table = _single_byte_table(whatwg_name)
    return [(index, index + 1) for index, byte in enumerate(body) if table[byte] == "\ufffd"]


def _codec_error_ranges(body: bytes, codec: str) -> list[tuple[int, int]]:
    """Ranges *codec* replaces with U+FFFD when decoding *body*.

    One decode pass fills the ranges through a registered error handler, so
    the input is read once and a U+FFFD the bytes themselves encode is never
    recorded: it is ordinary output, not a decoder error.
    """
    records: list[tuple[int, int]] = []
    token = _recorded_decode_ranges.set(records)
    try:
        body.decode(codec, _RECORDING_ERROR_HANDLER)
    except LookupError:
        return []
    finally:
        _recorded_decode_ranges.reset(token)
    return records


# The WHATWG GBK decoder is the gb18030 decoder (the two decode every byte
# sequence identically), so its error ranges come from Python's gb18030, not
# its narrower gbk codec. The encoding reported to callers stays the resolved
# codec name; this only picks the codec that reproduces the real output.
_RANGE_CODEC_OVERRIDES = {"GBK": "gb18030"}


def _decode_error_ranges(
    body: bytes, whatwg_name: str | None, codec: str
) -> list[tuple[int, int]]:
    if whatwg_name is not None and whatwg_name in _SINGLE_BYTE:
        return _single_byte_error_ranges(body, whatwg_name)
    range_codec = _RANGE_CODEC_OVERRIDES.get(whatwg_name or "", codec)
    return _codec_error_ranges(body, range_codec)


def _candidate(
    source: str,
    label: str | None,
    whatwg_name: str | None,
    position: tuple[int, int],
    chosen_encoding: str,
) -> EncodingCandidate:
    encoding = None if whatwg_name is None else codec_name(whatwg_name)
    return EncodingCandidate(
        source=source,
        label=label,
        encoding=encoding,
        position=position,
        chosen=encoding == chosen_encoding and encoding is not None,
    )


def _build_diagnostics(
    content_type_header: str | None,
    body: bytes,
    bom: tuple[str, int] | None,
    chosen_whatwg_name: str | None,
    chosen_codec: str,
    decoded_body: bytes,
    *,
    url: str | None,
) -> HtmlEncodingDiagnostics:
    bom_whatwg = bom[0] if bom is not None else None
    bom_length = bom[1] if bom is not None else 0
    http_label = _http_content_type_label(content_type_header or "")
    meta_whatwg = declared_encoding(body)
    autodetect_whatwg = autodetect_encoding(body, url=url)

    candidates = (
        _candidate(
            "bom",
            bom_whatwg,
            bom_whatwg,
            (0, bom_length),
            chosen_codec,
        ),
        _candidate(
            "http",
            http_label,
            http_label,
            (0, 0),
            chosen_codec,
        ),
        _candidate(
            "meta",
            meta_whatwg,
            meta_whatwg,
            (0, 0),
            chosen_codec,
        ),
        _candidate(
            "autodetect",
            autodetect_whatwg,
            autodetect_whatwg,
            (0, 0),
            chosen_codec,
        ),
    )
    conflicts = tuple(
        EncodingConflict(
            source=candidate.source,
            label=candidate.label,
            encoding=candidate.encoding,
        )
        for candidate in candidates
        if candidate.encoding is not None
        and candidate.label is not None
        and candidate.encoding != chosen_codec
    )
    ranges = _decode_error_ranges(decoded_body, chosen_whatwg_name, chosen_codec)
    errors = tuple(
        DecodeError(
            start=start + bom_length,
            end=end + bom_length,
            data=body[start + bom_length : end + bom_length],
        )
        for start, end in ranges
    )
    return HtmlEncodingDiagnostics(
        encoding=chosen_codec,
        bom_length=bom_length,
        candidates=candidates,
        order=_ENCODING_SOURCE_ORDER,
        conflicts=conflicts,
        errors=errors,
    )


def html_encoding(
    content_type_header: str | None, html_body_str: bytes, *, url: str | None = None
) -> str:
    """Return the encoding :func:`html_to_unicode` would decode
    *html_body_str* with, given the same *url*, without decoding it.

    .. versionadded:: 2.6.0

    >>> import w3lib.encoding
    >>> w3lib.encoding.html_encoding(None, b'<meta charset="latin1">')
    'cp1252'
    """
    enc = http_content_type_encoding(content_type_header)
    return cast("str", codec_name(detect_encoding(html_body_str, enc, url=url)))


@overload
def html_to_unicode(
    content_type_header: str | None,
    html_body_str: bytes,
    default_encoding: str | None = None,
    auto_detect_fun: Callable[[bytes], str | None] | None = None,
    *,
    url: str | None = None,
    diagnostics: Literal[False] = False,
) -> tuple[str, str]: ...


@overload
def html_to_unicode(
    content_type_header: str | None,
    html_body_str: bytes,
    default_encoding: str | None = None,
    auto_detect_fun: Callable[[bytes], str | None] | None = None,
    *,
    url: str | None = None,
    diagnostics: Literal[True],
) -> tuple[str, str, HtmlEncodingDiagnostics]: ...


def html_to_unicode(
    content_type_header: str | None,
    html_body_str: bytes,
    default_encoding: str | None = None,
    auto_detect_fun: Callable[[bytes], str | None] | None = None,
    *,
    url: str | None = None,
    diagnostics: bool = False,
) -> tuple[str, str] | tuple[str, str, HtmlEncodingDiagnostics]:
    r'''Convert raw html bytes to unicode

    .. versionadded:: 2.6.0
       The *url* parameter.

    This finds the encoding of the html body the way a web browser does,
    trying in order:

    * BOM (byte-order mark)
    * http content type header
    * meta or xml tag declarations
    * UTF-8, if the body is valid UTF-8
    * the encoding guessed from the bytes of the body and the top-level domain
      of *url*, the URL of the body, as Firefox does

    If a BOM is found, it will be stripped.

    If `content_type_header` is not present, ``None`` can be passed signifying
    that the header was not present.

    This method will not fail, if characters cannot be converted to unicode,
    ``\ufffd`` (the unicode replacement character) will be inserted instead.

    Returns a tuple of ``(<encoding used>, <unicode_string>)``. With
    ``diagnostics=True`` it returns an :class:`HtmlEncodingDiagnostics` as a
    third element: the candidate each source offered, the precedence order,
    the conflicts and the input byte ranges that actually became U+FFFD. A
    U+FFFD the body itself encodes is ordinary output and is never listed.
    The body is the in-memory ``bytes`` object passed in; the extra
    per-source scans run only when diagnostics are requested, nothing is
    re-fetched or re-consumed, and the error ranges come from a single extra
    in-memory decode with a recording error handler.

    Examples:

    >>> import w3lib.encoding
    >>> w3lib.encoding.html_to_unicode(None,
    ... b"""<!DOCTYPE html>
    ... <head>
    ... <meta charset="UTF-8" />
    ... <meta name="viewport" content="width=device-width" />
    ... <title>Creative Commons France</title>
    ... <link rel='canonical' href='http://creativecommons.fr/' />
    ... <body>
    ... <p>Creative Commons est une organisation \xc3\xa0 but non lucratif
    ... qui a pour dessein de faciliter la diffusion et le partage des oeuvres
    ... tout en accompagnant les nouvelles pratiques de cr\xc3\xa9ation \xc3\xa0 l\xe2\x80\x99\xc3\xa8re numerique.</p>
    ... </body>
    ... </html>""")
    ('utf-8', '<!DOCTYPE html>\n<head>\n<meta charset="UTF-8" />\n<meta name="viewport" content="width=device-width" />\n<title>Creative Commons France</title>\n<link rel=\'canonical\' href=\'http://creativecommons.fr/\' />\n<body>\n<p>Creative Commons est une organisation \xe0 but non lucratif\nqui a pour dessein de faciliter la diffusion et le partage des oeuvres\ntout en accompagnant les nouvelles pratiques de cr\xe9ation \xe0 l\u2019\xe8re numerique.</p>\n</body>\n</html>')
    >>>

    '''
    bom = bom_encoding(html_body_str)
    if bom is not None:
        whatwg_name, bom_length = bom
        encoding = cast("str", codec_name(whatwg_name))
        decoded_body = html_body_str[bom_length:]
        text = decode(decoded_body, whatwg_name)
        if not diagnostics:
            return encoding, text
        report = _build_diagnostics(
            content_type_header,
            html_body_str,
            bom,
            whatwg_name,
            encoding,
            decoded_body,
            url=url,
        )
        return encoding, text, report

    enc = http_content_type_encoding(content_type_header)
    if auto_detect_fun is None and default_encoding is None:
        whatwg_name = detect_encoding(html_body_str, enc, url=url)
        encoding = cast("str", codec_name(whatwg_name))
        text = decode(html_body_str, whatwg_name)
        if not diagnostics:
            return encoding, text
        report = _build_diagnostics(
            content_type_header,
            html_body_str,
            None,
            whatwg_name,
            encoding,
            html_body_str,
            url=url,
        )
        return encoding, text, report

    warnings.warn(
        "The default_encoding and auto_detect_fun parameters of "
        "w3lib.encoding.html_to_unicode() are deprecated.",
        DeprecationWarning,
        stacklevel=2,
    )
    if enc is None:
        enc = html_body_declared_encoding(html_body_str)
    if enc is None and auto_detect_fun is not None:
        enc = auto_detect_fun(html_body_str)
    if enc is None:
        enc = default_encoding or "utf8"
    text = to_unicode(html_body_str, enc)
    if not diagnostics:
        return enc, text
    deprecated_whatwg = resolve_label(enc)
    deprecated_codec = codec_name(enc) or enc
    report = _build_diagnostics(
        content_type_header,
        html_body_str,
        None,
        deprecated_whatwg,
        deprecated_codec,
        html_body_str,
        url=url,
    )
    return enc, text, report
