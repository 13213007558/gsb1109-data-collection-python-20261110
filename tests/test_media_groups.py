import unittest

from readability import Document


ARTICLE_TEXT = (
    "This is the gallery article body with enough useful text, punctuation, "
    "and additional context for extraction. "
) * 6


def gallery_document(body):
    sample = f"""
    <html><body>
        <nav><img src="/assets/site-logo.svg" alt="Site logo"></nav>
        <article><div class="article-content">
            <p>{ARTICLE_TEXT}</p>
            {body}
            <p>{ARTICLE_TEXT}</p>
        </div></article>
    </body></html>
    """
    return Document(sample)


class TestMediaGroups(unittest.TestCase):
    def test_gallery_figures_keep_captions_and_order(self):
        figures = "".join(
            '<figure><img src="/photos/{0}.jpg" alt="Photo {0}">'
            "<figcaption>Caption for photo {0}</figcaption></figure>".format(index)
            for index in range(3)
        )
        groups = gallery_document(figures).media_groups()

        self.assertEqual([0, 1, 2], [group["position"] for group in groups])
        for index, group in enumerate(groups):
            self.assertEqual("image", group["type"])
            self.assertEqual("Caption for photo %d" % index, group["caption"])
            self.assertEqual("Photo %d" % index, group["alt"])
            self.assertIsNone(group["missing_reason"])
            self.assertEqual(
                "/photos/%d.jpg" % index, group["sources"][0]["url"]
            )

    def test_lazy_loading_sources_keep_their_origin(self):
        groups = gallery_document(
            '<img src="/assets/placeholder.gif" data-src="/real/photo.jpg" '
            'data-srcset="/real/photo.jpg 1x, /real/photo@2x.jpg 2x" '
            'alt="Lazy photo">'
        ).media_groups()

        self.assertEqual(1, len(groups))
        sources = {
            (source["url"], source["origin"]) for source in groups[0]["sources"]
        }
        self.assertIn(("/assets/placeholder.gif", "src"), sources)
        self.assertIn(("/real/photo.jpg", "data-src"), sources)
        self.assertIn(("/real/photo@2x.jpg", "data-srcset"), sources)
        descriptors = {
            source["url"]: source.get("descriptor")
            for source in groups[0]["sources"]
        }
        self.assertEqual("2x", descriptors["/real/photo@2x.jpg"])

    def test_picture_candidates_are_all_preserved(self):
        groups = gallery_document(
            "<picture>"
            '<source srcset="/img/photo.webp 1x, /img/photo@2x.webp 2x" '
            'type="image/webp">'
            '<source media="(max-width: 600px)" srcset="/img/photo-small.jpg">'
            '<img src="/img/photo.jpg" alt="Picture photo">'
            "</picture>"
        ).media_groups()

        self.assertEqual(1, len(groups))
        sources = groups[0]["sources"]
        urls = [source["url"] for source in sources]
        self.assertIn("/img/photo.jpg", urls)
        self.assertIn("/img/photo.webp", urls)
        self.assertIn("/img/photo@2x.webp", urls)
        self.assertIn("/img/photo-small.jpg", urls)
        webp = next(s for s in sources if s["url"] == "/img/photo.webp")
        self.assertEqual("source[srcset]", webp["origin"])
        self.assertEqual("image/webp", webp["type"])
        small = next(s for s in sources if s["url"] == "/img/photo-small.jpg")
        self.assertEqual("(max-width: 600px)", small["media"])

    def test_navigation_and_icon_images_are_excluded(self):
        groups = gallery_document(
            '<img class="icon" src="/assets/icon-share.png" alt="Share">'
            '<img src="/assets/avatar.png" width="16" height="16">'
            '<img src="/photos/team.jpg" alt="The team">'
        ).media_groups()

        urls = [
            source["url"] for group in groups for source in group["sources"]
        ]
        self.assertEqual(["/photos/team.jpg"], urls)
        self.assertEqual("The team", groups[0]["alt"])

    def test_video_and_audio_groups_keep_descriptions(self):
        groups = gallery_document(
            "<figure>"
            '<video controls poster="/img/poster.jpg" title="Clip title">'
            '<source src="/media/clip.mp4" type="video/mp4">'
            "</video>"
            "<figcaption>Clip caption</figcaption>"
            "</figure>"
            '<audio src="/media/episode.mp3" aria-label="Episode one"></audio>'
            '<iframe src="https://www.youtube.com/embed/abc"></iframe>'
        ).media_groups()

        self.assertEqual(["video", "audio", "video"], [g["type"] for g in groups])
        video, audio, iframe = groups
        self.assertEqual("Clip caption", video["caption"])
        self.assertEqual("Clip title", video["alt"])
        video_sources = {(s["url"], s["origin"]) for s in video["sources"]}
        self.assertIn(("/media/clip.mp4", "source[src]"), video_sources)
        self.assertIn(("/img/poster.jpg", "poster"), video_sources)
        self.assertEqual("Episode one", audio["alt"])
        self.assertEqual(
            "/media/episode.mp3", audio["sources"][0]["url"]
        )
        self.assertEqual(
            "https://www.youtube.com/embed/abc", iframe["sources"][0]["url"]
        )

    def test_same_media_twice_keeps_each_caption(self):
        groups = gallery_document(
            '<figure><img src="/photos/shared.jpg">'
            "<figcaption>First context</figcaption></figure>"
            '<figure><img src="/photos/shared.jpg">'
            "<figcaption>Second context</figcaption></figure>"
        ).media_groups()

        self.assertEqual(2, len(groups))
        self.assertEqual("First context", groups[0]["caption"])
        self.assertEqual("Second context", groups[1]["caption"])
        for group in groups:
            self.assertEqual("/photos/shared.jpg", group["sources"][0]["url"])

    def test_class_based_caption_is_associated(self):
        groups = gallery_document(
            '<div><img src="/photos/solo.jpg">'
            '<div class="caption">A class based caption</div></div>'
        ).media_groups()

        self.assertEqual(1, len(groups))
        self.assertEqual("A class based caption", groups[0]["caption"])

    def test_missing_resource_reports_reason(self):
        groups = gallery_document(
            '<img alt="Broken image">'
            "<video></video>"
            '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">'
        ).media_groups()

        self.assertEqual(3, len(groups))
        self.assertEqual([], groups[0]["sources"])
        self.assertIn("no resource URL", groups[0]["missing_reason"])
        self.assertIn("no resource URL", groups[1]["missing_reason"])
        self.assertIn("data: URI", groups[2]["missing_reason"])

    def test_xpath_is_reported_when_enabled(self):
        sample = f"""
        <html><body><article><div class="article-content">
            <p>{ARTICLE_TEXT}</p>
            <img src="/photos/x.jpg" alt="X">
        </div></article></body></html>
        """
        groups = Document(sample, xpath=True).media_groups()

        self.assertEqual(1, len(groups))
        self.assertIn("xpath", groups[0])
        self.assertTrue(groups[0]["xpath"].startswith("/html/body"))

    def test_summary_is_not_affected_by_media_groups(self):
        sample = f"""
        <html><body><article><div class="article-content">
            <p>{ARTICLE_TEXT}</p>
            <figure><img src="/photos/a.jpg" alt="A">
            <figcaption>Caption A</figcaption></figure>
        </div></article></body></html>
        """
        doc = Document(sample)
        groups = doc.media_groups()
        summary = Document(sample).summary()

        self.assertEqual(1, len(groups))
        self.assertIn("gallery article body", summary)


if __name__ == "__main__":
    unittest.main()
