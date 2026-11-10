"""
Capture interaction query: pair request and response records in a
WARC/ARC into request/response interactions.

Pairing rules:

- Explicit links recorded in the archive (``WARC-Concurrent-To`` and
  ``WARC-Refers-To``) always take precedence over any heuristic.
- When no explicit link exists, candidate pairs are only proposed under
  caller-supplied constraints: a maximum time delta between the request
  and response ``WARC-Date`` (``max_time_delta``) and, optionally, a
  restriction to specific target URLs (``urls``). If ``max_time_delta``
  is not given, no heuristic pairing is attempted at all.
- If a response matches more than one possible request (eg. the same
  URL requested several times in a short period), the ambiguity is
  preserved: all candidates are reported and no pair is picked.
- A redirect (3xx + Location) and the follow-up request it triggers are
  reported as separate interactions; the redirect target is annotated
  on the response but never merged into a single interaction.
- Duplicate record ids and contradictory explicit relations are
  reported as conflicts, never silently overwritten.
- The result is independent of the physical record order in the
  archive: explicit pairs are resolved by record id, and heuristic
  candidates are computed over the full record set, so shuffling
  records changes neither the explicit pairs nor turns ambiguous
  evidence into a definite answer.
"""

import json
import os
import sys

from collections import OrderedDict

from warcio.archiveiterator import ArchiveIterator
from warcio.timeutils import iso_date_to_datetime
from warcio.utils import fsspec_open


# ============================================================================
def get_all_headers(headers, name):
    """Return all values for a (possibly repeated) header."""
    name_lower = name.lower()
    return [value for (header, value) in headers.headers
            if header.lower() == name_lower]


# ============================================================================
class RecordSummary(object):
    """Summary of a single request/response/revisit record, holding
    everything needed to pair records and describe them in a report.
    """
    def __init__(self, record, offset=None, length=None, filename=None):
        rec_headers = record.rec_headers

        self.rec_type = record.rec_type
        self.record_id = rec_headers.get_header('WARC-Record-ID')
        self.url = rec_headers.get_header('WARC-Target-URI')
        self.date = rec_headers.get_header('WARC-Date')

        self.concurrent_to = get_all_headers(rec_headers, 'WARC-Concurrent-To')
        self.refers_to = get_all_headers(rec_headers, 'WARC-Refers-To')

        self.offset = offset
        self.length = length
        self.filename = filename

        self.http_status = None
        self.http_method = None
        self.redirect_location = None

        if record.http_headers:
            if self.rec_type == 'request':
                self.http_method = record.http_headers.protocol
            else:
                self.http_status = record.http_headers.get_statuscode()
                if self.http_status.startswith('3'):
                    location = record.http_headers.get_header('Location')
                    if location:
                        self.redirect_location = location

        self.dt = self._parse_date(self.date)

    @staticmethod
    def _parse_date(date):
        if not date:
            return None
        try:
            return iso_date_to_datetime(date)
        except Exception:
            return None

    def sort_key(self):
        return (self.date or '',
                self.record_id or '',
                self.url or '',
                self.http_status or self.http_method or '',
                self.offset if self.offset is not None else -1)

    def to_dict(self):
        result = OrderedDict()
        result['record_id'] = self.record_id
        result['type'] = self.rec_type
        if self.url is not None:
            result['url'] = self.url
        if self.date is not None:
            result['date'] = self.date
        if self.http_method:
            result['http:method'] = self.http_method
        if self.http_status:
            result['http:status'] = self.http_status
        if self.redirect_location:
            result['redirect'] = OrderedDict([('status', self.http_status),
                                              ('location', self.redirect_location)])
        if self.filename:
            result['filename'] = self.filename
        if self.offset is not None:
            result['offset'] = str(self.offset)
        return result


# ============================================================================
class InteractionQuery(object):
    """Collects record summaries and computes request/response
    interactions according to the rules described in the module docstring.
    """
    REQUEST_TYPE = 'request'
    RESPONSE_TYPES = ('response', 'revisit')
    TRACKED_TYPES = (REQUEST_TYPE,) + RESPONSE_TYPES

    EXPLICIT_HEADERS = (('WARC-Concurrent-To', 'concurrent_to'),
                        ('WARC-Refers-To', 'refers_to'))

    def __init__(self, max_time_delta=None, urls=None):
        # heuristic pairing is only allowed when the caller supplies
        # a maximum time delta (in seconds)
        self.max_time_delta = max_time_delta
        self.urls = set(urls) if urls else None

        self.requests = []
        self.responses = []

    def add_record(self, summary):
        if self.urls is not None and summary.url not in self.urls:
            return

        if summary.rec_type == self.REQUEST_TYPE:
            self.requests.append(summary)
        elif summary.rec_type in self.RESPONSE_TYPES:
            self.responses.append(summary)

    def all_records(self):
        return self.requests + self.responses

    def report(self):
        conflicts = []

        id_map = self._build_id_map(conflicts)
        edges = self._collect_explicit_edges(id_map, conflicts)

        interactions, ambiguous, paired, conflicted = \
            self._resolve_explicit(edges, conflicts)

        heuristic_interactions, heuristic_ambiguous, unpaired = \
            self._resolve_heuristic(paired, conflicted)

        interactions.extend(heuristic_interactions)
        ambiguous.extend(heuristic_ambiguous)

        interactions.sort(key=lambda entry: (entry['response']['date'] or '',
                                             entry['response']['record_id'] or '',
                                             entry['request']['record_id'] or ''))
        ambiguous.sort(key=lambda entry: (entry['record'].get('date') or '',
                                          entry['record'].get('record_id') or ''))
        conflicts.sort(key=lambda entry: json.dumps(entry, sort_keys=True))

        return OrderedDict([('interactions', interactions),
                            ('ambiguous', ambiguous),
                            ('unpaired', unpaired),
                            ('conflicts', conflicts)])

    # ------------------------------------------------------------
    def _build_id_map(self, conflicts):
        id_map = {}
        for summary in self.all_records():
            if summary.record_id:
                id_map.setdefault(summary.record_id, []).append(summary)

        for record_id in sorted(id_map):
            records = id_map[record_id]
            if len(records) > 1:
                records = sorted(records, key=RecordSummary.sort_key)
                conflicts.append(OrderedDict([
                    ('type', 'duplicate-record-id'),
                    ('record_id', record_id),
                    ('records', [rec.to_dict() for rec in records]),
                ]))

        return id_map

    # ------------------------------------------------------------
    def _collect_explicit_edges(self, id_map, conflicts):
        """Resolve explicit references to request<->response edges.

        Returns a dict of (request, response) -> set of header names
        that assert the link. Unresolvable references are reported as
        conflicts and never silently dropped or overwritten.
        """
        edges = {}

        for summary in self.all_records():
            for header_name, attr in self.EXPLICIT_HEADERS:
                for ref in getattr(summary, attr):
                    targets = id_map.get(ref, [])

                    if not targets:
                        conflicts.append(OrderedDict([
                            ('type', 'unresolved-reference'),
                            ('header', header_name),
                            ('source', summary.to_dict()),
                            ('reference', ref),
                        ]))
                        continue

                    if len(targets) > 1:
                        targets = sorted(targets, key=RecordSummary.sort_key)
                        conflicts.append(OrderedDict([
                            ('type', 'ambiguous-reference'),
                            ('header', header_name),
                            ('source', summary.to_dict()),
                            ('reference', ref),
                            ('matches', [rec.to_dict() for rec in targets]),
                        ]))
                        continue

                    target = targets[0]

                    if (summary.rec_type == self.REQUEST_TYPE and
                            target.rec_type in self.RESPONSE_TYPES):
                        req, resp = summary, target
                    elif (summary.rec_type in self.RESPONSE_TYPES and
                            target.rec_type == self.REQUEST_TYPE):
                        req, resp = target, summary
                    else:
                        # eg. a revisit referring to another response:
                        # not a request/response interaction, ignore
                        continue

                    edges.setdefault((req, resp), set()).add(header_name)

        return edges

    # ------------------------------------------------------------
    def _resolve_explicit(self, edges, conflicts):
        req_neighbors = {}
        resp_neighbors = {}
        for (req, resp) in edges:
            req_neighbors.setdefault(req, set()).add(resp)
            resp_neighbors.setdefault(resp, set()).add(req)

        interactions = []
        ambiguous = []
        paired = set()
        conflicted = set()

        # contradictory relations: a record explicitly linked to more
        # than one counterpart -- report, never silently pick one
        for records, neighbors in ((req_neighbors, 'request'),
                                   (resp_neighbors, 'response')):
            for record, others in records.items():
                if len(others) <= 1:
                    continue
                conflicted.add(record)
                others = sorted(others, key=RecordSummary.sort_key)
                via = set()
                for other in others:
                    req, resp = (record, other) if records is req_neighbors \
                                else (other, record)
                    via.update(edges[(req, resp)])

                conflicts.append(OrderedDict([
                    ('type', 'contradictory-links'),
                    ('record', record.to_dict()),
                    ('linked_records', [rec.to_dict() for rec in others]),
                    ('via', sorted(via)),
                ]))
                ambiguous.append(OrderedDict([
                    ('record', record.to_dict()),
                    ('candidates', [rec.to_dict() for rec in others]),
                    ('reason', 'contradictory-explicit-links'),
                ]))

        for (req, resp), headers in edges.items():
            if req in conflicted or resp in conflicted:
                continue

            paired.add(req)
            paired.add(resp)

            basis = 'explicit:' + '+'.join(sorted(headers))
            evidence = 'linked by ' + ' and '.join(sorted(headers))
            interactions.append(self._make_interaction(req, resp, basis,
                                                       'explicit', evidence))

        return interactions, ambiguous, paired, conflicted

    # ------------------------------------------------------------
    def _resolve_heuristic(self, paired, conflicted):
        interactions = []
        ambiguous = []

        free_requests = [req for req in self.requests
                         if req not in paired and req not in conflicted]
        free_responses = [resp for resp in self.responses
                          if resp not in paired and resp not in conflicted]

        unpaired_requests = []
        unpaired_responses = []

        if self.max_time_delta is None:
            # no caller-allowed time condition: no heuristic pairing
            unpaired_requests = [self._unpaired(req, 'heuristic-disabled')
                                 for req in free_requests]
            unpaired_responses = [self._unpaired(resp, 'heuristic-disabled')
                                  for resp in free_responses]
        else:
            tentative = {}
            for resp in sorted(free_responses, key=RecordSummary.sort_key):
                candidates = self._find_candidates(resp, free_requests)

                if len(candidates) == 1:
                    tentative[resp] = candidates[0]
                elif len(candidates) > 1:
                    # one response, several possible requests:
                    # keep the ambiguity, do not pick one
                    ambiguous.append(OrderedDict([
                        ('record', resp.to_dict()),
                        ('candidates', [rec.to_dict() for rec in candidates]),
                        ('reason', 'multiple-requests-within-time-window'),
                    ]))
                else:
                    unpaired_responses.append(
                        self._unpaired(resp, 'no-request-within-time-window'))

            # a request tentatively matched to several responses can not
            # be assigned to any of them with certainty
            req_to_resps = {}
            for resp, req in tentative.items():
                req_to_resps.setdefault(req, []).append(resp)

            for resp in sorted(tentative, key=RecordSummary.sort_key):
                req = tentative[resp]
                if len(req_to_resps[req]) > 1:
                    ambiguous.append(OrderedDict([
                        ('record', resp.to_dict()),
                        ('candidates', [req.to_dict()]),
                        ('reason', 'candidate-request-claimed-by-multiple-responses'),
                    ]))
                    continue

                delta = abs((req.dt - resp.dt).total_seconds())
                evidence = ('same target url, |request date - response date| '
                            '= {0:g}s <= max_time_delta {1:g}s'
                            .format(delta, self.max_time_delta))
                interactions.append(self._make_interaction(
                    req, resp, 'heuristic:url+time', 'candidate', evidence))

            heuristically_paired = set(req for req, resps in req_to_resps.items()
                                       if len(resps) == 1)
            for req in free_requests:
                if req not in heuristically_paired:
                    unpaired_requests.append(
                        self._unpaired(req, 'no-matching-response'))

        unpaired_requests.sort(key=lambda entry: (entry['record'].get('date') or '',
                                                  entry['record'].get('record_id') or ''))
        unpaired_responses.sort(key=lambda entry: (entry['record'].get('date') or '',
                                                   entry['record'].get('record_id') or ''))

        unpaired = OrderedDict([('requests', unpaired_requests),
                                ('responses', unpaired_responses)])

        return interactions, ambiguous, unpaired

    def _find_candidates(self, resp, requests):
        candidates = []
        for req in requests:
            if not req.url or req.url != resp.url:
                continue
            if not req.dt or not resp.dt:
                continue
            delta = abs((req.dt - resp.dt).total_seconds())
            if delta > self.max_time_delta:
                continue
            candidates.append(req)

        candidates.sort(key=RecordSummary.sort_key)
        return candidates

    # ------------------------------------------------------------
    @staticmethod
    def _make_interaction(req, resp, basis, confidence, evidence):
        return OrderedDict([('request', req.to_dict()),
                            ('response', resp.to_dict()),
                            ('basis', basis),
                            ('confidence', confidence),
                            ('evidence', evidence)])

    @staticmethod
    def _unpaired(summary, reason):
        return OrderedDict([('record', summary.to_dict()),
                            ('reason', reason)])


# ============================================================================
def query_interactions(fileobj, max_time_delta=None, urls=None, filename=None):
    """Read an archive stream and return the interaction report."""
    query = InteractionQuery(max_time_delta=max_time_delta, urls=urls)

    it = ArchiveIterator(fileobj, arc2warc=True)
    for record in it:
        if record.rec_type not in InteractionQuery.TRACKED_TYPES:
            continue

        summary = RecordSummary(record,
                                offset=it.get_record_offset(),
                                length=it.get_record_length(),
                                filename=filename)
        query.add_record(summary)

    return query.report()


# ============================================================================
class Interactions(object):
    """Command-line front end for the interaction query, mirroring
    the Indexer interface.
    """
    def __init__(self, inputs, output, max_time_delta=None, urls=None):
        self.inputs = inputs
        self.output = output
        self.max_time_delta = max_time_delta
        self.urls = urls

    def process_all(self):
        query = InteractionQuery(max_time_delta=self.max_time_delta,
                                 urls=self.urls)

        for filename in self.inputs:
            try:
                stdin = sys.stdin.buffer
            except AttributeError:  # py2
                stdin = sys.stdin
            with fsspec_open(filename, 'rb', stdin) as fh:
                self.process_one(fh, query, filename)

        report = query.report()

        with fsspec_open(self.output, 'wt', sys.stdout) as out:
            out.write(json.dumps(report, indent=2) + '\n')

    def process_one(self, input_, query, filename):
        it = ArchiveIterator(input_, arc2warc=True)
        for record in it:
            if record.rec_type not in InteractionQuery.TRACKED_TYPES:
                continue

            summary = RecordSummary(record,
                                    offset=it.get_record_offset(),
                                    length=it.get_record_length(),
                                    filename=os.path.basename(filename))
            query.add_record(summary)
