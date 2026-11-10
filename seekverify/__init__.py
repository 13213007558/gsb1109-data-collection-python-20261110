"""
seekverify: offline verification that warcio index offsets re-locate records.

The package never modifies a supplied index. Verification compares three
sources of truth:

1. the supplied index entry,
2. an independent byte-level reader (:mod:`seekverify.rawreader`),
3. a warcio re-seek performed exactly like the production backend.

Failures split into :class:`IndexInconsistentError` (the bytes are fine, the
index does not describe them) and :class:`CorruptionError` (the archive bytes
themselves are damaged).
"""

from .errors import CorruptionError, IndexInconsistentError, SeekVerifyError
from .models import IndexEntry, RecordFingerprint, VerificationResult
from .verifier import SeekVerifier, build_truth_index, load_index

__all__ = [
    "CorruptionError",
    "IndexEntry",
    "IndexInconsistentError",
    "RecordFingerprint",
    "SeekVerifier",
    "SeekVerifyError",
    "VerificationResult",
    "build_truth_index",
    "load_index",
]
