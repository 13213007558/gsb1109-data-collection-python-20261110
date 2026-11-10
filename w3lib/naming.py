"""
Cross-platform file naming for offline crawl storage.

This module assigns every crawled URL a safe relative path that can be
created on both Windows and Linux, and keeps a manifest that maps each
path back to the original URL.

Whether two URLs refer to the same resource (different query ordering,
percent-encoding spelling, letter case, …) is decided by an explicit
*identity policy* passed to :class:`PathAllocator`; nothing is merged
implicitly.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .url import canonicalize_url

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    IdentityPolicy = Callable[[str], str]
    PathLike = str | Path

__all__ = [
    "PathAllocator",
    "canonical_identity",
    "ensure_creatable",
    "exact_identity",
    "is_safe_relative_path",
    "load_manifest",
    "save_manifest",
]

# Characters not allowed in file names on Windows (Linux only forbids "/"
# and NUL, both covered here).
_INVALID_CHARS = frozenset('<>:"/\\|?*')

# Device names reserved by Windows, matched case-insensitively against the
# part of a file name before the first dot.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_DEFAULT_MAX_COMPONENT_LENGTH = 80
_DEFAULT_MAX_PATH_LENGTH = 200
_DEFAULT_HASH_LENGTH = 12


def exact_identity(url: str) -> str:
    """Identity policy under which only identical URL strings are the same
    resource."""
    return url


def canonical_identity(url: str) -> str:
    """Identity policy under which URLs that only differ in ways servers
    usually ignore (query argument order, percent-encoding case, host case,
    …) are the same resource. Uses :func:`w3lib.url.canonicalize_url`."""
    return canonicalize_url(url)


def _fold(path: str) -> str:
    """Return the key under which *path* is unique on case-insensitive,
    normalization-insensitive file systems (Windows, macOS)."""
    return unicodedata.normalize("NFC", path).casefold()


def _is_reserved(component: str) -> bool:
    base = component.split(".", 1)[0]
    return base.casefold() in _RESERVED_NAMES


def _sanitize_component(text: str, max_length: int) -> str:
    """Make *text* a single file name component safe for Windows and Linux."""
    text = unicodedata.normalize("NFC", text)
    text = "".join(
        "_" if char in _INVALID_CHARS or ord(char) < 32 or ord(char) == 127 else char
        for char in text
    )
    # Windows strips trailing dots and spaces; leading spaces are fragile too.
    text = text.strip(" ").rstrip(".")
    if not text:
        text = "_"
    text = text[:max_length].rstrip(" .")
    if not text:
        text = "_"
    if _is_reserved(text):
        text = ("_" + text)[:max_length]
    return text


def _slug_components(url: str, max_component_length: int) -> list[str]:
    """Build human-readable path components from *url*."""
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    raw: list[str] = []
    if parts is not None:
        netloc = parts.netloc.rsplit("@", 1)[-1]
        if netloc:
            raw.append(netloc)
        raw.extend(segment for segment in parts.path.split("/") if segment)
        if parts.query:
            if raw:
                raw[-1] = f"{raw[-1]}_{parts.query}"
            else:
                raw.append(f"index_{parts.query}")
    else:
        raw.append(url)
    if not raw:
        raw = ["index"]
    return [_sanitize_component(component, max_component_length) for component in raw]


def is_safe_relative_path(
    path: str,
    *,
    max_component_length: int = _DEFAULT_MAX_COMPONENT_LENGTH,
    max_path_length: int = _DEFAULT_MAX_PATH_LENGTH,
) -> bool:
    """Return True if *path* is a relative path that can be created on both
    Windows and Linux without conflicting with another path that differs
    only in letter case or Unicode normalization form."""
    if not path or len(path) > max_path_length:
        return False
    if path != unicodedata.normalize("NFC", path):
        return False
    if path.startswith("/") or "\\" in path:
        return False
    for component in path.split("/"):
        if component in ("", ".", ".."):
            return False
        if len(component) > max_component_length:
            return False
        if component[-1] in " .":
            return False
        if any(
            char in _INVALID_CHARS or ord(char) < 32 or ord(char) == 127
            for char in component
        ):
            return False
        if _is_reserved(component):
            return False
    return True


class PathAllocator:
    """Assign safe relative paths to URLs, tracking a manifest.

    :param identity: callable mapping a URL to its identity key. URLs with
        the same key share a path; URLs with different keys never do. Use
        :func:`exact_identity` (default) to treat every distinct URL string
        as a separate resource, :func:`canonical_identity` to merge URLs
        that only differ in cosmetic ways, or any custom callable.
    :param manifest: existing mapping of relative path -> original URL, as
        returned by a previous run. Its assignments are kept as-is.
    :param existing: extra relative paths already taken on disk (e.g. files
        from other sources). New assignments avoid them.
    """

    def __init__(
        self,
        *,
        identity: IdentityPolicy = exact_identity,
        manifest: Mapping[str, str] | None = None,
        existing: Iterable[str] = (),
        max_component_length: int = _DEFAULT_MAX_COMPONENT_LENGTH,
        max_path_length: int = _DEFAULT_MAX_PATH_LENGTH,
        hash_length: int = _DEFAULT_HASH_LENGTH,
    ) -> None:
        if hash_length < 8:
            raise ValueError("hash_length must be at least 8")
        if max_component_length < hash_length + 4:
            raise ValueError("max_component_length is too small for the hash suffix")
        if max_path_length < max_component_length:
            raise ValueError("max_path_length must be >= max_component_length")
        self._identity = identity
        self._max_component_length = max_component_length
        self._max_path_length = max_path_length
        self._hash_length = hash_length
        self._path_to_url: dict[str, str] = {}
        self._identity_to_path: dict[str, str] = {}
        self._taken: set[str] = set()
        for path in existing:
            self._taken.add(_fold(path))
        if manifest:
            for path, url in manifest.items():
                self._path_to_url[path] = url
                self._identity_to_path.setdefault(identity(url), path)
                self._taken.add(_fold(path))

    @property
    def manifest(self) -> dict[str, str]:
        """Mapping of assigned relative path -> original URL."""
        return dict(self._path_to_url)

    def assign(self, url: str) -> str:
        """Return the relative path for *url*, allocating a new one if its
        identity has not been seen before."""
        key = self._identity(url)
        path = self._identity_to_path.get(key)
        if path is None:
            path = self._allocate(url, key)
            self._identity_to_path[key] = path
            self._path_to_url[path] = url
            self._taken.add(_fold(path))
        return path

    def assign_all(self, urls: Iterable[str]) -> dict[str, str]:
        """Assign paths to *urls* and return a URL -> path mapping for them.
        Feeding the same URLs in several batches yields the same assignments
        as feeding them all at once."""
        return {url: self.assign(url) for url in urls}

    def _allocate(self, url: str, key: str) -> str:
        attempt = 0
        while True:
            candidate = self._candidate_path(url, key, attempt)
            if _fold(candidate) not in self._taken and is_safe_relative_path(
                candidate,
                max_component_length=self._max_component_length,
                max_path_length=self._max_path_length,
            ):
                return candidate
            attempt += 1

    def _candidate_path(self, url: str, key: str, attempt: int) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[: self._hash_length]
        suffix = digest if attempt == 0 else f"{digest}~{attempt}"
        components = _slug_components(url, self._max_component_length)
        last_base = components[-1]

        def last_component(budget: int) -> str:
            budget = min(budget, self._max_component_length - len(suffix) - 1)
            if budget < 1:
                return suffix[: self._max_path_length]
            base = last_base[:budget].rstrip(" .") or "_"
            return f"{base}-{suffix}"

        budget = self._max_component_length - len(suffix) - 1
        last = last_component(budget)
        relpath = "/".join([*components[:-1], last])
        if len(relpath) <= self._max_path_length:
            return relpath
        if len(components) > 1:
            budget = self._max_path_length - len(components[0]) - len(suffix) - 2
            relpath = f"{components[0]}/{last_component(budget)}"
            if len(relpath) <= self._max_path_length:
                return relpath
        return last_component(self._max_path_length - len(suffix) - 1)


def save_manifest(manifest: Mapping[str, str], file: PathLike) -> None:
    """Write *manifest* (relative path -> original URL) as JSON, so the
    original addresses can be recovered from the stored files."""
    with Path(file).open("w", encoding="utf-8") as fp:
        json.dump(dict(manifest), fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")


def load_manifest(file: PathLike) -> dict[str, str]:
    """Read a manifest written by :func:`save_manifest`."""
    with Path(file).open(encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise ValueError("not a path -> URL manifest")
    return data


def ensure_creatable(
    paths: Iterable[str],
    root: PathLike,
    *,
    max_component_length: int = _DEFAULT_MAX_COMPONENT_LENGTH,
    max_path_length: int = _DEFAULT_MAX_PATH_LENGTH,
) -> list[Path]:
    """Validate *paths* by really creating them as empty files under *root*.

    Every path is checked against the cross-platform rules first, then
    created exclusively: an existing file (including one that only differs
    in case or Unicode normalization on such file systems) raises
    :exc:`FileExistsError` instead of being overwritten. Returns the created
    placeholder files, which also reserve the names on disk.
    """
    created = []
    for relpath in paths:
        if not is_safe_relative_path(
            relpath,
            max_component_length=max_component_length,
            max_path_length=max_path_length,
        ):
            raise ValueError(f"unsafe relative path: {relpath!r}")
        target = Path(root, *relpath.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x"):
            pass
        created.append(target)
    return created
