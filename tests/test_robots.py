from datetime import UTC, datetime

from w3lib.robots import (
    IndexingPermission,
    indexing_permission,
    merge_robots_rules,
    parse_meta_robots,
    parse_x_robots_tag,
)

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
PAST = datetime(2020, 1, 1, tzinfo=UTC)
FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


class TestDefaults:
    def test_no_declarations(self):
        permission = indexing_permission()
        assert permission == IndexingPermission()
        assert permission.can_index is True
        assert permission.can_follow is True
        assert permission.max_snippet is None
        assert permission.unavailable is None
        assert permission.unknown_directives == ()

    def test_empty_declarations(self):
        permission = indexing_permission(x_robots_tag="", html="<html></html>")
        assert permission.can_index is True
        assert permission.can_follow is True


class TestXRobotsTag:
    def test_noindex(self):
        permission = indexing_permission(x_robots_tag="noindex")
        assert permission.can_index is False
        assert permission.index_source == "X-Robots-Tag"
        assert permission.can_follow is True

    def test_nofollow(self):
        permission = indexing_permission(x_robots_tag="nofollow")
        assert permission.can_follow is False
        assert permission.follow_source == "X-Robots-Tag"
        assert permission.can_index is True

    def test_none_means_noindex_nofollow(self):
        permission = indexing_permission(x_robots_tag="none")
        assert permission.can_index is False
        assert permission.can_follow is False

    def test_multiple_headers_merge(self):
        permission = indexing_permission(x_robots_tag=["noindex", "nofollow"])
        assert permission.can_index is False
        assert permission.can_follow is False

    def test_bytes_value(self):
        permission = indexing_permission(x_robots_tag=b"noindex")
        assert permission.can_index is False

    def test_crawler_specific(self):
        permission = indexing_permission(
            x_robots_tag="googlebot: noindex, nofollow",
            user_agent="Mozilla/5.0 (compatible; Googlebot/2.1)",
        )
        assert permission.can_index is False
        assert permission.can_follow is False
        assert permission.index_source == "X-Robots-Tag (googlebot)"

    def test_crawler_specific_not_matching(self):
        permission = indexing_permission(
            x_robots_tag="googlebot: noindex",
            user_agent="bingbot/2.0",
        )
        assert permission.can_index is True

    def test_crawler_specific_without_user_agent(self):
        permission = indexing_permission(x_robots_tag="googlebot: noindex")
        assert permission.can_index is True

    def test_crawler_specific_overrides_generic(self):
        permission = indexing_permission(
            x_robots_tag=["noindex, nofollow", "googlebot: follow"],
            user_agent="Googlebot",
        )
        # The specific declaration replaces the generic one entirely.
        assert permission.can_index is True
        assert permission.can_follow is True

    def test_crawler_prefix_applies_to_following_directives(self):
        (rule,) = parse_x_robots_tag("googlebot: noindex, nofollow", "googlebot")
        assert rule.noindex is True
        assert rule.nofollow is True

    def test_directive_with_value_is_not_a_crawler_prefix(self):
        (rule,) = parse_x_robots_tag("max-snippet: 50")
        assert rule.specific is False
        assert rule.max_snippet == 50

    def test_case_insensitive(self):
        permission = indexing_permission(x_robots_tag="NoIndex")
        assert permission.can_index is False


class TestMetaRobots:
    def test_meta_noindex(self):
        permission = indexing_permission(
            html='<html><head><meta name="robots" content="noindex"></head></html>'
        )
        assert permission.can_index is False
        assert permission.index_source == '<meta name="robots">'

    def test_meta_crawler_specific(self):
        permission = indexing_permission(
            html='<meta name="googlebot" content="nofollow">',
            user_agent="Googlebot/2.1",
        )
        assert permission.can_follow is False
        assert permission.follow_source == '<meta name="googlebot">'

    def test_meta_crawler_specific_not_matching(self):
        permission = indexing_permission(
            html='<meta name="googlebot" content="nofollow">',
            user_agent="bingbot",
        )
        assert permission.can_follow is True

    def test_non_robots_meta_ignored(self):
        permission = indexing_permission(
            html='<meta name="description" content="noindex is not a directive here">'
        )
        assert permission.can_index is True
        assert permission.unknown_directives == ()

    def test_meta_in_script_ignored(self):
        permission = indexing_permission(
            html='<script>var x = "<meta name=\'robots\' content=\'noindex\'>";</script>'
        )
        assert permission.can_index is True

    def test_entities_in_content(self):
        permission = indexing_permission(
            html='<meta name="robots" content="noindex&#x2c; nofollow">'
        )
        assert permission.can_index is False
        assert permission.can_follow is False


class TestSnippetLimits:
    def test_max_snippet(self):
        permission = indexing_permission(x_robots_tag="max-snippet: 50")
        assert permission.max_snippet == 50
        assert permission.max_snippet_source == "X-Robots-Tag"
        assert permission.can_index is True

    def test_max_snippet_smallest_wins(self):
        permission = indexing_permission(
            x_robots_tag="max-snippet: 100",
            html='<meta name="robots" content="max-snippet: 20">',
        )
        assert permission.max_snippet == 20
        assert permission.max_snippet_source == '<meta name="robots">'

    def test_max_snippet_minus_one_is_unlimited(self):
        permission = indexing_permission(x_robots_tag="max-snippet: -1")
        assert permission.max_snippet is None
        assert permission.max_snippet_source is None

    def test_max_snippet_number_beats_minus_one(self):
        permission = indexing_permission(
            x_robots_tag=["max-snippet: -1", "max-snippet: 30"]
        )
        assert permission.max_snippet == 30

    def test_nosnippet(self):
        permission = indexing_permission(x_robots_tag="nosnippet")
        assert permission.nosnippet is True
        assert permission.nosnippet_source == "X-Robots-Tag"
        assert permission.max_snippet == 0
        assert permission.max_snippet_source == "X-Robots-Tag"

    def test_nosnippet_beats_max_snippet(self):
        permission = indexing_permission(
            x_robots_tag="max-snippet: 50",
            html='<meta name="robots" content="nosnippet">',
        )
        assert permission.max_snippet == 0
        assert permission.max_snippet_source == '<meta name="robots">'

    def test_invalid_max_snippet_preserved_as_unknown(self):
        permission = indexing_permission(x_robots_tag="max-snippet: lots")
        assert permission.max_snippet is None
        assert permission.unknown_directives == ("max-snippet: lots",)


class TestUnavailableAfter:
    def test_deadline_in_future(self):
        permission = indexing_permission(
            x_robots_tag="unavailable_after: 1 Jan 2030 00:00:00 GMT", now=NOW
        )
        assert permission.unavailable_after == FUTURE
        assert permission.unavailable_after_source == "X-Robots-Tag"
        assert permission.unavailable is False
        assert permission.can_index is True

    def test_deadline_passed(self):
        permission = indexing_permission(
            x_robots_tag="unavailable_after: 1 Jan 2020 00:00:00 GMT", now=NOW
        )
        assert permission.unavailable is True
        assert permission.can_index is False
        assert permission.index_source == "X-Robots-Tag"

    def test_deadline_without_now(self):
        permission = indexing_permission(
            x_robots_tag="unavailable_after: 1 Jan 2020 00:00:00 GMT"
        )
        assert permission.unavailable_after == PAST
        assert permission.unavailable is None
        assert permission.can_index is True

    def test_rfc850_date_with_comma(self):
        permission = indexing_permission(
            x_robots_tag="unavailable_after: Monday, 01-Jan-30 00:00:00 GMT, nofollow",
            now=NOW,
        )
        assert permission.unavailable_after == FUTURE
        assert permission.unavailable is False
        assert permission.can_follow is False

    def test_earliest_deadline_wins(self):
        permission = indexing_permission(
            x_robots_tag="unavailable_after: 1 Jan 2030 00:00:00 GMT",
            html='<meta name="robots" content="unavailable_after: 1 Jan 2020 00:00:00 GMT">',
        )
        assert permission.unavailable_after == PAST
        assert permission.unavailable_after_source == '<meta name="robots">'

    def test_invalid_date_preserved_as_unknown(self):
        permission = indexing_permission(x_robots_tag="unavailable_after: someday")
        assert permission.unavailable_after is None
        assert permission.unknown_directives == ("unavailable_after: someday",)

    def test_naive_now_read_as_utc(self):
        permission = indexing_permission(
            x_robots_tag="unavailable_after: 1 Jan 2020 00:00:00 GMT",
            now=datetime(2026, 11, 10),
        )
        assert permission.unavailable is True


class TestConflicts:
    def test_noindex_beats_index(self):
        permission = indexing_permission(
            x_robots_tag="index", html='<meta name="robots" content="noindex">'
        )
        assert permission.can_index is False

    def test_all_does_not_lift_noindex(self):
        permission = indexing_permission(x_robots_tag="all, noindex")
        assert permission.can_index is False

    def test_repeated_directives(self):
        permission = indexing_permission(
            x_robots_tag=["noindex", "noindex", "noindex"]
        )
        assert permission.can_index is False

    def test_header_and_meta_merge(self):
        permission = indexing_permission(
            x_robots_tag="noindex",
            html='<meta name="robots" content="nofollow">',
        )
        assert permission.can_index is False
        assert permission.index_source == "X-Robots-Tag"
        assert permission.can_follow is False
        assert permission.follow_source == '<meta name="robots">'


class TestUnknownDirectives:
    def test_unknown_preserved(self):
        permission = indexing_permission(x_robots_tag="noarchive, noimageindex")
        assert permission.can_index is True
        assert permission.unknown_directives == ("noarchive", "noimageindex")

    def test_unknown_with_value_preserved(self):
        permission = indexing_permission(x_robots_tag="max-image-preview: large")
        assert permission.unknown_directives == ("max-image-preview: large",)

    def test_unknown_from_meta(self):
        permission = indexing_permission(
            html='<meta name="robots" content="noindex, noodp">'
        )
        assert permission.unknown_directives == ("noodp",)

    def test_unknown_deduplicated(self):
        permission = indexing_permission(
            x_robots_tag="noarchive", html='<meta name="robots" content="noarchive">'
        )
        assert permission.unknown_directives == ("noarchive",)

    def test_unknown_of_ignored_generic_rules_not_reported(self):
        permission = indexing_permission(
            x_robots_tag=["noarchive", "googlebot: noindex"],
            user_agent="Googlebot",
        )
        # Only the specific rules apply, so the generic unknown is dropped.
        assert permission.unknown_directives == ()


class TestMerging:
    def test_no_rules(self):
        assert merge_robots_rules([]) == IndexingPermission()

    def test_specific_rules_replace_generic(self):
        rules = parse_x_robots_tag(
            ["noindex", "googlebot: max-snippet: 10"], "Googlebot"
        )
        permission = merge_robots_rules(rules)
        assert permission.can_index is True
        assert permission.max_snippet == 10

    def test_meta_and_header_specific_rules_combine(self):
        rules = parse_x_robots_tag("googlebot: noindex", "Googlebot")
        rules += parse_meta_robots(
            '<meta name="googlebot" content="nofollow">', "Googlebot"
        )
        permission = merge_robots_rules(rules)
        assert permission.can_index is False
        assert permission.can_follow is False


class TestCrawlVsDisplay:
    """The scenario the library is built for: a page that may be fetched but
    not shown."""

    def test_fetchable_but_not_indexable(self):
        permission = indexing_permission(
            x_robots_tag="noindex",
            html='<meta name="robots" content="nosnippet, max-snippet: 0">',
            user_agent="MyBot/1.0",
            now=NOW,
        )
        assert permission.can_index is False
        assert permission.can_follow is True
        assert permission.max_snippet == 0
        # No single boolean could say this: fetching was allowed, display is not.
        assert permission.index_source == "X-Robots-Tag"
        assert permission.follow_source is None
