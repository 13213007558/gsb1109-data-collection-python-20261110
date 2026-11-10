"""
Explicit redaction (sanitization) export for WARC archives.

Removes sensitive HTTP headers (cookies, authentication headers) and
selected form fields from request bodies, while leaving every record that
does not match the policy byte-for-byte untouched at the semantic level.

Conservative policies are applied deliberately:

* Compressed payloads (``Content-Encoding``) and transport-framed payloads
  (``Transfer-Encoding``) are never edited in place, because rewriting the
  stored bytes would require re-encoding the entity. Header-level
  redaction is still applied; the skipped body edit is reported.
* Duplicate HTTP headers are all removed, not just the first occurrence.
* A form body that cannot be parsed is never partially rewritten: the
  record is kept unmodified and the failure is reported explicitly, so a
  record is never claimed to be clean while the original sensitive value
  is still present.
* Whenever bytes change, the stale ``WARC-Payload-Digest`` /
  ``WARC-Block-Digest`` (and the HTTP ``Content-Length`` when the body
  changes) are discarded and recomputed from the new bytes.

The input archive is only ever opened read-only (``rb``).
"""

import json
import os

from io import BytesIO

from six.moves.urllib.parse import unquote_plus

from warcio.archiveiterator import ArchiveIterator
from warcio.digestverifyingreader import (
    _compare_digest_rfc_3548,
    _parse_digest,
)
from warcio.recordloader import ArcWarcRecord
from warcio.utils import Digester, fsspec_open
from warcio.warcwriter import WARCWriter


# Header names matched case-insensitively against HTTP header names.
DEFAULT_SENSITIVE_HEADERS = (
    'cookie',
    'set-cookie',
    'authorization',
    'proxy-authorization',
)

FORM_CONTENT_TYPE = 'application/x-www-form-urlencoded'

REDACTED = 'redacted'
RETAINED = 'retained'
FAILED = 'failed'


# ============================================================================
class RedactionPolicy(object):
    """Declarative description of what must be removed.

    :param sensitive_headers: HTTP header names to remove (case-insensitive).
    :param sensitive_form_fields: form field names to remove from
        ``application/x-www-form-urlencoded`` request bodies.
    :param fail_on_parse_error: when True, an unparseable form body causes
        the record to be kept unmodified and flagged as failed; when False
        the record is retained with a warning action instead.
    """

    def __init__(self, sensitive_headers=DEFAULT_SENSITIVE_HEADERS,
                 sensitive_form_fields=(), fail_on_parse_error=True):
        self.sensitive_headers = [name.lower() for name in sensitive_headers]
        self.sensitive_form_fields = set(sensitive_form_fields)
        self.fail_on_parse_error = fail_on_parse_error


# ============================================================================
class RedactionResult(object):
    """Mapping entry describing how one source record was exported."""

    def __init__(self, source_offset, source_length, record_id, rec_type, uri):
        self.source_offset = source_offset
        self.source_length = source_length
        self.record_id = record_id
        self.rec_type = rec_type
        self.uri = uri
        self.output_offset = None
        self.status = RETAINED
        self.actions = []
        self.reasons = []
        self.old_payload_digest = None
        self.new_payload_digest = None
        self.old_block_digest = None
        self.new_block_digest = None
        self.source_digest_failed = False

    def add_action(self, target, action, **details):
        entry = {'target': target, 'action': action}
        entry.update(details)
        self.actions.append(entry)

    def fail(self, reason):
        self.status = FAILED
        self.reasons.append(reason)

    def mark_redacted(self):
        if self.status != FAILED:
            self.status = REDACTED

    def to_dict(self):
        return {
            'source': {
                'offset': self.source_offset,
                'length': self.source_length,
                'record_id': self.record_id,
                'type': self.rec_type,
                'uri': self.uri,
            },
            'output': {
                'offset': self.output_offset,
                'record_id': self.record_id,
            },
            'status': self.status,
            'actions': self.actions,
            'reasons': self.reasons,
            'source_digest_failed': self.source_digest_failed,
            'digests': {
                'old_payload': self.old_payload_digest,
                'new_payload': self.new_payload_digest,
                'old_block': self.old_block_digest,
                'new_block': self.new_block_digest,
            },
        }


# ============================================================================
class RecordRedactor(object):
    """Applies a :class:`RedactionPolicy` to parsed WARC records."""

    def __init__(self, policy=None):
        self.policy = policy or RedactionPolicy()

    def redact(self, record):
        """Return ``(output_record, result)`` for a single parsed record.

        The returned record is always writable by :class:`WARCWriter`.
        A failed result means the record must be written unmodified.
        """
        rec_headers = record.rec_headers
        result = RedactionResult(
            source_offset=None,
            source_length=None,
            record_id=rec_headers.get_header('WARC-Record-ID'),
            rec_type=record.rec_type,
            uri=rec_headers.get_header('WARC-Target-URI'),
        )
        result.old_payload_digest = rec_headers.get_header('WARC-Payload-Digest')
        result.old_block_digest = rec_headers.get_header('WARC-Block-Digest')

        body = record.raw_stream.read()

        if not record.http_headers:
            return self._rebuild(record, body, result), result

        original_http_headers = list(record.http_headers.headers)

        header_changed = self._redact_headers(record.http_headers, result)
        body_changed, new_body = self._redact_body(record, body, result)

        if header_changed:
            result.mark_redacted()

        if result.status == FAILED:
            # Conservative policy: do not ship a half-cleaned record.
            # Restore the parsed headers and emit the original bytes so
            # the record is never claimed clean while sensitive values
            # remain in it.
            record.http_headers.headers = original_http_headers
            return self._rebuild(record, body, result), result

        if body_changed:
            body = new_body
            result.mark_redacted()
            self._sync_http_content_length(record.http_headers, len(body))

        output = self._rebuild(record, body, result)

        if result.status == REDACTED:
            # The block always changes when headers or body are redacted,
            # so its digest is stale and must be recomputed.
            rec_headers.remove_header('WARC-Block-Digest')
            if body_changed:
                # Only a payload byte change invalidates the payload digest.
                # revisit records carry no payload and their digest points at
                # the original capture; body is never edited there, so it is
                # preserved.
                rec_headers.remove_header('WARC-Payload-Digest')

        return output, result

    # ------------------------------------------------------------------
    def _redact_headers(self, http_headers, result):
        removed = {}
        kept = []
        for name, value in http_headers.headers:
            if name.lower() in self.policy.sensitive_headers:
                removed[name.lower()] = removed.get(name.lower(), 0) + 1
            else:
                kept.append((name, value))

        for name, count in removed.items():
            result.add_action('http-header:' + name, 'removed', count=count)

        if removed:
            http_headers.headers = kept
            return True
        return False

    def _redact_body(self, record, body, result):
        """Redact form fields in a request body.

        Returns ``(changed, new_body)``. On failure the body is returned
        unchanged and the result is marked failed/skipped.
        """
        if record.rec_type != 'request':
            return False, body

        http_headers = record.http_headers
        content_type = http_headers.get_header('Content-Type') or ''
        if FORM_CONTENT_TYPE not in content_type.lower():
            return False, body

        if not self.policy.sensitive_form_fields:
            return False, body

        # Conservative: never rewrite a framed/encoded payload blindly.
        if http_headers.get_header('Content-Encoding'):
            result.add_action('form-fields', 'skipped',
                              reason='content-encoding present; payload not rewritten')
            return False, body
        if http_headers.get_header('Transfer-Encoding'):
            result.add_action('form-fields', 'skipped',
                              reason='transfer-encoding present; payload not rewritten')
            return False, body

        if not body:
            return False, body

        try:
            pairs = self._parse_form(body)
        except FormParseError as exc:
            reason = 'unparseable form body ({0}); record kept unmodified'.format(exc)
            if self.policy.fail_on_parse_error:
                result.add_action('form-fields', 'parse-error')
                result.fail(reason)
            else:
                result.add_action('form-fields', 'skipped', reason=reason)
            return False, body

        kept_pairs = []
        removed_fields = set()
        for raw_key, raw_value in pairs:
            field_name = _form_decode(raw_key)
            if (field_name in self.policy.sensitive_form_fields or
                    raw_key.decode('ascii') in self.policy.sensitive_form_fields):
                removed_fields.add(field_name)
            else:
                kept_pairs.append((raw_key, raw_value))

        if not removed_fields:
            return False, body

        new_body = b'&'.join(key + b'=' + value for key, value in kept_pairs)
        if new_body == body:
            return False, body

        for field in sorted(removed_fields):
            result.add_action('form-field:' + field, 'removed')

        return True, new_body

    @staticmethod
    def _parse_form(body):
        """Split a urlencoded body into ``(raw_key, raw_value)`` pairs.

        Raises :class:`FormParseError` on anything that cannot be mapped
        unambiguously to key/value pairs.
        """
        pairs = []
        for segment in body.split(b'&'):
            if segment == b'':
                # trailing '&' or empty segment: tolerate but do not invent
                continue
            if b'=' not in segment:
                raise FormParseError('segment without "=": {0!r}'.format(segment))
            raw_key, raw_value = segment.split(b'=', 1)
            try:
                raw_key.decode('ascii')
            except UnicodeDecodeError:
                raise FormParseError('non-ascii key in form body')
            pairs.append((raw_key, raw_value))
        return pairs

    @staticmethod
    def _sync_http_content_length(http_headers, new_length):
        if http_headers.get_header('Content-Length') is not None:
            http_headers.replace_header('Content-Length', str(new_length))

    @staticmethod
    def _rebuild(record, body, result):
        payload = BytesIO(body)
        new_record = ArcWarcRecord(
            record.format,
            record.rec_type,
            record.rec_headers,
            payload,
            record.http_headers,
            record.content_type,
            len(body),
        )
        new_record.payload_length = len(body)
        return new_record


class FormParseError(Exception):
    pass


def _form_decode(raw_key):
    try:
        return unquote_plus(raw_key.decode('ascii'))
    except (UnicodeDecodeError, AttributeError):
        return raw_key.decode('latin-1')


def _digest_matches(data, declared_digest):
    """Compare freshly hashed bytes against a WARC digest header.

    Tolerates the RFC 3548 encodings warcio itself accepts (base32,
    base64/base64url, hex) instead of assuming base32.
    """
    try:
        algo, _ = _parse_digest(declared_digest)
    except ValueError:
        return False
    digester = Digester(algo)
    digester.update(data)
    return _compare_digest_rfc_3548(digester, declared_digest) is True


# ============================================================================
def verify_redacted_archive(path):
    """Re-read an output archive and independently verify every digest.

    Returns a list of issue dicts; an empty list means every declared
    ``WARC-Block-Digest`` / ``WARC-Payload-Digest`` matches the bytes on
    disk (across supported digest encodings) and every ``Content-Length``
    matches the actual block size. Each issue carries ``index`` and
    ``record_id`` so callers can tell pre-existing source breakage apart
    from problems introduced by redaction.
    """
    issues = []
    with fsspec_open(path, 'rb') as fh:
        iterator = ArchiveIterator(fh)
        for index, record in enumerate(iterator):
            rec_headers = record.rec_headers
            record_id = rec_headers.get_header('WARC-Record-ID')
            prefix = 'record {0} ({1})'.format(index, record_id)

            declared_length = rec_headers.get_header('Content-Length')
            block_parts = []
            if record.http_headers:
                record.http_headers.compute_headers_buffer()
                block_parts.append(record.http_headers.headers_buff)
            payload = record.raw_stream.read()
            block_parts.append(payload)
            block = b''.join(block_parts)

            if declared_length is not None and int(declared_length) != len(block):
                issues.append({'index': index, 'record_id': record_id,
                               'detail': '{0}: Content-Length {1} != actual {2}'.format(
                                   prefix, declared_length, len(block))})

            block_digest = rec_headers.get_header('WARC-Block-Digest')
            if block_digest and not _digest_matches(block, block_digest):
                issues.append({'index': index, 'record_id': record_id,
                               'detail': prefix + ': block digest mismatch'})

            payload_digest = rec_headers.get_header('WARC-Payload-Digest')
            if (payload_digest and record.rec_type not in ('warcinfo', 'revisit')
                    and not _digest_matches(payload, payload_digest)):
                issues.append({'index': index, 'record_id': record_id,
                               'detail': prefix + ': payload digest mismatch'})

    return issues


# ============================================================================
def sanitize_file(input_path, output_path, policy=None, gzip=False,
                  manifest_path=None, strict=False):
    """Redact ``input_path`` into ``output_path`` and write a JSONL mapping.

    The input file is opened in ``rb`` mode and never written to.

    :param strict: when True, raise :class:`RedactionError` if any record
        fails redaction or redaction introduces a digest/length problem.
        Digests that were already broken in the source of an untouched
        record are reported but do not count as introduced failures.
    :return: tuple ``(results, introduced, pre_existing)`` where
        ``introduced`` lists verification issues caused by this run and
        ``pre_existing`` lists broken digests inherited from the source.
    """
    policy = policy or RedactionPolicy()
    redactor = RecordRedactor(policy)

    if os.path.abspath(str(input_path)) == os.path.abspath(str(output_path)):
        raise RedactionError('refusing to overwrite the input file')

    results = []
    source_failures = set()
    with fsspec_open(input_path, 'rb') as fh, \
            fsspec_open(output_path, 'wb') as out:
        writer = WARCWriter(filebuf=out, gzip=gzip)
        # arc2warc normalizes ARC inputs into proper WARC records (as the
        # recompressor does); plain WARC records are parsed identically.
        # check_digests lets us attribute pre-existing source breakage.
        iterator = ArchiveIterator(fh, arc2warc=True, check_digests=True)

        for record in iterator:
            output_record, result = redactor.redact(record)

            checker = getattr(record, 'digest_checker', None)
            if checker is not None and checker.passed is False:
                source_failures.add(result.record_id)
                result.source_digest_failed = True
                for problem in checker.problems:
                    result.reasons.append('pre-existing source issue: ' + problem)

            # Source geometry is only known after the record is drained.
            result.source_offset = iterator.get_record_offset()
            result.source_length = iterator.get_record_length()

            result.output_offset = out.tell()
            writer.write_record(output_record)

            stored = output_record.rec_headers
            result.new_payload_digest = stored.get_header('WARC-Payload-Digest')
            result.new_block_digest = stored.get_header('WARC-Block-Digest')
            results.append(result)

    if manifest_path:
        with fsspec_open(manifest_path, 'w', encoding='utf-8') as manifest:
            for result in results:
                manifest.write(json.dumps(result.to_dict(),
                                          sort_keys=True,
                                          ensure_ascii=False))
                manifest.write('\n')

    all_issues = verify_redacted_archive(output_path)
    redacted_ids = {r.record_id for r in results if r.status == REDACTED}

    # A redacted record always carries freshly recomputed digests, so any
    # verification failure on it was introduced here. A failure on a
    # retained record whose source was already broken is pre-existing.
    introduced, pre_existing = [], []
    for issue in all_issues:
        record_id = issue['record_id']
        if record_id in redacted_ids or record_id not in source_failures:
            introduced.append(issue['detail'])
        else:
            pre_existing.append(issue['detail'])

    failed = [r for r in results if r.status == FAILED]
    if strict and (failed or introduced):
        raise RedactionError(
            '{0} record(s) failed redaction; {1} introduced digest '
            'problem(s)'.format(len(failed), len(introduced)))

    return results, introduced, pre_existing


class RedactionError(Exception):
    pass
