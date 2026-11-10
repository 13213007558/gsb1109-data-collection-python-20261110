"""
Error taxonomy for seek verification.

Two failure families must never be conflated:

* ``IndexInconsistentError`` - the archive bytes are intact, but the index
  entry does not describe what physically lives at its offset (wrong offset
  unit, stale ID, tampered length, offset pointing at another record, ...).
* ``CorruptionError`` - the archive itself cannot be parsed at that location
  (bad gzip CRC, truncated member, garbage between members, ...), independent
  of any index.

A third, internal base class (:class:`SeekVerifyError`) exists so callers can
catch every failure raised by this package with one ``except``.
"""


class SeekVerifyError(Exception):
    """Base class for every error raised by ``seekverify``."""

    kind = "error"

    def __init__(self, message, detail=None):
        super(SeekVerifyError, self).__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self):
        return {
            "type": self.__class__.__name__,
            "kind": self.kind,
            "message": self.message,
            "detail": self.detail,
        }


class IndexInconsistentError(SeekVerifyError):
    """The index entry disagrees with the (independently readable) archive."""

    kind = "index_inconsistent"


class CorruptionError(SeekVerifyError):
    """The archive bytes themselves are damaged at this location."""

    kind = "corruption"
