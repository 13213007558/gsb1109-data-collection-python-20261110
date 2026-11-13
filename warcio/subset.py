import json
import re
import sys

from collections import OrderedDict
from io import BytesIO

from warcio.archiveiterator import ArchiveIterator
from warcio.utils import fsspec_open
from warcio.warcwriter import GzippingWrapper


# ============================================================================
class SubsetPolicy(object):
    """
    Explicit policy describing which related records a subset may follow.

    ``follow`` accepts any of the relation names below. Unknown names are
    rejected so a typo can not silently produce an incomplete subset.

    concurrent -- follow WARC-Concurrent-To in both directions
                  (request/response capture pairs)
    refers-to  -- follow WARC-Refers-To, WARC-Refers-To-Target-URI and
                  WARC-Refers-To-Date references (revisit dependencies)
    payload    -- include a record carrying the payload digest needed by a
                  revisit, when the revisit itself has no body
    warcinfo   -- include the warcinfo record(s) for each source archive used
    """

    CONCURRENT = 'concurrent'
    REFERS_TO = 'refers-to'
    PAYLOAD = 'payload'
    WARCINFO = 'warcinfo'

    RELATIONS = (CONCURRENT, REFERS_TO, PAYLOAD, WARCINFO)

    _ALIASES = {
        'concurrent': CONCURRENT,
        'concurrent-to': CONCURRENT,
        'pair': CONCURRENT,
        'pairs': CONCURRENT,
        'request-response': CONCURRENT,
        'refers-to': REFERS_TO,
        'refers': REFERS_TO,
        'revisit': REFERS_TO,
        'payload': PAYLOAD,
        'payload-digest': PAYLOAD,
        'digest': PAYLOAD,
        'warcinfo': WARCINFO,
        'info': WARCINFO,
    }

    def __init__(self, follow=None, include_warcinfo=None):
        if isinstance(follow, str):
            follow = follow.split(',')

        relations = set()
        follow_was_explicit = follow is not None
        if follow is None:
            relations.update(self.RELATIONS)
        else:
            for relation in follow:
                relation = relation.strip().lower()
                if not relation:
                    continue
                if relation == 'all':
                    relations.update(self.RELATIONS)
                    continue
                if relation == 'none':
                    continue
                try:
                    relations.add(self._ALIASES[relation])
                except KeyError:
                    raise ValueError('Unknown relation policy: ' + relation)

        self.follow = relations

        if include_warcinfo is None:
            include_warcinfo = (not follow_was_explicit
                                or self.WARCINFO in relations)
        self.include_warcinfo = include_warcinfo

    def follows(self, relation):
        return relation in self.follow

    @property
    def warcinfo(self):
        return self.include_warcinfo

    def to_dict(self):
        return OrderedDict([
            ('follow', [relation for relation in self.RELATIONS
                        if relation in self.follow]),
            ('include_warcinfo', self.include_warcinfo),
        ])

    def __repr__(self):
        return 'SubsetPolicy(follow={0!r}, include_warcinfo={1!r})'.format(
            sorted(self.follow), self.include_warcinfo)


# ============================================================================
class SubsetExporter(object):
    """
    Export a stable, de-duplicated subset of WARC/ARC records.

    Seeds are WARC-Record-ID values or exact WARC-Target-URI values. Related
    records are added only when requested by ``SubsetPolicy``.

    Included records keep their original WARC-Record-ID and payload bytes.
    Unresolved external dependencies are listed in the manifest, and a
    manifest with such dependencies is never marked complete.
    """

    def __init__(self, inputs, output=None, manifest=None, policy=None,
                 seed_ids=None, seed_uris=None, seeds=None, max_records=None,
                 gzip_output=None):
        if isinstance(inputs, (str, bytes)):
            inputs = [inputs]

        self.inputs = list(inputs)
        self.output = output
        self.manifest = manifest
        self.policy = policy if policy is not None else SubsetPolicy()
        self.max_records = max_records

        if gzip_output is None:
            gzip_output = bool(output and str(output).endswith('.gz'))
        self.gzip_output = gzip_output

        self.seed_ids = list(seed_ids or ())
        self.seed_uris = list(seed_uris or ())
        for seed in seeds or ():
            self.add_seed(seed)

        self._all = []
        self._id_index = {}
        self._uri_index = {}
        self._digest_index = {}
        self._concurrent_index = {}

        self._selected = OrderedDict()
        self._missing = OrderedDict()
        self._seed_keys = set()

        self._source_counts = OrderedDict()
        self._source_buffers = {}
        self._warcinfo_found = False

        self._scanned = False

    # ------------------------------------------------------------------
    def add_seed_id(self, record_id):
        self.seed_ids.append(record_id)

    def add_seed_uri(self, uri):
        self.seed_uris.append(uri)

    def add_seed(self, value):
        """Add a seed, detecting a record ID from its URN/angle brackets."""
        value = value.strip()
        if value.startswith('<') or value.lower().startswith('urn:'):
            self.add_seed_id(value)
        else:
            self.add_seed_uri(value)

    # ------------------------------------------------------------------
    def export(self):
        self._scan()
        self._select_seeds()
        self._follow_relations()
        self._select_warcinfo()
        self._check_limit()

        if self.output:
            self._write_records()

        if self.manifest:
            self._write_manifest()

        return self

    # ------------------------------------------------------------------
    def _scan(self):
        if self._scanned:
            return

        for filename in self.inputs:
            if filename != '-':
                with _open_source(filename, 'rb') as stream:
                    self._scan_one(filename, stream)
                continue

            # stdin can only be read once; keep its bytes for later record
            # copies, since records must be written in source order.
            data = sys.stdin.buffer.read()
            self._source_buffers[filename] = data
            self._scan_one(filename, BytesIO(data))

        self._scanned = True

    def _scan_one(self, filename, stream):
        count = 0
        it = ArchiveIterator(stream, arc2warc=True)

        for record in it:
            count += 1
            rec_headers = record.rec_headers

            entry = _ArchiveEntry(
                source=filename,
                offset=it.get_record_offset(),
                length=it.get_record_length(),
                record_id=rec_headers.get_header('WARC-Record-ID'),
                target_uri=rec_headers.get_header('WARC-Target-URI'),
                rec_type=record.rec_type,
                payload_digest=rec_headers.get_header('WARC-Payload-Digest'),
                payload_length=record.payload_length,
            )
            entry.position = len(self._all)
            entry.key = entry.record_id or ('anonymous', filename, entry.offset)
            entry.date = rec_headers.get_header('WARC-Date')
            entry.digest_key = _normalize_digest(entry.payload_digest)
            entry.concurrent_to = _header_values(rec_headers,
                                                'WARC-Concurrent-To')
            entry.refers_to = _header_values(rec_headers, 'WARC-Refers-To')
            entry.refers_to_uri = rec_headers.get_header(
                'WARC-Refers-To-Target-URI')
            entry.refers_to_date = rec_headers.get_header(
                'WARC-Refers-To-Date')

            self._all.append(entry)

            if entry.record_id:
                self._id_index.setdefault(entry.record_id, []).append(entry)
            if entry.target_uri:
                self._uri_index.setdefault(_normalize_uri(entry.target_uri),
                                           []).append(entry)
            if entry.digest_key:
                self._digest_index.setdefault(entry.digest_key,
                                              []).append(entry)
            for ref_id in entry.concurrent_to:
                self._concurrent_index.setdefault(ref_id, []).append(entry)

            if entry.rec_type == 'warcinfo':
                self._warcinfo_found = True

        self._source_counts[filename] = count

    # ------------------------------------------------------------------
    def _select_seeds(self):
        if not self.seed_ids and not self.seed_uris:
            raise ValueError('At least one seed (record ID or URL) is required')

        for record_id in self.seed_ids:
            record_id = record_id.strip()
            matched = self._id_index.get(record_id, [])
            if not matched:
                self._add_missing('record-id', record_id,
                                  'seed record ID not found in archive')
                continue
            for entry in matched:
                self._select(entry, 'seed')
                self._seed_keys.add(entry.key)

        for uri in self.seed_uris:
            all_matches = self._uri_index.get(_normalize_uri(uri), [])

            # URL seeds identify page records; request records are included
            # by the explicit concurrent policy instead of URL equality.
            matched = [entry for entry in all_matches
                       if entry.rec_type != 'request']
            if not matched:
                matched = all_matches

            if not matched:
                self._add_missing('url', uri,
                                  'seed URL not found in archive')
                continue
            for entry in matched:
                self._select(entry, 'seed')
                self._seed_keys.add(entry.key)

    # ------------------------------------------------------------------
    def _follow_relations(self):
        queue = [entry for entry in self._all if entry.key in self._seed_keys]
        inspected = set()

        while queue:
            current = queue.pop(0)
            if current.key in inspected:
                continue
            inspected.add(current.key)

            related = []
            if self.policy.follows(SubsetPolicy.CONCURRENT):
                related.extend(self._follow_concurrent(current))
            if self.policy.follows(SubsetPolicy.REFERS_TO):
                related.extend(self._follow_refers_to(current))
            if self.policy.follows(SubsetPolicy.PAYLOAD):
                related.extend(self._follow_payload(current))

            for selected in related:
                if selected.entry.key not in inspected:
                    queue.append(selected.entry)

    def _follow_concurrent(self, current):
        related = []

        for ref_id in current.concurrent_to:
            matched = self._id_index.get(ref_id, [])
            if not matched:
                self._add_missing('record-id', ref_id,
                                  'WARC-Concurrent-To target not found',
                                  referrer=current.record_id)
                continue
            related.extend(self._select(entry, 'concurrent-to')
                           for entry in matched)

        for entry in self._concurrent_index.get(current.record_id, []):
            related.append(self._select(entry, 'concurrent-to'))

        return related

    def _follow_refers_to(self, current):
        related = []

        for ref_id in current.refers_to:
            matched = self._id_index.get(ref_id, [])
            if not matched:
                self._add_missing('record-id', ref_id,
                                  'WARC-Refers-To target not found',
                                  referrer=current.record_id)
                continue
            related.extend(self._select(entry, 'refers-to')
                           for entry in matched)

        if current.refers_to_uri:
            matched = self._match_refers_to_uri(current)
            if not matched:
                self._add_missing('url', current.refers_to_uri,
                                  'WARC-Refers-To-Target-URI target not found',
                                  referrer=current.record_id)
            else:
                related.extend(self._select(entry, 'refers-to')
                               for entry in matched)

        return related

    def _match_refers_to_uri(self, current):
        matched = self._uri_index.get(_normalize_uri(current.refers_to_uri), [])
        matched = [entry for entry in matched if entry.rec_type != 'revisit']
        if not matched:
            return []

        if current.refers_to_date:
            matched = [entry for entry in matched
                       if _same_date(entry.date, current.refers_to_date)]
            if not matched:
                return []

        with_payload = [entry for entry in matched
                        if entry.payload_length > 0
                        or entry.rec_type in ('response', 'resource')]
        return with_payload or matched

    def _payload_candidates(self, current):
        candidates = []
        for entry in self._digest_index.get(current.digest_key, []):
            if entry.key == current.key or entry.rec_type == 'revisit':
                continue
            # A zero-length block can share the empty-payload digest with
            # unrelated requests; it can not explain a revisit's body.
            if entry.payload_length == 0:
                continue
            candidates.append(entry)

        candidates.sort(key=lambda entry: (
            entry.payload_length <= 0,
            0 if entry.rec_type in ('response', 'resource') else 1,
            entry.position,
        ))
        return candidates

    def _follow_payload(self, current):
        # Only revisits need another record's body. Responses that carry
        # their own payload do not, even if a digest is present.
        if current.rec_type != 'revisit':
            return []
        if not current.payload_digest or current.payload_length > 0:
            return []

        candidates = self._payload_candidates(current)
        if not candidates:
            self._add_missing('payload-digest', current.payload_digest,
                              'shared payload record not found',
                              referrer=current.record_id)
            return []

        return [self._select(candidates[0], 'payload')]

    # ------------------------------------------------------------------
    def _select_warcinfo(self):
        if not self.policy.warcinfo:
            if self._warcinfo_found:
                self._add_missing('warcinfo', '*',
                                  'warcinfo records excluded by policy')
            return

        sources = {selected.entry.source for selected in self._selected.values()}
        for entry in self._all:
            if entry.rec_type == 'warcinfo' and entry.source in sources:
                self._select(entry, 'warcinfo')

    # ------------------------------------------------------------------
    def _select(self, entry, reason):
        existing = self._selected.get(entry.key)
        if existing is not None:
            if reason not in existing.reasons:
                existing.reasons.append(reason)
            return existing

        selected = _SelectedRecord(entry)
        selected.reasons.append(reason)
        self._selected[entry.key] = selected
        return selected

    # ------------------------------------------------------------------
    def _check_limit(self):
        if self.max_records is not None and len(self._selected) > self.max_records:
            raise SubsetLimitExceeded(
                'Subset needs {0} records, more than limit {1}'.format(
                    len(self._selected), self.max_records))

    # ------------------------------------------------------------------
    def _ordered_selection(self):
        return sorted(self._selected.values(),
                      key=lambda selected: selected.entry.position)

    @property
    def selected_entries(self):
        return [selected.entry for selected in self._ordered_selection()]

    @property
    def selected_records(self):
        """Selected records in original archive order, with payloads loaded."""
        for selected in self._ordered_selection():
            yield self._get_record(selected)

    def _get_record(self, selected):
        if selected.record is None:
            selected.record = self._load_record(selected.entry)
        return selected.record

    def _load_record(self, entry):
        if entry.source in self._source_buffers:
            stream = BytesIO(self._source_buffers[entry.source])
            return self._load_record_from(entry, stream)

        with _open_source(entry.source, 'rb') as stream:
            return self._load_record_from(entry, stream)

    def _load_record_from(self, entry, stream):
        stream.seek(entry.offset)
        # no_record_parse keeps the HTTP status line and headers in the
        # raw block, so copying the block preserves every payload byte.
        it = ArchiveIterator(stream, no_record_parse=True, arc2warc=True)
        try:
            record = next(it)
        except StopIteration:
            raise SubsetError('No record at offset {0} in {1}'.format(
                entry.offset, entry.source))

        record.raw_stream = BytesIO(record.raw_stream.read())
        record.length = len(record.raw_stream.getbuffer())
        record.payload_length = record.length

        declared = record.rec_headers.get_header('Content-Length')
        if declared is not None and declared != str(record.length):
            # ARC-to-WARC conversion can synthesize headers whose
            # Content-Length counts bytes that the ARC parser consumed.
            # Without a block digest it is safe to correct the length;
            # with one, rewriting would invalidate the digest.
            if record.rec_headers.get_header('WARC-Block-Digest'):
                raise SubsetError(
                    'Record {0} at offset {1} in {2} has Content-Length {3}, '
                    'but {4} bytes remain'.format(
                        entry.record_id, entry.offset, entry.source,
                        declared, record.length))
            entry.normalized_length = True
            record.rec_headers.replace_header('Content-Length',
                                              str(record.length))

        if entry.record_id:
            record.rec_headers.replace_header('WARC-Record-ID',
                                              entry.record_id)
        return record

    # ------------------------------------------------------------------
    def _write_records(self):
        with _open_output(self.output, 'wb') as out:
            for selected in self._ordered_selection():
                record = self._get_record(selected)
                buff = _record_bytes(record)

                start = _tell(out)
                if self.gzip_output:
                    gzipping = GzippingWrapper(out)
                    gzipping.write(buff)
                    gzipping.flush()
                else:
                    out.write(buff)
                end = _tell(out)

                if start is not None and end is not None:
                    selected.export_offset = start
                    selected.export_length = end - start

    # ------------------------------------------------------------------
    def _write_manifest(self):
        data = self.manifest_data()
        with _open_output(self.manifest, 'wt') as out:
            json.dump(data, out, indent=2)
            out.write('\n')
        return data

    # ------------------------------------------------------------------
    def manifest_data(self):
        missing = list(self._missing.values())
        for item in self._unresolved_dependencies():
            if item not in missing:
                missing.append(item)

        records = []
        for selected in self._ordered_selection():
            entry = selected.entry
            item = OrderedDict([
                ('record_id', entry.record_id),
                ('record_type', entry.rec_type),
                ('target_uri', entry.target_uri),
                ('source', entry.source),
                ('source_offset', str(entry.offset)),
                ('source_length', str(entry.length)),
                ('payload_digest', entry.payload_digest),
                ('reasons', list(selected.reasons)),
            ])
            if selected.export_offset is not None:
                item['export_offset'] = str(selected.export_offset)
                item['export_length'] = str(selected.export_length)
            if entry.normalized_length:
                item['normalized_content_length'] = True
            records.append(item)

        data = OrderedDict([
            ('complete', not missing),
            ('status', 'complete' if not missing else 'incomplete'),
            ('record_count', len(records)),
            ('source_records', len(self._all)),
            ('policy', self.policy.to_dict()),
            ('seeds', OrderedDict([
                ('record_ids', list(self.seed_ids)),
                ('urls', list(self.seed_uris)),
            ])),
            ('sources', OrderedDict(
                (name, count) for name, count in self._source_counts.items())),
            ('missing', missing),
            ('missing_values', [item['value'] for item in missing]),
            ('records', records),
        ])

        if self.output:
            data['output'] = self.output

        return data

    def _unresolved_dependencies(self):
        """Dependencies of included records that are absent from the export."""
        selected_ids = set()
        selected_keys = set(self._selected)
        for selected in self._selected.values():
            entry = selected.entry
            if entry.record_id:
                selected_ids.add(entry.record_id)

        missing = []
        for selected in self._ordered_selection():
            entry = selected.entry

            for ref_id in entry.concurrent_to:
                if ref_id not in selected_ids:
                    missing.append(_missing_item(
                        'record-id', ref_id,
                        'WARC-Concurrent-To target not included',
                        entry.record_id))

            for other in self._concurrent_index.get(entry.record_id, []):
                if other.key not in selected_keys:
                    missing.append(_missing_item(
                        'record-id', other.record_id,
                        'WARC-Concurrent-To record not included',
                        entry.record_id))

            for ref_id in entry.refers_to:
                if ref_id not in selected_ids:
                    missing.append(_missing_item(
                        'record-id', ref_id,
                        'WARC-Refers-To target not included',
                        entry.record_id))

            if entry.refers_to_uri:
                matched = self._match_refers_to_uri(entry)
                if not any(item.key in selected_keys for item in matched):
                    missing.append(_missing_item(
                        'url', entry.refers_to_uri,
                        'WARC-Refers-To-Target-URI target not included',
                        entry.record_id))

            if entry.rec_type == 'revisit' and entry.payload_length <= 0:
                candidates = self._payload_candidates(entry)
                if not any(item.key in selected_keys for item in candidates):
                    missing.append(_missing_item(
                        'payload-digest', entry.payload_digest,
                        'shared payload not included',
                        entry.record_id))

        return missing

    # ------------------------------------------------------------------
    def _add_missing(self, kind, value, reason, referrer=None):
        item = _missing_item(kind, value, reason, referrer)
        key = tuple(item.items())
        if key not in self._missing:
            self._missing[key] = item

    # ------------------------------------------------------------------
    def verify(self, filename=None, require_warcinfo=None):
        """
        Verify that the exported archive explains every included record.

        Returns a list of human-readable problems; an empty list means every
        request/response pair, revisit reference, and shared payload is
        resolvable inside the export and every payload digest still matches.

        ``require_warcinfo`` defaults to the exporter's policy, so an
        explicit ``warcinfo`` exclusion is not reported as a failure.
        """
        filename = filename or self.output
        if not filename:
            raise ValueError('No output archive to verify')

        if require_warcinfo is None:
            require_warcinfo = self.policy.warcinfo

        problems = []
        records = []

        with _open_source(filename, 'rb') as stream:
            for record in ArchiveIterator(stream):
                rec_headers = record.rec_headers
                payload_digest = rec_headers.get_header('WARC-Payload-Digest')

                digester = None
                if payload_digest and record.rec_type != 'revisit':
                    from warcio.utils import Digester
                    digester = Digester(_digest_algorithm(payload_digest))

                has_payload = False
                while True:
                    piece = record.raw_stream.read(65536)
                    if not piece:
                        break
                    has_payload = True
                    if digester is not None:
                        digester.update(piece)

                computed_digest = None
                if digester is not None:
                    from warcio.digestverifyingreader import _compare_digest_rfc_3548
                    if not _compare_digest_rfc_3548(digester, payload_digest):
                        computed_digest = str(digester)

                records.append(_VerificationRecord(
                    record_id=rec_headers.get_header('WARC-Record-ID'),
                    target_uri=rec_headers.get_header('WARC-Target-URI'),
                    rec_type=record.rec_type,
                    payload_digest=payload_digest,
                    digest_key=_normalize_digest(payload_digest),
                    refers_to=rec_headers.get_header('WARC-Refers-To'),
                    refers_to_uri=rec_headers.get_header(
                        'WARC-Refers-To-Target-URI'),
                    refers_to_date=rec_headers.get_header(
                        'WARC-Refers-To-Date'),
                    date=rec_headers.get_header('WARC-Date'),
                    concurrent_to=_header_values(rec_headers,
                                                'WARC-Concurrent-To'),
                    has_payload=has_payload,
                    computed_digest=computed_digest,
                ))

        ids = {}
        for record in records:
            if not record.record_id:
                problems.append('record without WARC-Record-ID: {0}'.format(
                    record.target_uri))
                continue
            if record.record_id in ids:
                problems.append('duplicate WARC-Record-ID: ' + record.record_id)
            ids[record.record_id] = record

        digests = {}
        for record in records:
            if record.digest_key and record.has_payload:
                digests.setdefault(record.digest_key, []).append(record)

        for record in records:
            for ref_id in record.concurrent_to:
                if ref_id not in ids:
                    problems.append('WARC-Concurrent-To target missing: ' +
                                    ref_id)

            if record.record_id:
                for other in self._concurrent_index.get(record.record_id, []):
                    if other.record_id and other.record_id not in ids:
                        problems.append(
                            'WARC-Concurrent-To record missing: ' +
                            other.record_id)

            if record.refers_to and record.refers_to not in ids:
                problems.append('WARC-Refers-To target missing: ' +
                                record.refers_to)

            if record.rec_type == 'revisit':
                if not record.digest_key:
                    problems.append('revisit without payload digest: ' +
                                    str(record.record_id))
                elif not digests.get(record.digest_key):
                    problems.append('revisit payload not included: ' +
                                    str(record.record_id))

                if record.refers_to_uri and not _find_referent(record, records):
                    problems.append(
                        'revisit WARC-Refers-To target unresolved: ' +
                        str(record.record_id))

            if record.computed_digest:
                problems.append('payload digest mismatch: ' +
                                str(record.record_id))

        if require_warcinfo and records and not any(
                record.rec_type == 'warcinfo' for record in records):
            problems.append('no warcinfo record in export')

        if self._selected and self.manifest_data()['complete'] is False:
            problems.append('export manifest is incomplete: {0} unresolved '
                            'dependencies'.format(
                                len(self.manifest_data()['missing'])))

        return problems


# ============================================================================
class _ArchiveEntry(object):
    __slots__ = ('source', 'offset', 'length', 'record_id', 'target_uri',
                 'rec_type', 'payload_digest', 'digest_key', 'payload_length',
                 'concurrent_to', 'refers_to', 'refers_to_uri',
                 'refers_to_date', 'position', 'key', 'date',
                 'normalized_length')

    def __init__(self, source, offset, length, record_id, target_uri,
                 rec_type, payload_digest, payload_length):
        self.source = source
        self.offset = offset
        self.length = length
        self.record_id = record_id
        self.target_uri = target_uri
        self.rec_type = rec_type
        self.payload_digest = payload_digest
        self.digest_key = _normalize_digest(payload_digest)
        self.payload_length = payload_length
        self.concurrent_to = []
        self.refers_to = []
        self.refers_to_uri = None
        self.refers_to_date = None
        self.position = None
        self.key = None
        self.date = None
        self.normalized_length = False


class _SelectedRecord(object):
    __slots__ = ('entry', 'reasons', 'record', 'export_offset',
                 'export_length')

    def __init__(self, entry):
        self.entry = entry
        self.reasons = []
        self.record = None
        self.export_offset = None
        self.export_length = None


class _VerificationRecord(object):
    __slots__ = ('record_id', 'target_uri', 'rec_type', 'payload_digest',
                 'digest_key', 'refers_to', 'refers_to_uri', 'refers_to_date',
                 'date', 'concurrent_to', 'has_payload', 'computed_digest')

    def __init__(self, record_id, target_uri, rec_type, payload_digest,
                 digest_key, refers_to, refers_to_uri, refers_to_date, date,
                 concurrent_to, has_payload, computed_digest):
        self.record_id = record_id
        self.target_uri = target_uri
        self.rec_type = rec_type
        self.payload_digest = payload_digest
        self.digest_key = digest_key
        self.refers_to = refers_to
        self.refers_to_uri = refers_to_uri
        self.refers_to_date = refers_to_date
        self.date = date
        self.concurrent_to = concurrent_to
        self.has_payload = has_payload
        self.computed_digest = computed_digest


# ============================================================================
class SubsetError(ValueError):
    pass


class SubsetLimitExceeded(SubsetError):
    pass


# ============================================================================
def _normalize_uri(uri):
    return uri.strip() if uri else uri


def _header_values(headers, name):
    values = []
    name_lower = name.lower()

    for header_name, value in headers.headers:
        if header_name.lower() != name_lower or value is None:
            continue
        for part in re.split(r'[\s,]+', value.strip()):
            if part:
                values.append(part)

    return values


def _missing_item(kind, value, reason, referrer=None):
    item = OrderedDict([
        ('kind', kind),
        ('value', value),
        ('reason', reason),
    ])
    if referrer:
        item['referrer'] = referrer
    return item


def _find_referent(revisit, records):
    if not revisit.refers_to_uri:
        return None

    for record in records:
        if record.rec_type == 'revisit':
            continue
        if _normalize_uri(record.target_uri) != _normalize_uri(
                revisit.refers_to_uri):
            continue
        if revisit.refers_to_date and not _same_date(
                record.date, revisit.refers_to_date):
            continue
        return record

    return None


def _same_date(value, expected):
    if not value or not expected:
        return value == expected

    # WARC-Date may differ only in fractional seconds between producers.
    return value.rstrip('0') == expected.rstrip('0') or \
        value.split('.')[0] == expected.split('.')[0]


def _digest_algorithm(digest):
    return digest.split(':', 1)[0].lower() if ':' in digest else 'sha1'


def _normalize_digest(digest):
    """Normalize RFC 3548 digest encodings so hex and base32 values compare."""
    if not digest or ':' not in digest:
        return digest

    algorithm, value = digest.split(':', 1)
    try:
        from warcio.digestverifyingreader import _to_b32
        length = {'sha1': 32, 'sha256': 52}.get(algorithm.lower())
        if length and len(value) != length:
            value = _to_b32(length, value)
    except Exception:  # pragma: no cover
        return digest.lower()

    return algorithm.lower() + ':' + value


def _record_bytes(record):
    """Serialize one record without changing its ID or payload bytes."""
    out = BytesIO()
    out.write(record.rec_headers.to_bytes(encoding='utf-8'))
    if record.http_headers:
        out.write(record.http_headers.to_ascii_bytes())
    out.write(record.raw_stream.read())
    out.write(b'\r\n\r\n')
    return out.getvalue()


def _tell(out):
    try:
        return out.tell()
    except (AttributeError, OSError):  # pragma: no cover
        return None


# ============================================================================
def _open_source(filename, mode):
    """fsspec_open with stdin support for the special '-' filename."""
    if filename == '-':
        return fsspec_open(filename, mode, sys.stdin.buffer)
    return fsspec_open(filename, mode)


def _open_output(filename, mode):
    if filename == '-':
        if 'b' in mode:
            return fsspec_open(filename, mode, sys.stdout.buffer)
        return fsspec_open(filename, mode, sys.stdout)
    return fsspec_open(filename, mode)


# ============================================================================
def export_subset(inputs, output=None, manifest=None, seeds=None,
                  seed_ids=None, seed_uris=None, follow=None,
                  include_warcinfo=None, max_records=None,
                  gzip_output=None, policy=None):
    """Convenience wrapper around :class:`SubsetExporter`."""
    if policy is None:
        policy = SubsetPolicy(follow=follow,
                              include_warcinfo=include_warcinfo)

    exporter = SubsetExporter(inputs, output=output, manifest=manifest,
                              policy=policy, seed_ids=seed_ids,
                              seed_uris=seed_uris, max_records=max_records,
                              gzip_output=gzip_output)

    for seed in seeds or ():
        exporter.add_seed(seed)

    return exporter.export()
