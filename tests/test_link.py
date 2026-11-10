from w3lib.link import (
    Link,
    LinkSelectionStatus,
    parse_link_header,
    select_pagination_link,
)

FEED_URL = "https://shop.example/feed"
COMMENTS_ID = "https://shop.example/feed#comments"

# One response, two paginated collections: the feed itself and the comments
# embedded in it.  Both declare their own ``next`` link.
TWO_COLLECTION_HEADERS = [
    '</feed?page=2>; rel="next"',
    '</comments?cursor=c2>; rel="next"; anchor="#comments"',
]


class TestParseLinkHeader:
    def test_single_link(self):
        links = parse_link_header('</page/2>; rel="next"')
        assert len(links) == 1
        assert links[0].target == "/page/2"
        assert links[0].relations == frozenset({"next"})
        assert links[0].anchor is None
        assert links[0].raw == '</page/2>; rel="next"'

    def test_multiple_links_in_one_value(self):
        links = parse_link_header('</a>; rel="next", </b>; rel="prev"')
        assert [link.target for link in links] == ["/a", "/b"]

    def test_comma_inside_target_and_quoted_param(self):
        links = parse_link_header('</p?a=1,2>; rel="next"; title="x, y; z"')
        assert len(links) == 1
        assert links[0].target == "/p?a=1,2"
        assert links[0].params["title"] == "x, y; z"

    def test_multiple_relations_in_one_rel_param(self):
        links = parse_link_header('</page/2>; rel="next chapter"')
        assert links[0].relations == frozenset({"next", "chapter"})

    def test_relations_are_lower_cased(self):
        links = parse_link_header('</page/2>; REL="Next"')
        assert links[0].relations == frozenset({"next"})

    def test_quoted_string_escape(self):
        links = parse_link_header(r'</p>; title="a\"b\\c"')
        assert links[0].params["title"] == 'a"b\\c'

    def test_valueless_and_repeated_params(self):
        links = parse_link_header('</p>; rel; anchor="#a"; anchor="#b"')
        assert links[0].params["rel"] is None
        # First occurrence wins, per RFC 8288.
        assert links[0].anchor == "#a"

    def test_anchor_param(self):
        links = parse_link_header('</c?cursor=2>; rel="next"; anchor="#comments"')
        assert links[0].anchor == "#comments"

    def test_malformed_values_are_skipped(self):
        assert parse_link_header("not a link") == []
        assert parse_link_header('</p>; rel="next", oops') != []
        assert parse_link_header("") == []

    def test_bytes_input(self):
        links = parse_link_header(b'</page/2>; rel="next"')
        assert links[0].target == "/page/2"


class TestSelectPaginationLink:
    def test_unique_absolute_target(self):
        selection = select_pagination_link(
            "https://example.com/list",
            '<https://example.com/list?page=2>; rel="next"',
        )
        assert selection.status is LinkSelectionStatus.UNIQUE
        assert selection.ok
        assert selection.url == "https://example.com/list?page=2"

    def test_relative_target_is_resolved(self):
        selection = select_pagination_link(
            "https://example.com/list?page=1",
            '<../list?page=2>; rel="next"',
        )
        assert selection.status is LinkSelectionStatus.UNIQUE
        assert selection.url == "https://example.com/list?page=2"

    def test_multiple_rel_values_match(self):
        selection = select_pagination_link(
            "https://example.com/doc",
            '</doc/2>; rel="next chapter"',
            rel="next",
        )
        assert selection.status is LinkSelectionStatus.UNIQUE
        assert selection.url == "https://example.com/doc/2"

    def test_rel_matching_is_case_insensitive(self):
        selection = select_pagination_link(
            "https://example.com/list",
            '</list?page=2>; REL="NEXT"',
            rel="Next",
        )
        assert selection.status is LinkSelectionStatus.UNIQUE

    def test_not_found_is_distinct_from_ambiguous(self):
        selection = select_pagination_link(
            "https://example.com/list",
            '</list?page=0>; rel="prev"',
        )
        assert selection.status is LinkSelectionStatus.NOT_FOUND
        assert not selection.ok
        assert selection.url is None
        assert selection.matches == []

    def test_conflicting_targets_are_ambiguous(self):
        selection = select_pagination_link(
            "https://example.com/list",
            [
                '</list?page=2>; rel="next"',
                '</list?page=3>; rel="next"',
            ],
        )
        assert selection.status is LinkSelectionStatus.AMBIGUOUS
        assert not selection.ok
        assert selection.url is None
        # The conflicting declarations stay traceable.
        assert [link.raw for link in selection.matches] == [
            '</list?page=2>; rel="next"',
            '</list?page=3>; rel="next"',
        ]
        assert [link.target for link in selection.matches] == [
            "/list?page=2",
            "/list?page=3",
        ]

    def test_duplicate_declarations_of_same_target_stay_unique(self):
        selection = select_pagination_link(
            "https://example.com/list",
            [
                '</list?page=2>; rel="next"',
                '<https://example.com/list?page=2>; rel="next"',
            ],
        )
        assert selection.status is LinkSelectionStatus.UNIQUE
        assert selection.url == "https://example.com/list?page=2"
        # Both original declarations remain inspectable.
        assert len(selection.matches) == 2

    def test_last_next_is_not_silently_picked(self):
        selection = select_pagination_link(
            "https://example.com/list",
            '</list?page=2>; rel="next", </list?page=3>; rel="next"',
        )
        assert selection.status is LinkSelectionStatus.AMBIGUOUS
        assert selection.url is None

    def test_multiple_header_fields_are_combined(self):
        selection = select_pagination_link(
            "https://example.com/list",
            ['</list?page=0>; rel="prev"', '</list?page=2>; rel="next"'],
        )
        assert selection.status is LinkSelectionStatus.UNIQUE
        assert selection.url == "https://example.com/list?page=2"


class TestTwoCollectionsOneResponse:
    """Two collections paginate through Link headers of the same response;
    selecting one must never follow the other collection's chain."""

    def test_feed_next(self):
        selection = select_pagination_link(FEED_URL, TWO_COLLECTION_HEADERS)
        assert selection.status is LinkSelectionStatus.UNIQUE
        assert selection.url == "https://shop.example/feed?page=2"
        assert [link.target for link in selection.matches] == ["/feed?page=2"]

    def test_comments_next_via_absolute_anchor(self):
        selection = select_pagination_link(
            FEED_URL, TWO_COLLECTION_HEADERS, collection=COMMENTS_ID
        )
        assert selection.status is LinkSelectionStatus.UNIQUE
        assert selection.url == "https://shop.example/comments?cursor=c2"

    def test_comments_next_via_relative_collection_id(self):
        selection = select_pagination_link(
            FEED_URL, TWO_COLLECTION_HEADERS, collection="#comments"
        )
        assert selection.status is LinkSelectionStatus.UNIQUE
        assert selection.url == "https://shop.example/comments?cursor=c2"
        assert selection.collection == COMMENTS_ID

    def test_relative_anchor_is_resolved_against_response_url(self):
        selection = select_pagination_link(
            FEED_URL,
            '</feed?page=2>; rel="next"; anchor="/feed"',
            collection=FEED_URL,
        )
        assert selection.status is LinkSelectionStatus.UNIQUE
        assert selection.url == "https://shop.example/feed?page=2"

    def test_other_collection_links_do_not_leak(self):
        # The comments collection has no next page: asking for it must be
        # NOT_FOUND, not the feed's next page.
        headers = [
            '</feed?page=2>; rel="next"',
            '</comments?cursor=c2>; rel="prev"; anchor="#comments"',
        ]
        selection = select_pagination_link(FEED_URL, headers, collection=COMMENTS_ID)
        assert selection.status is LinkSelectionStatus.NOT_FOUND
        assert selection.url is None

    def test_ambiguity_is_scoped_to_one_collection(self):
        # The feed's duplicate next links must not make the comments
        # selection ambiguous, and vice versa.
        headers = [
            '</feed?page=2>; rel="next"',
            '</feed?page=3>; rel="next"',
            '</comments?cursor=c2>; rel="next"; anchor="#comments"',
        ]
        feed = select_pagination_link(FEED_URL, headers)
        comments = select_pagination_link(FEED_URL, headers, collection="#comments")
        assert feed.status is LinkSelectionStatus.AMBIGUOUS
        assert comments.status is LinkSelectionStatus.UNIQUE
        assert comments.url == "https://shop.example/comments?cursor=c2"

    def test_selection_reports_what_was_asked(self):
        selection = select_pagination_link(
            FEED_URL, TWO_COLLECTION_HEADERS, collection="#comments", rel="next"
        )
        assert selection.response_url == FEED_URL
        assert selection.collection == COMMENTS_ID
        assert selection.rel == "next"
        assert isinstance(selection.matches[0], Link)
