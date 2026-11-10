import unittest

import lxml.html

from readability import ContentStatus
from readability import Document
from readability.content_status import CONTENT_STATES
from readability.content_status import analyze_content_status
from readability.content_status import collect_structural_signals
from readability.content_status import determine_content_status


ARTICLE_TEXT = (
    "This is ordinary article text with punctuation and useful facts. " * 12
)
SHORT_ARTICLE_TEXT = (
    "A short but real article note with a full stop at the end."
)

SIGNAL_KEYS = {"source", "signal", "value", "indicates", "strength"}


class TestContentStatus(unittest.TestCase):
    def test_short_full_text_is_never_guaranteed_complete(self):
        """A short marker-free article stays unknown, never 'complete'."""
        html = (
            "<html><body><article><p>{}</p></article></body></html>".format(
                ARTICLE_TEXT
            )
        )

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.UNKNOWN, result.state)
        self.assertEqual([], result.signals)
        self.assertFalse(result.contradictory_signals)
        self.assertGreater(result.body_text_length, 0)
        self.assertEqual(
            {
                ContentStatus.UNKNOWN,
                ContentStatus.PREVIEW,
                ContentStatus.RESTRICTED,
                ContentStatus.NO_CONTENT,
            },
            CONTENT_STATES,
        )

    def test_short_body_alone_is_not_truncation_evidence(self):
        html = (
            "<html><body><article><p>{}</p></article></body></html>".format(
                SHORT_ARTICLE_TEXT
            )
        )

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.UNKNOWN, result.state)
        self.assertGreaterEqual(
            result.body_text_length, 25
        )  # real text was extracted
        self.assertEqual([], result.signals)

    def test_subscription_promo_does_not_prove_restriction(self):
        html = """
        <html><body>
            <article><p>{text}</p></article>
            <aside class="subscription-promo newsletter-signup">
                <p>Subscribe to our newsletter today for more stories.</p>
            </aside>
        </body></html>
        """.format(text=ARTICLE_TEXT)

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.UNKNOWN, result.state)
        self.assertEqual([], result.signals)

    def test_long_preview_is_marked_preview_with_signals(self):
        html = """
        <html><head>
            <meta name="content_tier" content="preview">
        </head><body>
            <div data-content-tier="preview" class="preview-text">
                <article><p>{text}</p>
                <p>The story continues after this preview teaser.</p></article>
            </div>
            <aside class="subscription-promo">
                <p>Subscribe now for unlimited access.</p>
            </aside>
        </body></html>
        """.format(text=ARTICLE_TEXT)

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.PREVIEW, result.state)
        self.assertFalse(result.contradictory_signals)
        self.assertGreaterEqual(result.body_text_length, 250)
        indications = {signal["indicates"] for signal in result.signals}
        self.assertIn("preview", indications)
        self.assertNotIn("restricted", indications)
        for signal in result.signals:
            self.assertEqual(SIGNAL_KEYS, set(signal))
        self.assertTrue(
            any(
                signal["source"] == "meta"
                and signal["signal"] == "content_tier"
                for signal in result.signals
            )
        )
        self.assertTrue(
            any(
                signal["source"] == "data-attribute"
                and signal["signal"] == "data-content-tier"
                for signal in result.signals
            )
        )

    def test_explicit_paywall_is_restricted_even_with_long_body(self):
        html = """
        <html><head>
            <script type="application/ld+json">
            {{"@type":"NewsArticle","isAccessibleForFree": false,
              "isPartOf": {{"@type":"WebPageElement",
                            "isAccessibleForFree": false}}}}
            </script>
        </head><body>
            <div data-paywall="true"><article><p>{text}</p></article></div>
        </body></html>
        """.format(text=ARTICLE_TEXT)

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.RESTRICTED, result.state)
        self.assertGreaterEqual(result.body_text_length, 250)
        jsonld = [
            signal
            for signal in result.signals
            if signal["source"] == "json-ld"
        ]
        self.assertTrue(jsonld)
        self.assertTrue(
            any(
                signal["signal"] == "isAccessibleForFree"
                and signal["indicates"] == "restricted"
                for signal in jsonld
            )
        )
        self.assertTrue(
            any(
                signal["signal"] == "isPartOf.isAccessibleForFree"
                for signal in jsonld
            )
        )

    def test_pure_login_page_without_article_is_restricted(self):
        html = """
        <html><body>
            <div id="login-wall" class="gate-login">
                <h1>Please sign in to continue reading</h1>
                <form>
                    <input type="email" name="email">
                    <input type="password" name="password">
                    <button type="submit">Log in</button>
                </form>
            </div>
        </body></html>
        """

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.RESTRICTED, result.state)
        self.assertLess(result.body_text_length, 250)
        signals = {
            (signal["source"], signal["signal"])
            for signal in result.signals
        }
        self.assertIn(("element", "id:login"), signals)
        self.assertIn(("element", "class:login"), signals)
        self.assertIn(("form", "password-input"), signals)

    def test_password_input_alone_marks_login_page_restricted(self):
        html = """
        <html><body>
            <form><input type="password"><button>Continue</button></form>
        </body></html>
        """

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.RESTRICTED, result.state)
        self.assertTrue(
            any(
                signal["source"] == "form"
                and signal["signal"] == "password-input"
                for signal in result.signals
            )
        )

    def test_login_widget_on_substantial_page_stays_unknown(self):
        """A sign-in box next to a real article does not make a wall."""
        html = """
        <html><body>
            <article><p>{text}</p></article>
            <form class="signin-box">
                <input type="password"><button>Sign in</button>
            </form>
        </body></html>
        """.format(text=ARTICLE_TEXT)

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.UNKNOWN, result.state)
        self.assertTrue(
            any(
                signal["signal"] == "password-input"
                for signal in result.signals
            )
        )

    def test_teaser_cards_on_full_article_stay_unknown(self):
        """Teaser/preview UI vocabulary is weak and stays recorded."""
        cards = "".join(
            '<div class="teaser-card"><p>Related story {}</p></div>'.format(i)
            for i in range(4)
        )
        html = """
        <html><body>
            <article><p>{text}</p></article>
            <section class="related-teasers">{cards}</section>
        </body></html>
        """.format(text=ARTICLE_TEXT, cards=cards)

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.UNKNOWN, result.state)
        self.assertTrue(
            any(
                signal["signal"] == "class:teaser"
                and signal["strength"] == "weak"
                for signal in result.signals
            )
        )

    def test_paywall_class_token_is_strong_on_full_article(self):
        html = """
        <html><body>
            <div class="paywall-meter"><article><p>{text}</p></article></div>
        </body></html>
        """.format(text=ARTICLE_TEXT)

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.RESTRICTED, result.state)
        self.assertTrue(
            any(
                signal["signal"] == "class:paywall"
                and signal["strength"] == "strong"
                for signal in result.signals
            )
        )

    def test_empty_page_without_markers_is_no_content(self):
        result = Document("<html><body></body></html>").content_status()

        self.assertEqual(ContentStatus.NO_CONTENT, result.state)
        self.assertEqual(0, result.body_text_length)
        self.assertEqual([], result.signals)

    def test_no_content_page_carries_no_completeness_claim(self):
        result = Document(
            "<html><body><div></div></body></html>"
        ).content_status()

        self.assertEqual(ContentStatus.NO_CONTENT, result.state)
        self.assertEqual([], result.signals)
        self.assertFalse(result.contradictory_signals)

    def test_contradictory_markers_are_preserved_and_unknown(self):
        html = """
        <html><head>
            <script type="application/ld+json">
            {{"@type":"NewsArticle","isAccessibleForFree": true}}
            </script>
        </head><body>
            <div class="paywall" data-paywall="true">
                <article><p>{text}</p></article>
            </div>
        </body></html>
        """.format(text=ARTICLE_TEXT)

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.UNKNOWN, result.state)
        self.assertTrue(result.contradictory_signals)
        indications = {signal["indicates"] for signal in result.signals}
        self.assertIn("restricted", indications)
        self.assertIn("free", indications)

    def test_free_and_restricted_meta_markers_contradict(self):
        html = """
        <html><head>
            <meta name="content_tier" content="premium">
            <meta name="accessRights" content="free">
        </head><body><article><p>{text}</p></article></body></html>
        """.format(text=ARTICLE_TEXT)

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.UNKNOWN, result.state)
        self.assertTrue(result.contradictory_signals)

    def test_microdata_paywall_marker_is_recorded(self):
        html = """
        <html><body>
            <div itemscope itemtype="https://schema.org/NewsArticle">
                <meta itemprop="isAccessibleForFree" content="false">
                <article><p>{text}</p></article>
            </div>
        </body></html>
        """.format(text=ARTICLE_TEXT)

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.RESTRICTED, result.state)
        self.assertTrue(
            any(
                signal["source"] == "microdata"
                and signal["signal"] == "isAccessibleForFree"
                and signal["indicates"] == "restricted"
                for signal in result.signals
            )
        )

    def test_malformed_json_ld_is_ignored(self):
        html = """
        <html><head>
            <script type="application/ld+json">{{not valid json</script>
            <script type="application/ld+json">
            {{"@type":"NewsArticle","isAccessibleForFree": false}}
            </script>
        </head><body><article><p>{text}</p></article></body></html>
        """.format(text=ARTICLE_TEXT)

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.RESTRICTED, result.state)
        self.assertEqual(
            1,
            len(
                [
                    signal
                    for signal in result.signals
                    if signal["source"] == "json-ld"
                ]
            ),
        )

    def test_signals_survive_summary_dom_mutation(self):
        html = """
        <html><head>
            <script type="application/ld+json">
            {{"@type":"NewsArticle","isAccessibleForFree": false}}
            </script>
        </head><body>
            <div data-paywall><article><p>{text}</p></article></div>
        </body></html>
        """.format(text=ARTICLE_TEXT)

        doc = Document(html)
        summary = doc.summary()
        result = doc.content_status()

        self.assertIn("ordinary article text", summary)
        self.assertEqual(ContentStatus.RESTRICTED, result.state)
        self.assertTrue(
            any(
                signal["source"] == "json-ld"
                for signal in result.signals
            )
        )

    def test_content_status_is_computed_once(self):
        doc = Document(
            "<html><body><article><p>{}</p></article></body></html>".format(
                ARTICLE_TEXT
            )
        )

        self.assertIs(doc.content_status(), doc.content_status())

    def test_as_dict_exposes_state_and_signals(self):
        result = Document(
            "<html><body></body></html>"
        ).content_status()

        data = result.as_dict()

        self.assertEqual(
            ["body_text_length", "contradictory_signals", "signals", "state"],
            sorted(data),
        )
        self.assertEqual(ContentStatus.NO_CONTENT, data["state"])

    def test_bytes_input_is_supported(self):
        html = (
            "<html><body>"
            '<div data-content-tier="preview"><article><p>{}</p></article>'
            "</div></body></html>".format(ARTICLE_TEXT)
        ).encode("utf-8")

        result = Document(html).content_status()

        self.assertEqual(ContentStatus.PREVIEW, result.state)

    def test_analyze_helper_uses_pre_clean_document(self):
        html = """
        <html><head>
            <script type="application/ld+json">
            {{"@type":"NewsArticle","isAccessibleForFree": false}}
            </script>
        </head><body><article><p>{text}</p></article></body></html>
        """.format(text=ARTICLE_TEXT)
        doc = lxml.html.document_fromstring(html)
        blocks = [
            script.text
            for script in doc.findall(
                ".//script[@type='application/ld+json']"
            )
        ]
        structural = collect_structural_signals(doc)

        result = analyze_content_status(
            doc, blocks, "<div><p>{}</p></div>".format(ARTICLE_TEXT), 25, 250
        )

        self.assertEqual(ContentStatus.RESTRICTED, result.state)
        self.assertTrue(
            any(
                signal["source"] == "json-ld"
                for signal in result.signals
            )
        )
        self.assertEqual(structural, [])


class TestDetermineContentStatusRules(unittest.TestCase):
    def signal(self, name, indication, strength):
        return {
            "source": "test",
            "signal": name,
            "value": "1",
            "indicates": indication,
            "strength": strength,
        }

    def test_no_text_no_markers_is_no_content(self):
        state, contradictory = determine_content_status([], 0, 25, 250)

        self.assertEqual(ContentStatus.NO_CONTENT, state)
        self.assertFalse(contradictory)

    def test_no_text_with_restricted_marker_is_restricted(self):
        signals = [self.signal("data-paywall", "restricted", "strong")]
        state, _ = determine_content_status(signals, 0, 25, 250)

        self.assertEqual(ContentStatus.RESTRICTED, state)

    def test_short_text_without_markers_stays_unknown(self):
        state, _ = determine_content_status([], 100, 25, 250)

        self.assertEqual(ContentStatus.UNKNOWN, state)

    def test_weak_marker_on_substantial_page_stays_unknown(self):
        signals = [
            self.signal("class:preview", "preview", "weak"),
            self.signal("id:login", "registration", "weak"),
        ]
        state, _ = determine_content_status(signals, 800, 25, 250)

        self.assertEqual(ContentStatus.UNKNOWN, state)

    def test_strong_marker_on_substantial_page_is_restricted(self):
        signals = [self.signal("data-paywall", "restricted", "strong")]
        state, _ = determine_content_status(signals, 800, 25, 250)

        self.assertEqual(ContentStatus.RESTRICTED, state)

    def test_substantial_text_with_only_registration_marker_is_unknown(self):
        signals = [
            self.signal("password-input", "registration", "weak")
        ]
        state, _ = determine_content_status(signals, 800, 25, 250)

        self.assertEqual(ContentStatus.UNKNOWN, state)

    def test_insufficient_text_with_registration_marker_is_restricted(self):
        signals = [
            self.signal("password-input", "registration", "weak")
        ]
        state, _ = determine_content_status(signals, 30, 25, 250)

        self.assertEqual(ContentStatus.RESTRICTED, state)

    def test_weak_preview_marker_below_retry_length_is_preview(self):
        signals = [self.signal("class:teaser", "preview", "weak")]
        state, _ = determine_content_status(signals, 100, 25, 250)

        self.assertEqual(ContentStatus.PREVIEW, state)

    def test_preview_marker_with_substantial_text_is_preview(self):
        signals = [
            self.signal("meta:content_tier", "preview", "strong")
        ]
        state, _ = determine_content_status(signals, 800, 25, 250)

        self.assertEqual(ContentStatus.PREVIEW, state)

    def test_contradiction_forces_unknown_even_with_text(self):
        signals = [
            self.signal("isAccessibleForFree", "free", "strong"),
            self.signal("data-paywall", "restricted", "strong"),
        ]
        state, contradictory = determine_content_status(
            signals, 800, 25, 250
        )

        self.assertEqual(ContentStatus.UNKNOWN, state)
        self.assertTrue(contradictory)

    def test_weak_and_strong_contradiction_is_flagged(self):
        signals = [
            self.signal("class:premium", "restricted", "weak"),
            self.signal("meta:accessRights", "free", "strong"),
        ]
        state, contradictory = determine_content_status(
            signals, 100, 25, 250
        )

        self.assertEqual(ContentStatus.UNKNOWN, state)
        self.assertTrue(contradictory)


if __name__ == "__main__":
    unittest.main()
