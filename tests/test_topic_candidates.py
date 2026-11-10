import unittest

from readability import AmbiguousAnchor, Document
from readability.readability import Unparseable


FIRST_TEXT = "Firststory reporting, with detailed facts, and useful context. "
SECOND_TEXT = "Secondstory analysis, with detailed facts, and useful context. "
CARD_TEXT = "Recommended read, about another trending topic. "


def topic_page(first_text, second_text, card_text=None):
    card = ""
    if card_text is not None:
        card = (
            '<article class="recommended-card">'
            "<h3>Recommended</h3>"
            "<p>{}</p>"
            "</article>"
        ).format(card_text)
    return """
    <html><head><title>Topic page</title></head><body><main>
        <article class="story">
            <h2>First Story</h2>
            <p class="byline">Alice Reporter</p>
            <p>{first}</p>
            {card}
        </article>
        <article class="story">
            <h2>Second Story</h2>
            <p class="byline">Bob Reporter</p>
            <p>{second}</p>
        </article>
    </main></body></html>
    """.format(first=first_text, second=second_text, card=card)


class TestArticleCandidates(unittest.TestCase):
    """Topic pages concatenate several articles; candidates expose them."""

    def test_candidates_keep_title_author_range_and_overlaps(self):
        page = topic_page(FIRST_TEXT * 20, SECOND_TEXT * 8, CARD_TEXT * 3)
        candidates = Document(page).article_candidates()

        anchors = [candidate["anchor"] for candidate in candidates]
        self.assertEqual(len(anchors), len(set(anchors)))

        by_title = {
            candidate["title"]: candidate
            for candidate in candidates
            if candidate["title"]
        }
        first = by_title["First Story"]
        second = by_title["Second Story"]
        card = by_title["Recommended"]
        main = next(c for c in candidates if c["elem"].tag == "main")

        self.assertEqual("Alice Reporter", first["author"])
        self.assertEqual("Bob Reporter", second["author"])

        # The nested recommendation card is not counted as part of the
        # enclosing article's own content range.
        first_own_text = len(FIRST_TEXT * 20)
        self.assertGreaterEqual(first["text_length"], first_own_text)
        self.assertLess(first["text_length"], first_own_text + 60)
        self.assertLess(main["text_length"], 25)

        # Overlap relations are recorded in both directions.
        self.assertIn(card["anchor"], first["overlaps"])
        self.assertIn(main["anchor"], first["overlaps"])
        self.assertIn(first["anchor"], card["overlaps"])
        self.assertNotIn(second["anchor"], first["overlaps"])

    def test_anchor_selection_stable_when_article_lengths_swap(self):
        long_first = topic_page(FIRST_TEXT * 30, SECOND_TEXT * 6)
        long_second = topic_page(FIRST_TEXT * 6, SECOND_TEXT * 30)

        # Without an explicit anchor the top candidate follows the length.
        doc = Document(long_first)
        best = doc.select_best_candidate(doc.score_paragraphs())
        self.assertEqual("First Story", best["elem"].find(".//h2").text)
        doc = Document(long_second)
        best = doc.select_best_candidate(doc.score_paragraphs())
        self.assertEqual("Second Story", best["elem"].find(".//h2").text)

        def second_story_summary(page):
            doc = Document(page)
            candidates = doc.article_candidates()
            second = next(
                c for c in candidates if c["title"] == "Second Story"
            )
            return second["anchor"], doc.summary(anchor=second["anchor"])

        anchor_a, summary_a = second_story_summary(long_first)
        anchor_b, summary_b = second_story_summary(long_second)

        # The same identity keeps the same anchor and the same selection,
        # no matter which of the two articles is longer.
        self.assertEqual(anchor_a, anchor_b)
        for summary in (summary_a, summary_b):
            self.assertIn("Secondstory", summary)
            self.assertNotIn("Firststory", summary)

    def test_summary_selects_article_by_title_anchor(self):
        page = topic_page(FIRST_TEXT * 20, SECOND_TEXT * 6)
        summary = Document(page).summary(anchor="Second Story")

        self.assertIn("Secondstory", summary)
        self.assertNotIn("Firststory", summary)

    def test_anchored_summary_excludes_nested_recommendation_card(self):
        page = topic_page(FIRST_TEXT * 20, SECOND_TEXT * 8, CARD_TEXT * 4)
        doc = Document(page)
        first = next(
            c for c in doc.article_candidates() if c["title"] == "First Story"
        )

        summary = doc.summary(anchor=first["anchor"])

        self.assertIn("Firststory", summary)
        self.assertNotIn("Recommended", summary)
        self.assertNotIn("Secondstory", summary)

    def test_ambiguous_title_anchor_raises_with_candidates(self):
        page = """
        <html><body><main>
            <article><h2>Weekly Roundup</h2><p>{first}</p></article>
            <article><h2>Weekly Roundup</h2><p>{second}</p></article>
        </main></body></html>
        """.format(first=FIRST_TEXT * 10, second=SECOND_TEXT * 10)
        doc = Document(page)

        with self.assertRaises(AmbiguousAnchor) as context:
            doc.summary(anchor="Weekly Roundup")

        titles = [c["title"] for c in context.exception.candidates]
        self.assertEqual(2, titles.count("Weekly Roundup"))

    def test_unknown_anchor_raises_with_candidates(self):
        doc = Document(topic_page(FIRST_TEXT * 10, SECOND_TEXT * 10))

        with self.assertRaises(Unparseable) as context:
            doc.summary(anchor="/html/body/main/article[9]")

        titles = [c["title"] for c in context.exception.candidates]
        self.assertIn("First Story", titles)
        self.assertIn("Second Story", titles)


if __name__ == "__main__":
    unittest.main()
