import io
import json
from datetime import datetime, timezone

import pytest

from wayback import (
    Candidate,
    Conflict,
    MappingStatus,
    MatchType,
    UrlMappingConfig,
    export_review_csv,
    export_review_json,
    load_mapping_config,
    make_evidence,
    normalize_url,
    read_confirmations,
)
from wayback.exceptions import MappingConfigError


class TestNormalizeUrl:
    def test_lowercases_scheme_and_host_and_default_path(self):
        assert normalize_url('HTTP://EXAMPLE.com') == 'http://example.com/'

    def test_strips_default_ports_but_keeps_non_default(self):
        assert normalize_url('http://example.com:80/a') == 'http://example.com/a'
        assert normalize_url('https://example.com:443/a') == 'https://example.com/a'
        assert normalize_url('http://example.com:8080/a') == 'http://example.com:8080/a'

    def test_preserves_query_fragment_and_path_casing(self):
        assert normalize_url('http://example.com/aB?x=Y#Z') == 'http://example.com/aB?x=Y#Z'

    def test_strips_surrounding_whitespace(self):
        assert normalize_url('  http://example.com/  ') == 'http://example.com/'

    def test_rejects_relative_urls_and_non_strings(self):
        with pytest.raises(ValueError):
            normalize_url('/just/a/path')
        with pytest.raises(TypeError):
            normalize_url(None)


def make_config(**overrides):
    rules = overrides.pop(
        'rules',
        [
            {
                'id': 'domain-move',
                'match': 'http://old.example.gov/*',
                'new': 'https://new.example.gov/{*}',
            },
        ],
    )
    known = overrides.pop('known_mappings', None)
    return UrlMappingConfig(known_mappings=known, rules=rules)


class TestKnownMappings:
    def test_known_pair_is_confirmed(self):
        config = UrlMappingConfig(
            known_mappings={'http://old.example.gov/a': 'https://new.example.gov/b'}, rules=[]
        )
        result = config.apply('http://old.example.gov/a')
        assert result.status == MappingStatus.confirmed
        assert result.is_confirmed
        assert result.new_urls == ('https://new.example.gov/b',)
        assert result.candidates == (Candidate('https://new.example.gov/b', None, MatchType.exact),)

    def test_known_one_to_many_is_retained_not_merged(self):
        config = UrlMappingConfig(
            known_mappings={
                'http://old.example.gov/multi': [
                    'https://new.example.gov/a',
                    'https://new.example.gov/b',
                ],
            },
            rules=[],
        )
        result = config.apply('http://old.example.gov/multi')
        assert result.new_urls == ('https://new.example.gov/a', 'https://new.example.gov/b')
        assert [conflict.kind for conflict in result.conflicts] == ['one_to_many']
        assert result.conflicts[0].urls == ('https://new.example.gov/a', 'https://new.example.gov/b')

    def test_duplicate_known_mapping_raises(self):
        with pytest.raises(MappingConfigError):
            UrlMappingConfig(
                known_mappings={
                    'http://old.example.gov/a': 'https://new.example.gov/b',
                    'http://OLD.example.gov:80/a': 'https://new.example.gov/c',
                }
            )

    def test_known_mappings_are_normalized(self):
        config = make_config(known_mappings={'HTTP://OLD.example.gov:80/a': 'HTTPS://NEW.example.gov:443/b'})
        assert config.known_mappings == {'http://old.example.gov/a': ('https://new.example.gov/b',)}


class TestRules:
    def test_wildcard_rule_generates_candidate(self):
        config = make_config()
        result = config.apply('http://old.example.gov/deep/page')
        assert result.status == MappingStatus.candidate
        assert result.new_urls == ('https://new.example.gov/deep/page',)
        assert result.candidates[0].match_type == MatchType.rule
        assert result.candidates[0].rule_id == 'domain-move'

    def test_named_parameter_rule(self):
        config = make_config(
            rules=[
                {
                    'id': 'reports',
                    'match': 'http://old.example.gov/reports/{id}',
                    'new': 'https://new.example.gov/publications/{id}',
                }
            ]
        )
        result = config.apply('http://old.example.gov/reports/r-2020-1')
        assert result.new_urls == ('https://new.example.gov/publications/r-2020-1',)

    def test_multiple_wildcards_and_trailing_slash_distinction(self):
        config = make_config(
            rules=[
                {
                    'id': 'sections',
                    'match': 'http://old.example.gov/*/{page}',
                    'new': 'https://new.example.gov/{page}/{*}',
                }
            ]
        )
        result = config.apply('http://old.example.gov/sec/p1')
        assert result.new_urls == ('https://new.example.gov/p1/sec',)
        # Similar but distinct URLs must not be merged.
        results = config.build_table(['http://old.example.gov/sec/p1', 'http://old.example.gov/sec/p1/'])
        assert [r.old_url for r in results] == [
            'http://old.example.gov/sec/p1',
            'http://old.example.gov/sec/p1/',
        ]
        assert results[0].new_urls != results[1].new_urls

    def test_non_matching_url_is_unresolved(self):
        config = make_config()
        result = config.apply('http://elsewhere.example.net/anything')
        assert result.status == MappingStatus.unresolved
        assert result.candidates == ()
        assert result.conflicts == ()

    def test_path_only_rule_and_template_preserve_host_and_query(self):
        config = make_config(
            rules=[
                {
                    'id': 'path-rule',
                    'match': '/old-section/{rest}',
                    'new': '/new-section/{rest}',
                }
            ]
        )
        result = config.apply('http://old.example.gov/old-section/page?x=1#frag')
        assert result.new_urls == ('http://old.example.gov/new-section/page?x=1#frag',)

    def test_rules_with_same_output_keep_provenance_but_no_conflict(self):
        config = make_config(
            rules=[
                {'id': 'r1', 'match': 'http://old.example.gov/*', 'new': 'https://new.example.gov/{*}'},
                {'id': 'r2', 'match': 'http://old.example.gov/*', 'new': 'https://new.example.gov/{*}'},
            ]
        )
        result = config.apply('http://old.example.gov/a')
        assert [candidate.rule_id for candidate in result.candidates] == ['r1', 'r2']
        assert result.conflicts == ()

    def test_rule_conflict_is_retained(self):
        config = make_config(
            rules=[
                {'id': 'r1', 'match': 'http://old.example.gov/{p}', 'new': 'https://new.example.gov/{p}'},
                {'id': 'r2', 'match': 'http://old.example.gov/{p}', 'new': 'https://archive.example.gov/{p}'},
            ]
        )
        result = config.apply('http://old.example.gov/a')
        assert result.new_urls == (
            'https://new.example.gov/a',
            'https://archive.example.gov/a',
        )
        kinds = [conflict.kind for conflict in result.conflicts]
        assert 'rule_conflict' in kinds
        assert 'one_to_many' in kinds
        conflict = next(conflict for conflict in result.conflicts if conflict.kind == 'rule_conflict')
        assert conflict.rule_ids == ('r1', 'r2')


class TestExceptions:
    def test_blocking_exception_skips_rule_and_records_note(self):
        config = make_config(
            rules=[
                {
                    'id': 'domain-move',
                    'match': 'http://old.example.gov/*',
                    'new': 'https://new.example.gov/{*}',
                    'exceptions': [
                        {'match': 'http://old.example.gov/retired/*', 'note': 'section retired, no replacement'},
                    ],
                }
            ]
        )
        result = config.apply('http://old.example.gov/retired/page')
        assert result.status == MappingStatus.unresolved
        assert result.candidates == ()
        assert 'section retired' in result.notes[0]

    def test_overriding_exception_replaces_rule_output(self):
        config = make_config(
            rules=[
                {
                    'id': 'domain-move',
                    'match': 'http://old.example.gov/*',
                    'new': 'https://new.example.gov/{*}',
                    'exceptions': [
                        {'match': 'http://old.example.gov/special', 'new': 'https://new.example.gov/other'},
                    ],
                }
            ]
        )
        result = config.apply('http://old.example.gov/special')
        assert result.new_urls == ('https://new.example.gov/other',)
        assert result.candidates[0].match_type == MatchType.exception


class TestRuleVsKnown:
    def test_disagreeing_rule_output_is_retained_with_conflict(self):
        config = make_config(
            known_mappings={'http://old.example.gov/a': 'https://new.example.gov/known-a'},
        )
        result = config.apply('http://old.example.gov/a')
        assert result.status == MappingStatus.confirmed
        assert 'https://new.example.gov/known-a' in result.new_urls
        assert 'https://new.example.gov/a' in result.new_urls
        assert [conflict.kind for conflict in result.conflicts] == ['rule_vs_known']
        # The exact pair stays first and explicitly sourced.
        assert result.candidates[0] == Candidate('https://new.example.gov/known-a', None, MatchType.exact)


class TestBuildTable:
    def test_dedupes_sorts_and_accepts_cdx_like_objects(self):
        class FakeCdx:
            def __init__(self, original):
                self.original = original

        config = make_config()
        table = config.build_table(
            [
                FakeCdx('http://old.example.gov/b'),
                'http://old.example.gov/a',
                'http://OLD.example.gov:80/a',
            ]
        )
        assert [result.old_url for result in table] == [
            'http://old.example.gov/a',
            'http://old.example.gov/b',
        ]

    def test_many_to_one_conflict_across_rows(self):
        config = UrlMappingConfig(
            known_mappings={
                'http://s.gov/a': 'http://t.gov/z',
                'http://s.gov/b': 'http://t.gov/z',
            }
        )
        table = config.build_table(['http://s.gov/a', 'http://s.gov/b'])
        for result in table:
            conflict = next(conflict for conflict in result.conflicts if conflict.kind == 'many_to_one')
            assert conflict.urls == ('http://t.gov/z',)
            assert set(conflict.related_old_urls) == {'http://s.gov/a', 'http://s.gov/b'}

    def test_evidence_is_attached_but_does_not_affect_status(self):
        config = make_config()
        evidence = {
            'http://old.example.gov/a': make_evidence(captured_at='20200101000000', summary='digest:abc'),
            'http://old.example.gov/b': [
                make_evidence(captured_at='20190101000000', summary='digest:old'),
                make_evidence(captured_at='20210101000000', summary='digest:new'),
            ],
        }
        table = config.build_table(['http://old.example.gov/a', 'http://old.example.gov/b'], evidence=evidence)
        assert all(result.status == MappingStatus.candidate for result in table)
        assert table[0].evidence == ({'captured_at': '20200101000000', 'summary': 'digest:abc'},)
        assert len(table[1].evidence) == 2

    def test_real_cdx_record_original_attribute(self):
        from wayback import CdxRecord

        record = CdxRecord(
            urlkey='gov,example,old)/a',
            timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc),
            original='http://old.example.gov/a',
            mimetype='text/html',
            statuscode=200,
            digest='ABC',
            length=10,
        )
        table = make_config().build_table([record])
        assert table[0].old_url == 'http://old.example.gov/a'


class TestSafeTargets:
    def test_only_unambiguous_confirmed_mappings_are_safe(self):
        config = UrlMappingConfig(
            known_mappings={
                'http://old.example.gov/ok': 'https://new.example.gov/ok',
                'http://old.example.gov/multi': [
                    'https://new.example.gov/a',
                    'https://new.example.gov/b',
                ],
            },
            rules=[
                {
                    'id': 'domain-move',
                    'match': 'http://old.example.gov/*',
                    'new': 'https://new.example.gov/{*}',
                }
            ],
        )
        safe = config.safe_targets()
        assert safe == {'http://old.example.gov/ok': ('https://new.example.gov/ok',)}

    def test_candidates_are_never_safe(self):
        config = make_config()
        table = config.build_table(['http://old.example.gov/a'])
        assert config.safe_targets(table) == {}

    def test_confirmed_many_to_one_pairs_remain_individually_safe(self):
        config = UrlMappingConfig(
            known_mappings={
                'http://s.gov/a': 'http://t.gov/z',
                'http://s.gov/b': 'http://t.gov/z',
            }
        )
        table = config.build_table(['http://s.gov/a', 'http://s.gov/b'])
        safe = config.safe_targets(table)
        assert safe == {
            'http://s.gov/a': ('http://t.gov/z',),
            'http://s.gov/b': ('http://t.gov/z',),
        }


class TestConfigErrors:
    def test_missing_rule_keys(self):
        with pytest.raises(MappingConfigError):
            UrlMappingConfig(rules=[{'id': 'bad', 'match': 'http://x/*'}])

    def test_duplicate_pattern_parameter(self):
        with pytest.raises(MappingConfigError):
            UrlMappingConfig(rules=[{'match': 'http://x/{p}/{p}', 'new': 'http://y/{p}'}])

    def test_unknown_template_parameter(self):
        with pytest.raises(MappingConfigError):
            UrlMappingConfig(rules=[{'match': 'http://x/{p}', 'new': 'http://y/{q}'}])

    def test_wildcard_count_mismatch(self):
        with pytest.raises(MappingConfigError):
            UrlMappingConfig(rules=[{'match': 'http://x/*', 'new': 'http://y/{*}/{*}'}])

    def test_wildcard_placeholder_in_match_pattern(self):
        with pytest.raises(MappingConfigError):
            UrlMappingConfig(rules=[{'match': 'http://x/{*}', 'new': 'http://y/{*}'}])

    def test_bad_exception(self):
        with pytest.raises(MappingConfigError):
            UrlMappingConfig(
                rules=[
                    {
                        'match': 'http://x/*',
                        'new': 'http://y/{*}',
                        'exceptions': [{'new': 'http://y/z'}],
                    }
                ]
            )

    def test_from_dict_rejects_non_mapping(self):
        with pytest.raises(MappingConfigError):
            UrlMappingConfig.from_dict(['not', 'a', 'mapping'])


class TestExportAndReview:
    def test_csv_rows_include_candidates_conflicts_and_unresolved(self):
        config = make_config(
            known_mappings={'http://old.example.gov/known': 'https://new.example.gov/known'},
        )
        table = config.build_table(
            [
                'http://old.example.gov/known',
                'http://elsewhere.example.net/unknown',
            ]
        )
        stream = io.StringIO()
        export_review_csv(table, stream)
        rows = stream.getvalue().splitlines()
        assert rows[0].startswith('old_url,status,candidate_new_url')
        assert any('confirmed' in row and 'https://new.example.gov/known' in row for row in rows)
        unresolved_row = next(row for row in rows if 'unresolved' in row)
        assert unresolved_row.split(',')[2] == ''

    def test_json_review_roundtrip_only_returns_confirmed_selections(self, tmp_path):
        config = make_config()
        table = config.build_table(['http://old.example.gov/a', 'http://old.example.gov/b'])
        path = tmp_path / 'review.json'
        export_review_json(table, str(path))
        payload = json.loads(path.read_text())
        assert all(entry['confirmed_new_urls'] == [] for entry in payload)
        payload[0]['confirmed_new_urls'] = [payload[0]['candidates'][0]['new_url']]
        path.write_text(json.dumps(payload))

        confirmations = read_confirmations(str(path))
        assert confirmations == {'http://old.example.gov/a': ('https://new.example.gov/a',)}

    def test_load_mapping_config_from_json(self, tmp_path):
        path = tmp_path / 'mapping.json'
        path.write_text(
            json.dumps(
                {
                    'known_mappings': {'http://old.example.gov/a': 'https://new.example.gov/b'},
                    'rules': [
                        {'id': 'r1', 'match': 'http://old.example.gov/*', 'new': 'https://new.example.gov/{*}'}
                    ],
                }
            )
        )
        config = load_mapping_config(str(path))
        assert config.apply('http://old.example.gov/a').is_confirmed
        assert config.apply('http://old.example.gov/c').new_urls == ('https://new.example.gov/c',)

    def test_conflict_serializes_all_fields(self, tmp_path):
        config = make_config(
            rules=[
                {'id': 'r1', 'match': 'http://old.example.gov/{p}', 'new': 'https://new.example.gov/{p}'},
                {'id': 'r2', 'match': 'http://old.example.gov/{p}', 'new': 'https://archive.example.gov/{p}'},
            ]
        )
        table = config.build_table(['http://old.example.gov/a'])
        path = tmp_path / 'review.json'
        export_review_json(table, str(path))
        payload = json.loads(path.read_text())
        conflict = next(c for c in payload[0]['conflicts'] if c['kind'] == 'rule_conflict')
        assert conflict['rule_ids'] == ['r1', 'r2']
        assert isinstance(conflict['urls'], list) and len(conflict['urls']) == 2


def test_namedtuple_shapes():
    candidate = Candidate('https://new.example.gov/a', 'r1', MatchType.rule)
    assert candidate.new_url == 'https://new.example.gov/a'
    conflict = Conflict(kind='one_to_many', urls=(candidate.new_url,))
    assert conflict.related_old_urls == ()
