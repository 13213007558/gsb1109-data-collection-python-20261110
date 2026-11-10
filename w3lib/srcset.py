"""
Parsing of the HTML ``srcset`` attribute and selection of the image
candidate to download for a given viewport, device pixel ratio and
``sizes`` attribute.

Unlike a plain ``img.src`` read, which usually yields a thumbnail, this
module exposes every candidate of a ``srcset`` attribute and implements
the candidate selection of the HTML standard, so that the best available
resource can be requested instead:

>>> from w3lib.srcset import choose_srcset, parse_srcset
>>> srcset = "small.jpg 400w, medium.jpg 800w, large.jpg 1200w"
>>> sizes = "(max-width: 600px) 100vw, 400px"
>>> candidates = parse_srcset(srcset, base_url="https://example.com/")

The same image element picks different resources on different viewports.
On a 375 CSS px phone with a 2x screen the slot is the full 375 px, so
the 800w candidate is the smallest one that still covers 2x:

>>> selection = choose_srcset(candidates, viewport_width=375, device_pixel_ratio=2, sizes=sizes)
>>> selection.candidate.resolved_url
'https://example.com/medium.jpg'
>>> selection.reason
"slot is 375px (sizes '(max-width: 600px) 100vw, 400px' at a 375px viewport); need >= 2x, medium.jpg gives 2.13x (800w), the smallest sufficient candidate"

On a 1280 px desktop at 1x the slot is 400 px and the 400w candidate
already covers it:

>>> selection = choose_srcset(candidates, viewport_width=1280, device_pixel_ratio=1, sizes=sizes)
>>> selection.candidate.resolved_url
'https://example.com/small.jpg'
>>> selection.reason
"slot is 400px (sizes '(max-width: 600px) 100vw, 400px' at a 1280px viewport); need >= 1x, small.jpg gives 1.00x (400w), the smallest sufficient candidate"

Parsing is strict: mixing width and pixel density descriptors, repeating
a descriptor within a candidate, or using a malformed number raises
:exc:`SrcsetParseError` instead of being silently ignored. URLs are
split following the HTML standard, so a comma inside a URL, e.g. in a
``data:`` URL, does not start a new candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from w3lib._infra import _ASCII_WHITESPACE
from w3lib.url import _urljoin

__all__ = [
    "SrcsetCandidate",
    "SrcsetParseError",
    "SrcsetSelection",
    "choose_srcset",
    "evaluate_sizes",
    "parse_srcset",
]


class SrcsetParseError(ValueError):
    """A ``srcset`` attribute does not follow the supported grammar."""


# https://html.spec.whatwg.org/#valid-floating-point-number
_FLOAT_RE = re.compile(r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


@dataclass
class SrcsetCandidate:
    """One image candidate string of a ``srcset`` attribute.

    ``raw`` keeps the candidate text as written, and ``base_url`` the
    address resolution base, so that :attr:`resolved_url` can be computed
    lazily and the original markup is never lost.
    """

    raw: str
    url: str
    width: int | None = None
    density: float | None = None
    base_url: str | None = None

    @property
    def effective_density(self) -> float:
        """Pixel density of the candidate; a missing ``x`` descriptor is 1x."""
        return self.density if self.density is not None else 1.0

    @property
    def resolved_url(self) -> str:
        """The candidate URL resolved against :attr:`base_url`."""
        if self.base_url is None:
            return self.url
        return _urljoin(self.base_url, self.url)


def _parse_descriptors(
    tokens: list[str], candidate: str
) -> tuple[int | None, float | None]:
    width: int | None = None
    density: float | None = None
    for token in tokens:
        value, kind = token[:-1], token[-1:]
        if kind in ("w", "x"):
            if kind == "w":
                if not value.isascii() or not value.isdigit() or int(value) == 0:
                    raise SrcsetParseError(
                        f"Invalid width descriptor {token!r} in {candidate!r}"
                    )
                if width is not None or density is not None:
                    raise SrcsetParseError(
                        f"Duplicate or mixed descriptors in {candidate!r}"
                    )
                width = int(value)
            else:
                if not _FLOAT_RE.match(value) or float(value) <= 0:
                    raise SrcsetParseError(
                        f"Invalid pixel density descriptor {token!r} in {candidate!r}"
                    )
                if width is not None or density is not None:
                    raise SrcsetParseError(
                        f"Duplicate or mixed descriptors in {candidate!r}"
                    )
                density = float(value)
        else:
            raise SrcsetParseError(f"Unsupported descriptor {token!r} in {candidate!r}")
    return width, density


def parse_srcset(srcset: str, base_url: str | None = None) -> list[SrcsetCandidate]:
    """Parse a ``srcset`` attribute into a list of :class:`SrcsetCandidate`.

    The splitting loop follows `parse a srcset attribute`_ from the HTML
    standard: a URL runs to the next ASCII whitespace, so commas inside a
    URL, as in a ``data:`` URL, do not separate candidates, and a comma
    right after a URL simply ends the candidate.

    .. _parse a srcset attribute: https://html.spec.whatwg.org/#parse-a-srcset-attribute

    *base_url* is kept on each candidate as the address resolution base
    for :attr:`SrcsetCandidate.resolved_url`.

    Raises :exc:`SrcsetParseError` on duplicate or mixed descriptors
    within a candidate, on malformed descriptor numbers, on unknown
    descriptors, and on mixing width-described and density-described
    candidates in the same attribute.
    """
    candidates: list[SrcsetCandidate] = []
    position = 0
    end = len(srcset)
    while position < end:
        # Skip ASCII whitespace and commas between candidates.
        while position < end and srcset[position] in _ASCII_WHITESPACE + ",":
            position += 1
        if position >= end:
            break
        candidate_start = position
        # The URL runs to the next ASCII whitespace; commas are URL text.
        while position < end and srcset[position] not in _ASCII_WHITESPACE:
            position += 1
        url = srcset[candidate_start:position]
        tokens: list[str] = []
        if url.endswith(","):
            # A comma right after the URL ends the candidate; it has no
            # descriptors.
            url = url.rstrip(",")
        else:
            # Collect descriptors: runs of characters that are neither
            # ASCII whitespace nor a comma. A comma ends the candidate.
            while position < end:
                while position < end and srcset[position] in _ASCII_WHITESPACE:
                    position += 1
                if position >= end or srcset[position] == ",":
                    if position < end:
                        position += 1
                    break
                token_start = position
                while (
                    position < end
                    and srcset[position] not in _ASCII_WHITESPACE
                    and srcset[position] != ","
                ):
                    position += 1
                tokens.append(srcset[token_start:position])
        raw = srcset[candidate_start:position].rstrip(_ASCII_WHITESPACE + ",")
        width, density = _parse_descriptors(tokens, raw)
        candidates.append(
            SrcsetCandidate(
                raw=raw, url=url, width=width, density=density, base_url=base_url
            )
        )
    has_width = any(candidate.width is not None for candidate in candidates)
    has_density = any(candidate.width is None for candidate in candidates)
    if has_width and has_density:
        raise SrcsetParseError(
            f"Width and pixel density descriptors cannot be mixed in {srcset!r}"
        )
    return candidates


_CONDITION_LENGTH_RE = re.compile(
    r"""
    \(\s*(?P<feature>min|max)-width\s*:\s*(?P<condition>[0-9]+(?:\.[0-9]+)?)px\s*\)
    \s*(?P<length>[0-9]+(?:\.[0-9]+)?(?:px|vw))\s*
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BARE_LENGTH_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:px|vw)\Z", re.IGNORECASE)


def _length_to_px(length: str, viewport_width: float) -> float:
    value = float(length[:-2])
    if length[-2:].lower() == "vw":
        return value * viewport_width / 100
    return value


def evaluate_sizes(sizes: str, viewport_width: float) -> float | None:
    """Evaluate a ``sizes`` attribute to a slot width in CSS pixels.

    Only the explicitly supported subset is recognized: comma-separated
    ``(min-width: Npx)`` or ``(max-width: Npx)`` conditions each followed
    by a length in ``px`` or ``vw``, with a bare ``px`` or ``vw`` length
    as the final default. The first condition that matches
    *viewport_width* wins; if none does, the final length applies.

    Returns ``None`` — the slot width cannot be determined — for anything
    outside that subset (``calc()``, ``em``, media features other than
    viewport width, malformed numbers, …) instead of guessing.
    """
    parts = sizes.split(",")
    if not parts or any(not part.strip() for part in parts):
        return None
    for part in parts[:-1]:
        match = _CONDITION_LENGTH_RE.fullmatch(part.strip())
        if match is None:
            return None
        condition = float(match.group("condition"))
        if match.group("feature").lower() == "min":
            matched = viewport_width >= condition
        else:
            matched = viewport_width <= condition
        if matched:
            return _length_to_px(match.group("length"), viewport_width)
    default = parts[-1].strip()
    if not _BARE_LENGTH_RE.match(default):
        return None
    return _length_to_px(default, viewport_width)


@dataclass
class SrcsetSelection:
    """The outcome of :func:`choose_srcset`.

    ``candidate`` is ``None`` when the slot width could not be
    determined; ``reason`` always explains the outcome.
    """

    candidate: SrcsetCandidate | None
    source_size: float | None
    reason: str


def choose_srcset(
    candidates: list[SrcsetCandidate],
    viewport_width: float,
    device_pixel_ratio: float = 1.0,
    sizes: str | None = None,
) -> SrcsetSelection:
    """Pick the image candidate to download for the given environment.

    For width-described candidates the slot width comes from *sizes*
    (defaulting to ``100vw`` when *sizes* is not given) and each
    candidate is normalized to a density of ``width / slot``; the
    candidate with the smallest density that still reaches
    *device_pixel_ratio* wins, or the densest one when none does. For
    density-described candidates the same rule applies to their ``x``
    values directly. Ties keep document order.

    If *sizes* is given but cannot be evaluated with
    :func:`evaluate_sizes`, the selection is undetermined: the returned
    :class:`SrcsetSelection` has no candidate and its ``reason`` says so.
    """
    if not candidates:
        raise ValueError("No srcset candidates to choose from")
    if viewport_width <= 0:
        raise ValueError(f"viewport_width must be positive, got {viewport_width!r}")
    if device_pixel_ratio <= 0:
        raise ValueError(
            f"device_pixel_ratio must be positive, got {device_pixel_ratio!r}"
        )
    has_width = candidates[0].width is not None
    if any((candidate.width is not None) != has_width for candidate in candidates):
        raise SrcsetParseError(
            "Width and pixel density descriptors cannot be mixed: "
            f"{[candidate.raw for candidate in candidates]!r}"
        )
    source_size: float | None = None
    if has_width:
        if sizes is None:
            source_size = float(viewport_width)
            slot_note = f"slot is {source_size:g}px (no sizes, defaults to 100vw)"
        else:
            source_size = evaluate_sizes(sizes, viewport_width)
            if source_size is None or source_size <= 0:
                return SrcsetSelection(
                    candidate=None,
                    source_size=None,
                    reason=(
                        f"sizes {sizes!r} is outside the supported subset, "
                        "the slot width cannot be determined"
                    ),
                )
            slot_note = (
                f"slot is {source_size:g}px "
                f"(sizes {sizes!r} at a {viewport_width:g}px viewport)"
            )
        densities = []
        for candidate in candidates:
            assert candidate.width is not None
            densities.append((candidate, candidate.width / source_size))
        describe = "{1.url} gives {0:.2f}x ({1.width}w)".format
    else:
        slot_note = "density-described candidates"
        densities = [
            (candidate, candidate.effective_density) for candidate in candidates
        ]
        describe = "{1.url} gives {0:g}x".format

    sufficient = [entry for entry in densities if entry[1] >= device_pixel_ratio]
    if sufficient:
        candidate, density = min(sufficient, key=lambda entry: entry[1])
        outcome = "the smallest sufficient candidate"
    else:
        candidate, density = max(densities, key=lambda entry: entry[1])
        outcome = "the densest available candidate"
    reason = (
        f"{slot_note}; need >= {device_pixel_ratio:g}x, "
        f"{describe(density, candidate)}, {outcome}"
    )
    return SrcsetSelection(candidate=candidate, source_size=source_size, reason=reason)
