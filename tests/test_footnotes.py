import unittest

from readability import Document


ARTICLE_TEXT = "This is the article body with useful explanatory text. " * 12


def make_sample(paragraphs="", extra=""):
    return (
        "<html><body>"
        '<article><div class="article-content">'
        "<p>{}</p>".format(ARTICLE_TEXT)
        + paragraphs
        + "</div></article>"
        + extra
        + "</body></html>"
    )


class TestKeepFootnotes(unittest.TestCase):
    def test_collects_referenced_footnote_and_drops_footer_noise(self):
        sample = make_sample(
            '<p>Claim one<sup id="fnref1"><a href="#fn1">[1]</a></sup> '
            "is explained with more article text here.</p>",
            '<div class="footer"><ol class="references">'
            '<li id="fn1">First note text. '
            '<a href="#fnref1">back to text</a></li>'
            '<li id="fn2">Unused noise note.</li>'
            "</ol><p>Copyright noise</p></div>",
        )

        summary = Document(sample).summary(keep_footnotes=True)

        self.assertIn("First note text.", summary)
        self.assertIn('href="#fn1"', summary)
        self.assertIn('href="#fnref1"', summary)
        self.assertIn("readability-footnotes", summary)
        self.assertNotIn("Unused noise note.", summary)
        self.assertNotIn("Copyright noise", summary)
        self.assertNotIn("readability-missing-ref", summary)

    def test_repeated_reference_does_not_duplicate_note(self):
        sample = make_sample(
            "<p>First<sup><a href=\"#fn1\">[1]</a></sup> and "
            "again<sup><a href=\"#fn1\">[1]</a></sup> with more text here.</p>",
            '<ol><li id="fn1">Shared note text.</li></ol>',
        )

        summary = Document(sample).summary(keep_footnotes=True)

        self.assertEqual(1, summary.count("Shared note text."))
        self.assertEqual(1, summary.count('id="fn1"'))

    def test_missing_target_is_marked(self):
        sample = make_sample(
            '<p>Text<sup><a href="#fn404">[9]</a></sup> more text here.</p>'
        )

        summary = Document(sample).summary(keep_footnotes=True)

        self.assertIn("readability-missing-ref", summary)
        self.assertIn('data-readability-missing-ref="fn404"', summary)

    def test_external_fragment_links_are_not_footnotes(self):
        sample = make_sample(
            '<p>See <a href="http://other.example/page#section">external '
            "reference</a> and "
            '<a href="http://example.com/article#fn1">local reference</a> '
            "for more details on this topic.</p>",
            '<ol><li id="fn1">Collected note text.</li></ol>',
        )

        doc = Document(sample, url="http://example.com/article")
        summary = doc.summary(keep_footnotes=True)

        self.assertIn("Collected note text.", summary)
        self.assertIn("http://other.example/page#section", summary)
        self.assertNotIn("readability-missing-ref", summary)

    def test_mutually_referencing_notes_terminate(self):
        sample = make_sample(
            '<p>Claim<sup><a href="#fn1">[1]</a></sup> with more text '
            "and additional detail here.</p>",
            '<ol><li id="fn1">Note one, see <a href="#fn2">note two</a>.</li>'
            '<li id="fn2">Note two, see <a href="#fn1">note one</a>.</li>'
            "</ol>",
        )

        summary = Document(sample).summary(keep_footnotes=True)

        self.assertIn("Note one", summary)
        self.assertIn("Note two", summary)
        self.assertEqual(1, summary.count("Note one, see"))
        self.assertEqual(1, summary.count("Note two, see"))

    def test_duplicate_anchor_ids_resolve_to_first(self):
        sample = make_sample(
            '<p>Claim<sup><a href="#fn1">[1]</a></sup> with more text '
            "and additional detail here.</p>",
            '<p id="fn1">First duplicate note.</p>'
            '<div><p id="fn1">Second duplicate note.</p></div>',
        )

        summary = Document(sample).summary(keep_footnotes=True)

        self.assertIn("First duplicate note.", summary)
        self.assertNotIn("Second duplicate note.", summary)

    def test_internal_section_links_stay_reachable(self):
        sample = make_sample(
            '<p>See <a href="#details">the details section</a> for more '
            "context in this article.</p>"
            '<h2 id="details">Details</h2>'
            "<p>More details text that belongs to the article itself.</p>",
        )

        summary = Document(sample).summary(keep_footnotes=True)

        self.assertIn('href="#details"', summary)
        self.assertNotIn("readability-missing-ref", summary)
        self.assertNotIn("readability-footnotes", summary)

    def test_footnotes_not_collected_by_default(self):
        sample = make_sample(
            '<p>Claim<sup><a href="#fn1">[1]</a></sup> with more text '
            "and additional detail here.</p>",
            '<ol><li id="fn1">Note text.</li></ol>',
        )

        summary = Document(sample).summary()

        self.assertNotIn("Note text.", summary)
        self.assertNotIn("readability-missing-ref", summary)

    def test_html_partial_keeps_footnotes_inside_div(self):
        sample = make_sample(
            '<p>Claim<sup><a href="#fn1">[1]</a></sup> with more text '
            "and additional detail here.</p>",
            '<ol><li id="fn1">Partial note text.</li></ol>',
        )

        summary = Document(sample).summary(html_partial=True, keep_footnotes=True)

        self.assertTrue(summary.startswith("<div>"))
        self.assertIn("Partial note text.", summary)
        self.assertNotIn("<html>", summary)


if __name__ == "__main__":
    unittest.main()
