import inspect
import unittest

from readability.pagination import (
    CONFLICTING_TITLE,
    MISSING_PAGE,
    UNCERTAIN_JOIN,
    CONTINUATION,
    OVERLAP,
    UNCERTAIN,
    DownloadedPage,
    stitch_article,
)
import readability.pagination as pagination


P1 = (
    "The committee released its findings after months of closed door "
    "interviews with every witness involved in the inquiry."
)
P2 = (
    "Officials promised a fuller response once the remaining agencies had "
    "finished their internal review of the disputed evidence."
)
P3 = (
    "By the end of the week both chambers had scheduled public hearings to "
    "revisit the provisions that lobbyists had fought for years."
)
P4 = (
    "Local governments warned that the new timetable would strain budgets "
    "already weakened by the previous winter's emergency spending measures."
)
P5 = (
    "The regulator opened a separate consultation on disclosure rules that "
    "would require every listed firm to publish climate risk assessments."
)
P6 = (
    "Industry groups responded that the proposed thresholds were unrealistic "
    "for smaller suppliers without dedicated compliance teams on staff."
)
QUOTE = (
    "We have nothing to fear but fear itself, and the long paperwork that "
    "inevitably follows every grand announcement made in the chamber."
)


def make_page(title, body_html):
    return (
        "<html><head><title>%s</title></head>"
        "<body><article>%s</article></body></html>" % (title, body_html)
    )


def para(text):
    return "<p>%s</p>" % text


def link_para(text, href):
    return '<p><a href="%s">%s</a></p>' % (href, text)


class StitchArticleTest(unittest.TestCase):
    def test_removes_whole_paragraph_overlap_across_three_pages(self):
        pages = [
            DownloadedPage(1, make_page(
                "The Long Read", para(P1) + "<blockquote>%s</blockquote>" % QUOTE + para(P2)
            )),
            DownloadedPage(2, make_page(
                "The Long Read - Page 2", para(P2) + para(P3)
            )),
            DownloadedPage(3, make_page(
                "The Long Read - Page 3", para(P3) + para(P4)
            )),
        ]

        result = stitch_article(pages)

        self.assertEqual(
            [text for _, text in result.reading_order()],
            [P1, QUOTE, P2, P3, P4],
        )
        self.assertEqual(
            [source for source, _ in result.reading_order()],
            [1, 1, 1, 2, 3],
        )
        self.assertEqual(
            [boundary["relation"] for boundary in result.boundaries],
            [OVERLAP, OVERLAP],
        )
        self.assertEqual(result.issues, [])
        self.assertEqual(result.plain_text().count(QUOTE), 1)
        self.assertIn('data-source-page="2"', result.merged_html)

    def test_removes_multi_paragraph_overlap_in_one_skip(self):
        pages = [
            DownloadedPage(1, make_page(
                "Multi Overlap", para(P1) + para(P5) + para(P6)
            )),
            DownloadedPage(2, make_page(
                "Multi Overlap - Page 2", para(P5) + para(P6)
            )),
        ]

        result = stitch_article(pages)
        boundary = result.boundaries[0]

        self.assertEqual(boundary["relation"], OVERLAP)
        self.assertEqual(boundary["detail"]["matched_paragraphs"], 2)
        self.assertEqual(boundary["skip_right"], 2)
        self.assertEqual(
            [text for _, text in result.reading_order()], [P1, P5, P6]
        )
        self.assertEqual(
            [source for source, _ in result.reading_order()], [1, 1, 1]
        )

    def test_drops_repeated_fragment_when_left_paragraph_is_complete(self):
        fragment = P5[:52]
        pages = [
            DownloadedPage(1, make_page("Split Story", para(P1) + para(P5))),
            DownloadedPage(2, make_page(
                "Split Story - Page 2", para(fragment) + para(P6)
            )),
        ]

        result = stitch_article(pages)
        boundary = result.boundaries[0]

        self.assertEqual(boundary["relation"], CONTINUATION)
        self.assertEqual(boundary["detail"]["complete_side"], "left")
        self.assertEqual(
            [text for _, text in result.reading_order()], [P1, P5, P6]
        )
        self.assertEqual(result.plain_text().count(fragment), 1)

    def test_accepts_overlap_with_minor_editorial_drift(self):
        drifted = P2[:-6] + " today."
        self.assertNotEqual(drifted, P2)
        pages = [
            DownloadedPage(1, make_page("Drift Story", para(P1) + para(P2))),
            DownloadedPage(2, make_page(
                "Drift Story - Page 2", para(drifted) + para(P3)
            )),
        ]

        result = stitch_article(pages)
        boundary = result.boundaries[0]

        self.assertEqual(boundary["relation"], OVERLAP)
        self.assertEqual(
            [text for _, text in result.reading_order()], [P1, P2, P3]
        )

    def test_merges_paragraph_split_across_page_boundary(self):
        fragment = P5[:52]
        self.assertTrue(P5.startswith(fragment))
        pages = [
            DownloadedPage(1, make_page("Split Story", para(P1) + para(fragment))),
            DownloadedPage(2, make_page(
                "Split Story - Page 2", para(P5) + para(P6)
            )),
        ]

        result = stitch_article(pages)
        boundary = result.boundaries[0]

        self.assertEqual(boundary["relation"], CONTINUATION)
        self.assertEqual(boundary["detail"]["complete_side"], "right")
        self.assertEqual(
            [text for _, text in result.reading_order()], [P1, P5, P6]
        )
        merged = result.paragraphs[1]
        self.assertEqual(merged.text, P5)
        self.assertEqual(merged.source_page, (1, 2))
        self.assertIn('data-source-page="1,2"', result.merged_html)
        self.assertEqual(result.plain_text().count(fragment), 1)
        self.assertEqual(result.issues, [])

    def test_keeps_identical_quotation_that_appears_in_body_twice(self):
        pages = [
            DownloadedPage(1, make_page(
                "Repeated Quote", para(P1) + "<blockquote>%s</blockquote>" % QUOTE + para(P2)
            )),
            DownloadedPage(2, make_page(
                "Repeated Quote - Page 2",
                para(P2) + para(P3) + "<blockquote>%s</blockquote>" % QUOTE + para(P4),
            )),
        ]

        result = stitch_article(pages)

        self.assertEqual(result.boundaries[0]["relation"], OVERLAP)
        self.assertEqual(result.plain_text().count(QUOTE), 2)
        quote_sources = [
            source
            for source, text in result.reading_order()
            if text == QUOTE
        ]
        self.assertEqual(quote_sources, [1, 2])
        self.assertEqual(result.issues, [])

    def test_same_text_with_different_links_is_kept_and_flagged(self):
        wording = "Read the official registry entry for the full background."
        pages = [
            DownloadedPage(1, make_page(
                "Link Story", para(P1) + link_para(wording, "/v1")
            )),
            DownloadedPage(2, make_page(
                "Link Story - Page 2", link_para(wording, "/v2") + para(P2)
            )),
        ]

        result = stitch_article(pages)
        boundary = result.boundaries[0]

        self.assertEqual(boundary["relation"], UNCERTAIN)
        self.assertIn("different link targets", boundary["reason"])
        self.assertEqual(result.plain_text().count(wording), 2)
        link_sources = [
            source
            for source, text in result.reading_order()
            if text.startswith("Read the official")
        ]
        self.assertEqual(link_sources, [1, 2])
        self.assertEqual(
            [issue.kind for issue in result.issues], [UNCERTAIN_JOIN]
        )

    def test_missing_page_is_marked_and_boundary_left_uncertain(self):
        pages = [
            DownloadedPage(1, make_page("Gap Story", para(P1) + para(P2))),
            DownloadedPage(3, make_page("Gap Story - Page 3", para(P3) + para(P4))),
        ]

        result = stitch_article(pages)
        boundary = result.boundaries[0]

        self.assertEqual(boundary["between"], (1, 3))
        self.assertEqual(boundary["relation"], UNCERTAIN)
        self.assertIn("gap in the page sequence", boundary["reason"])
        issue_kinds = [issue.kind for issue in result.issues]
        self.assertIn(MISSING_PAGE, issue_kinds)
        self.assertIn(UNCERTAIN_JOIN, issue_kinds)
        missing = next(issue for issue in result.issues if issue.kind == MISSING_PAGE)
        self.assertEqual(missing.between, (1, 3))
        self.assertIn("2", missing.detail)
        # No evidence is acted upon across a gap: every paragraph survives.
        self.assertEqual(
            [text for _, text in result.reading_order()],
            [P1, P2, P3, P4],
        )

    def test_conflicting_titles_are_reported_but_stitching_continues(self):
        pages = [
            DownloadedPage(1, make_page("Story A", para(P1) + para(P2))),
            DownloadedPage(2, make_page("Story B - Page 2", para(P2) + para(P3))),
        ]

        result = stitch_article(pages)

        conflict = next(
            issue for issue in result.issues if issue.kind == CONFLICTING_TITLE
        )
        self.assertEqual(conflict.between, (1, 2))
        self.assertEqual(result.title, "Story A")
        self.assertEqual(result.titles[2], "Story B - Page 2")
        self.assertEqual(result.boundaries[0]["relation"], OVERLAP)
        self.assertEqual(
            [text for _, text in result.reading_order()], [P1, P2, P3]
        )

    def test_uncertain_join_reports_position_without_dropping_text(self):
        pages = [
            DownloadedPage(1, make_page("Separate Story", para(P1) + para(P2))),
            DownloadedPage(2, make_page(
                "Separate Story - Page 2", para(P3) + para(P4)
            )),
        ]

        result = stitch_article(pages)
        boundary = result.boundaries[0]

        self.assertEqual(boundary["relation"], UNCERTAIN)
        self.assertEqual(boundary["position"], 2)
        self.assertEqual(
            result.unresolved_positions(),
            [
                {
                    "position": 2,
                    "between": (1, 2),
                    "reason": boundary["reason"],
                }
            ],
        )
        self.assertEqual(
            [text for _, text in result.reading_order()],
            [P1, P2, P3, P4],
        )

    def test_repeated_title_heading_is_treated_as_boilerplate(self):
        pages = [
            DownloadedPage(1, make_page(
                "The Long Read", "<h1>The Long Read</h1>" + para(P1) + para(P2)
            )),
            DownloadedPage(2, make_page(
                "The Long Read - Page 2",
                "<h1>The Long Read</h1>" + para(P2) + para(P3),
            )),
        ]

        result = stitch_article(pages)

        self.assertEqual(result.title, "The Long Read")
        self.assertEqual(
            [text for _, text in result.reading_order()], [P1, P2, P3]
        )
        self.assertNotIn("The Long Read", result.plain_text())

    def test_accepts_tuples_dicts_and_respects_caller_page_order(self):
        html_one = make_page("Ordered Story", para(P1) + para(P2))
        html_two = make_page(
            "Ordered Story - Page 2", para(P2) + para(P3)
        )

        tuple_result = stitch_article(
            [(1, html_one, "https://example.test/1"), (2, html_two)]
        )
        self.assertEqual(tuple_result.page_order, (1, 2))
        self.assertEqual(
            [text for _, text in tuple_result.reading_order()], [P1, P2, P3]
        )

        reversed_result = stitch_article(
            [
                {"page_id": 2, "html": html_two},
                {"page_id": 1, "html": html_one},
            ]
        )
        self.assertEqual(reversed_result.page_order, (2, 1))

    def test_validates_page_arguments(self):
        with self.assertRaises(ValueError):
            stitch_article([])

        html = make_page("Dup Story", para(P1))
        with self.assertRaises(ValueError):
            stitch_article([(1, html), (1, html)])

        with self.assertRaises(TypeError):
            stitch_article([object()])

    def test_uses_injected_factory_and_makes_no_network_calls(self):
        source = inspect.getsource(pagination)
        self.assertNotIn("urllib", source)
        self.assertNotIn("import requests", source)
        self.assertNotIn("http.client", source)

        calls = []

        def factory(html, url=None):
            calls.append((html, url))
            return pagination.Document(html, url=url)

        pages = [
            DownloadedPage(
                1, make_page("Injected", para(P1) + para(P2)),
                url="https://example.test/1",
            ),
            DownloadedPage(
                2, make_page("Injected - Page 2", para(P2) + para(P3)),
                url="https://example.test/2",
            ),
        ]

        result = stitch_article(pages, document_factory=factory)

        self.assertEqual(len(calls), 2)
        self.assertEqual([call[1] for call in calls], [
            "https://example.test/1", "https://example.test/2"
        ])
        self.assertEqual(result.boundaries[0]["relation"], OVERLAP)


if __name__ == "__main__":
    unittest.main()
