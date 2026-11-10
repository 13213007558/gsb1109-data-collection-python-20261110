import unittest

from readability import Document


def paragraph(prefix, index):
    return (
        "<p>{} paragraph {} carries distinct reporting text with enough "
        "length for reliable alignment and punctuation.</p>".format(
            prefix, index
        )
    )


def paragraphs(prefix, count):
    return "".join(paragraph(prefix, index) for index in range(count))


def full_dual_page():
    heading = "<h2>Shared section heading</h2>"
    quote = (
        "<blockquote>A shared quotation that is long enough to take part "
        "in the block alignment checks.</blockquote>"
    )
    table = (
        "<table><tr><td>Dual table row with substantial cell text for the "
        "alignment checks.</td></tr></table>"
    )
    body = heading + paragraphs("shared", 6) + quote + table
    return """
    <html><body><main>
        <div class="desktop-article">{}</div>
        <div class="mobile-article">{}</div>
    </main></body></html>
    """.format(body, body)


def partial_dual_page(mobile_first=False):
    desktop = """
    <div class="desktop-version">
        {shared}{desktop_only}
    </div>
    """.format(
        shared=paragraphs("shared", 4),
        desktop_only=paragraphs("desktop-only", 2),
    )
    mobile = """
    <div class="mobile-version">
        {shared}{mobile_only}
    </div>
    """.format(
        shared=paragraphs("shared", 4),
        mobile_only=paragraphs("mobile-only", 4),
    )
    if mobile_first:
        variants = mobile + desktop
    else:
        variants = desktop + mobile
    return "<html><body><article>{}</article></body></html>".format(variants)


def legitimate_repetition_page():
    row = (
        "<tr><td>Repeated table row cell with substantial text for the "
        "table body.</td></tr>"
    )
    prose = "Ordinary article prose with useful details and punctuation. " * 8
    return """
    <html><body><article><div class="article-body">
        <h2>Repeated Chapter Title</h2>
        <p>{prose}</p>
        <blockquote>A quote repeated intentionally inside the article.</blockquote>
        <h2>Repeated Chapter Title</h2>
        <blockquote>A quote repeated intentionally inside the article.</blockquote>
        <table>{rows}</table>
        <p>{more_prose}</p>
    </div></article></body></html>
    """.format(prose=prose, rows=row * 4, more_prose=prose)


def block(prefix, index):
    return (
        "<p>{} block {} carries substantial reporting text with distinct "
        "wording and punctuation.</p>".format(prefix, index)
    )


def unlabeled_pair(shared_count, left_unique, right_unique):
    def variant(unique_prefix, unique_count):
        shared = "".join(block("shared", i) for i in range(shared_count))
        unique = "".join(
            block(unique_prefix, i) for i in range(unique_count)
        )
        return '<div class="story"><div>{}{}</div></div>'.format(
            shared, unique
        )

    return "<html><body><main>{}{}</main></body></html>".format(
        variant("left-only", left_unique),
        variant("right-only", right_unique),
    )


class TestParallelVariants(unittest.TestCase):
    def test_report_is_empty_before_summary(self):
        doc = Document(full_dual_page())

        self.assertEqual([], doc.parallel_variant_report())

    def test_merges_full_desktop_and_mobile_variants(self):
        doc = Document(full_dual_page())
        summary = doc.summary(html_partial=True)

        report = doc.parallel_variant_report()
        self.assertEqual(1, len(report))
        entry = report[0]
        self.assertEqual("merged", entry["status"])
        self.assertEqual("desktop", entry["primary"]["label"])
        self.assertEqual("mobile", entry["secondary"]["label"])
        self.assertGreaterEqual(entry["matched_blocks"], 8)
        self.assertEqual([], entry["unique_primary"])
        self.assertEqual([], entry["unique_secondary"])

        self.assertEqual(1, summary.count("Shared section heading"))
        self.assertEqual(1, summary.count("shared paragraph 0"))
        self.assertEqual(1, summary.count("shared paragraph 5"))
        self.assertEqual(1, summary.count("A shared quotation"))
        self.assertEqual(1, summary.count("Dual table row"))

        heading_pos = summary.find("Shared section heading")
        first_pos = summary.find("shared paragraph 0")
        last_pos = summary.find("shared paragraph 5")
        quote_pos = summary.find("A shared quotation")
        table_pos = summary.find("Dual table row")
        self.assertLess(heading_pos, first_pos)
        self.assertLess(first_pos, last_pos)
        self.assertLess(last_pos, quote_pos)
        self.assertLess(quote_pos, table_pos)

    def test_keeps_unique_paragraphs_with_sources_on_partial_overlap(self):
        doc = Document(partial_dual_page())
        summary = doc.summary(html_partial=True)

        entry = doc.parallel_variant_report()[0]
        self.assertEqual("merged", entry["status"])
        self.assertEqual(4, entry["matched_blocks"])
        self.assertTrue(
            any("desktop-only" in preview for preview in entry["unique_primary"])
        )
        self.assertTrue(
            any("mobile-only" in preview for preview in entry["unique_secondary"])
        )

        self.assertEqual(1, summary.count("shared paragraph 0"))
        self.assertIn("desktop-only paragraph 0", summary)
        self.assertIn("mobile-only paragraph 0", summary)
        self.assertIn(
            'data-readability-variant-source="mobile"', summary
        )

        last_shared = summary.find("shared paragraph 3")
        first_mobile = summary.find("mobile-only paragraph 0")
        first_desktop = summary.find("desktop-only paragraph 0")
        self.assertLess(last_shared, first_mobile)
        self.assertLess(first_mobile, first_desktop)

    def test_desktop_is_base_even_when_mobile_is_longer_or_first(self):
        doc = Document(partial_dual_page(mobile_first=True))
        summary = doc.summary(html_partial=True)

        entry = doc.parallel_variant_report()[0]
        self.assertEqual("desktop", entry["primary"]["label"])
        self.assertEqual("mobile", entry["secondary"]["label"])
        self.assertEqual(1, summary.count("shared paragraph 0"))
        self.assertIn("desktop-only paragraph 0", summary)
        self.assertIn("mobile-only paragraph 0", summary)

    def test_fuzzy_matching_does_not_pair_distinct_paragraphs(self):
        doc = Document(partial_dual_page())
        doc.summary(html_partial=True)

        entry = doc.parallel_variant_report()[0]
        matched_preview = " ".join(
            entry["unique_primary"] + entry["unique_secondary"]
        )
        self.assertIn("desktop-only", matched_preview)
        self.assertIn("mobile-only", matched_preview)
        self.assertEqual(4, entry["matched_blocks"])

    def test_preserves_legitimate_repeated_quotes_headings_and_rows(self):
        doc = Document(legitimate_repetition_page())
        summary = doc.summary(html_partial=True)

        self.assertEqual([], doc.parallel_variant_report())
        self.assertEqual(2, summary.count("Repeated Chapter Title"))
        self.assertEqual(2, summary.count("A quote repeated intentionally"))
        self.assertEqual(4, summary.count("Repeated table row cell"))

        first_heading = summary.find("Repeated Chapter Title")
        second_heading = summary.find(
            "Repeated Chapter Title", first_heading + 1
        )
        first_quote = summary.find("A quote repeated intentionally")
        second_quote = summary.find(
            "A quote repeated intentionally", first_quote + 1
        )
        self.assertLess(first_heading, first_quote)
        self.assertLess(first_quote, second_heading)
        self.assertLess(second_heading, second_quote)
        self.assertLess(second_quote, summary.find("Repeated table row cell"))

    def test_reports_candidate_without_touching_unconfirmed_pair(self):
        doc = Document(unlabeled_pair(6, 4, 4))
        summary = doc.summary(html_partial=True)

        report = doc.parallel_variant_report()
        self.assertEqual(1, len(report))
        entry = report[0]
        self.assertEqual("candidate", entry["status"])
        self.assertIn("partial overlap", entry["reason"])
        self.assertGreaterEqual(entry["matched_blocks"], 3)

        self.assertEqual(2, summary.count("shared block 0"))
        self.assertIn("left-only block 0", summary)
        self.assertIn("right-only block 0", summary)

    def test_merges_unlabeled_near_complete_variants(self):
        page = unlabeled_pair(9, 1, 1)
        doc = Document(page)
        summary = doc.summary(html_partial=True)

        entry = doc.parallel_variant_report()[0]
        self.assertEqual("merged", entry["status"])
        self.assertEqual("near-complete block alignment", entry["reason"])
        self.assertEqual(1, summary.count("shared block 0"))
        self.assertIn("left-only block 0", summary)
        self.assertIn("right-only block 0", summary)

    def test_ordinary_article_has_no_parallel_report(self):
        page = """
        <html><body><article>
            <p>{}</p>
        </article></body></html>
        """.format("Ordinary article text with punctuation. " * 20)

        doc = Document(page)
        doc.summary(html_partial=True)

        self.assertEqual([], doc.parallel_variant_report())


if __name__ == "__main__":
    unittest.main()
