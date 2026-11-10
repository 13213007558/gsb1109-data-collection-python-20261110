"""
requests.signing
~~~~~~~~~~~~~~~~

Pluggable request signing.

A signer is a callable supplied by the application. It receives a
:class:`~requests._types.SignatureInput` describing the *exact* request that
will be transmitted (final request target, selected headers and a body
summary) and returns the headers to attach, typically a signature header.

Signing always runs at the end of :meth:`PreparedRequest.prepare`, so the
input can never diverge from what is sent because of percent-encoding,
header merging or body encoding. A new signature is computed on every
preparation, which means redirects, retries and manually re-prepared
requests never reuse a stale signature.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from ._internal_utils import to_native_string
from .exceptions import InvalidHeader, SigningError, UnsignedBodyError
from .structures import CaseInsensitiveDict

if TYPE_CHECKING:
    from . import _types as _t
    from .models import PreparedRequest

#: Header names that are generated per transmission by the transport layer
#: rather than selected by the caller, so they are not part of the signed
#: header block by default. An explicitly provided ``Host`` header is kept.
_EXCLUDED_HEADERS: frozenset[str] = frozenset({"content-length", "transfer-encoding"})

#: Caller-supplied digest channel for one-shot bodies. Consumed (removed)
#: during signing so it is never transmitted as a regular header.
_DIGEST_HEADER = "X-Requests-Content-Digest"


def _body_is_deterministic(body: _t.BodyType) -> bool:
    """Whether a digest of ``body`` can be computed without consuming it.

    Only in-memory bytes/strings are deterministic. Other bodies need either
    a rewindable file object or a caller-provided digest.
    """
    return isinstance(body, (bytes, bytearray, str))


def _body_is_rewindable(body: _t.BodyType) -> bool:
    return all(hasattr(body, name) for name in ("read", "seek", "tell"))


def _body_to_bytes(body: bytes | bytearray | str) -> bytes:
    if isinstance(body, str):
        # urllib3 sends strings encoded as UTF-8, so hash that exact encoding.
        return body.encode("utf-8")
    return bytes(body)


_DIGEST_CHUNK_SIZE = 64 * 1024


def _digest_rewindable_body(request: PreparedRequest, body: object) -> bytes:
    """Hash a seekable body in chunks and rewind it to the start.

    The body object itself stays in place and is streamed by the transport
    as usual; only its position is restored so the bytes digested here are
    exactly the bytes that will be transmitted.
    """
    digest = hashlib.sha256()
    try:
        start = body.tell()  # type: ignore[attr-defined]
        while True:
            chunk = body.read(_DIGEST_CHUNK_SIZE)  # type: ignore[attr-defined]
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            digest.update(chunk)
        body.seek(start)  # type: ignore[attr-defined]
    except OSError as exc:
        raise UnsignedBodyError(
            "The request body could not be rewound to compute a digest "
            "without consuming it. Supply a verifiable body digest via the "
            f"{_DIGEST_HEADER} header instead.",
            request=request,
        ) from exc

    return digest.hexdigest().encode("ascii")


def _validate_provided_digest(
    request: PreparedRequest, digest: str | bytes | None
) -> bytes:
    """Validate a caller-provided digest for a one-shot body.

    The digest is only accepted for fixed-length streams: ``Content-Length``
    plus the digest gives the receiver two independent anchors to verify the
    transmitted body. Chunked streams without a length anchor are refused.
    """
    if digest is None:
        raise UnsignedBodyError(
            "Cannot sign a one-shot, non-rewindable request body without "
            "consuming it. Rewind the body (for example a seekable file) or "
            f"supply a verifiable body digest via the {_DIGEST_HEADER} "
            "header before preparing the request.",
            request=request,
        )

    if isinstance(digest, str):
        digest_bytes = digest.encode("ascii")
    elif isinstance(digest, (bytes, bytearray)):
        digest_bytes = bytes(digest)
    else:
        raise SigningError(
            f"The {_DIGEST_HEADER} value must be a str or bytes.",
            request=request,
        )

    if request.headers.get("Content-Length") is None:
        raise UnsignedBodyError(
            "A caller-provided body digest is only accepted for fixed-length "
            "streams carrying a Content-Length header; chunked bodies cannot "
            "be verified.",
            request=request,
        )
    return digest_bytes


def _selected_headers(
    request: PreparedRequest,
) -> CaseInsensitiveDict[str | bytes]:
    return CaseInsensitiveDict(
        (name, value)
        for name, value in request.headers.items()
        if name.lower() not in _EXCLUDED_HEADERS
    )


def build_signature_input(request: PreparedRequest) -> _t.SignatureInput:
    """Collect the exact, post-encoding content that will be transmitted.

    For in-memory bodies ``body`` holds the exact wire bytes and
    ``body_digest`` holds their SHA-256 hex digest. Seekable file-like bodies
    are hashed in chunks and rewound, so ``body`` stays ``None`` while
    ``body_digest`` is computed from the transmitted content. Truly one-shot
    bodies require a caller-supplied digest.

    :raises UnsignedBodyError: if the body is one-shot and no verifiable
        digest was supplied.
    """
    # The digest channel is internal: always strip it so it is never signed
    # into the header block or transmitted.
    provided_digest = request.headers.pop(_DIGEST_HEADER, None)
    body = request.body
    body_bytes: bytes | None = None
    body_digest: bytes | None = None

    if body is not None:
        if _body_is_deterministic(body):
            body_bytes = _body_to_bytes(cast("bytes | bytearray | str", body))
            body_digest = hashlib.sha256(body_bytes).hexdigest().encode("ascii")
        elif _body_is_rewindable(body):
            body_digest = _digest_rewindable_body(request, body)
        else:
            body_digest = _validate_provided_digest(request, provided_digest)

    return {
        "method": cast(str, request.method),
        "url": cast(str, request.url),
        "path_url": request.path_url,
        "headers": _selected_headers(request),
        "body": body_bytes,
        "body_digest": body_digest,
    }


def _apply_signer_headers(
    request: PreparedRequest, signer_headers: Mapping[str, str | bytes]
) -> None:
    from .utils import check_header_validity

    applied: list[str] = []
    for name, value in signer_headers.items():
        try:
            check_header_validity((name, value))
        except InvalidHeader:
            # Never echo the value: it may contain signature material.
            raise SigningError(
                f"Signer returned an invalid header named {name!r}.",
                request=request,
            ) from None
        header_name = to_native_string(name)
        request.headers[header_name] = value
        applied.append(header_name)
    request._signed_headers = tuple(applied)


def sign_request(request: PreparedRequest, signer: _t.SignerType) -> None:
    """Run ``signer`` against the finalized request and attach its headers.

    The signature is computed from, and attached to, the same
    :class:`PreparedRequest` instance that will be sent, so it cannot be
    computed against a pre-encoding representation or reused after the
    request has been rebuilt for a redirect or re-prepared.
    """
    # Drop any signature produced by an earlier preparation so a re-sign can
    # never silently keep stale material in place. getattr covers prepared
    # requests restored from older pickles without the attribute.
    for header_name in getattr(request, "_signed_headers", ()):
        request.headers.pop(header_name, None)
    request._signed_headers = ()

    signature_input = build_signature_input(request)
    signer_headers = signer(signature_input)
    if signer_headers is None:
        return
    if not isinstance(signer_headers, Mapping):
        raise SigningError(
            "A signer must return a mapping of headers or None.",
            request=request,
        )
    _apply_signer_headers(request, signer_headers)
