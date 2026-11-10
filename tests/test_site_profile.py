"""Tests for optional same-site template profiles.

The suite deliberately pairs two failure modes:

* a genuine navigation template (related-recommendation panel with long
  teasers) that single-page heuristics keep in the article body;
* repeated legitimate text (syndicated article intro, column description)
  that a naive signature-frequency approach would delete.

Only evidence-backed navigation blocks may be removed, so the profile has
to solve the first case while provably refusing the second one.
"""
import json
import subprocess
import sys

import pytest

from readability import Document
from readability import CrossSiteProfileError
from readability import ProfileScopeError
from readability.siteprofile import SiteProfile
from readability.siteprofile import _parse_reference
from readability.siteprofile import _signature

DOMAIN = "news.example.test"
ARTICLE_BODY = " ".join(
    ["This is a substantive news paragraph with reporting and details."] * 10
)
WIRE_INTRO = (
    "This wire story is republished in full under our shared newsroom "
    "agreement and must remain part of the article body."
)
COLUMN_NOTE = (
    "The policy desk explains regulation in plain language, with original "
    "reporting published every weekday morning for readers."
)
CARD_TEASER = (
    "Analysis and context from the newsroom desk, covering background data, "
    "interviews and reactions from multiple sources in detail here."
)


def recommendation_cards():
    return "".join(
        '<div class="grid__item">'
        '<a href="/story/{0}">Related headline number {0}</a>'
        "<p>{1}</p><p>{1}</p></div>".format(index, CARD_TEASER)
        for index in range(6)
    )


def build_page(article_id, *, include_column_note=True, grid_extra=""):
    navigation = (
        '<nav class="global-nav"><ul>'
        + "".join(
            '<li><a href="/section/{0}">Section {0}</a></li>'.format(i)
            for i in range(6)
        )
        + "</ul></nav>"
    )
    footer = (
        '<footer class="site-footer">'
        + " ".join(
            '<a href="/page/{0}">Footer policy link {0}</a>'.format(i)
            for i in range(8)
        )
        + "</footer>"
    )
    column_note = (
        '<div class="column-note"><p>{}</p></div>'.format(COLUMN_NOTE)
        if include_column_note
        else ""
    )
    return """
    <html><body>{navigation}
      <main><article>
        <h1>Story {article_id}</h1>
        <div class="article-body">
          <p class="wire-intro">{wire_intro}</p>
          <p>{body}</p>
          {column_note}
        </div>
        <div class="grid" data-module="cards">
          <h2>Recommended for you</h2>
          {grid_extra}
          {cards}
        </div>
      </article></main>
      {footer}
    </body></html>
    """.format(
        navigation=navigation,
        article_id=article_id,
        wire_intro=WIRE_INTRO,
        body=ARTICLE_BODY,
        column_note=column_note,
        grid_extra=grid_extra,
        cards=recommendation_cards(),
        footer=footer,
    )


@pytest.fixture
def trained_profile():
    references = [build_page(i) for i in range(3)]
    return SiteProfile.train(references, domain=DOMAIN)


class TestProfileTraining:
    def test_learns_repeated_navigation_templates(self, trained_profile):
        rules = {
            (rule["tag"], tuple(rule["class"])): rule
            for rule in trained_profile.rules
        }
        assert ("div", ("grid",)) in rules
        assert ("nav", ("global-nav",)) in rules
        assert ("footer", ("site-footer",)) in rules
        assert "repeated link items" in rules[("div", ("grid",))]["evidence"]
        for rule in trained_profile.rules:
            assert rule["page_count"] == 3

    def test_repeated_article_prose_is_not_learned(self, trained_profile):
        classes = {name for rule in trained_profile.rules for name in rule["class"]}
        assert "wire-intro" not in classes

    def test_legitimate_column_note_is_not_learned(self, trained_profile):
        classes = {name for rule in trained_profile.rules for name in rule["class"]}
        assert "column-note" not in classes

    def test_single_occurrence_is_not_learned(self):
        profile = SiteProfile.train(
            [build_page(0)], domain=DOMAIN, min_occurrences=2
        )
        assert profile.rules == ()

    def test_identifiable_signature_required(self):
        page = """
        <html><body><main><article>
          <p>{body}</p>
          <ul>{links}</ul>
        </article></main></body></html>
        """.format(
            body=ARTICLE_BODY,
            links="".join(
                '<li><a href="/x/{0}">Generic link {0}</a></li>'.format(i)
                for i in range(6)
            ),
        )
        profile = SiteProfile.train([page, page], domain=DOMAIN)
        assert all(rule["id"] or rule["class"] for rule in profile.rules)
        assert not any(rule["tag"] == "ul" and not rule["class"] for rule in profile.rules)

    def test_training_does_not_mutate_reference_pages(self):
        references = [build_page(i) for i in range(2)]
        snapshots = list(references)
        SiteProfile.train(references, domain=DOMAIN)
        assert references == snapshots

        with pytest.raises(TypeError):
            SiteProfile.train(
                [_parse_reference(build_page(0))], domain=DOMAIN
            )


class TestProfileExtraction:
    def test_baseline_keeps_low_density_recommendation_block(self):
        # Characterization test: without a profile the recommendation
        # panel leaks because its long teasers lower link density below
        # the single-page heuristics' removal threshold.
        summary = Document(
            build_page(99), url="https://{}/a/99".format(DOMAIN)
        ).summary()
        assert "Recommended for you" in summary
        assert "Related headline number 5" in summary

    def test_profile_removes_recommendation_block(self, trained_profile):
        summary = Document(
            build_page(99),
            url="https://{}/a/99".format(DOMAIN),
            site_profile=trained_profile,
        ).summary()
        assert "Recommended for you" not in summary
        assert "Related headline number 5" not in summary
        assert CARD_TEASER[:30] not in summary
        assert "substantive news paragraph" in summary

    def test_profile_removes_navigation_and_footer(self, trained_profile):
        summary = Document(
            build_page(99),
            url="https://{}/a/99".format(DOMAIN),
            site_profile=trained_profile,
        ).summary()
        assert "Section 5" not in summary
        assert "Footer policy link 7" not in summary

    def test_removed_blocks_are_reported(self, trained_profile):
        document = Document(
            build_page(99),
            url="https://{}/a/99".format(DOMAIN),
            site_profile=trained_profile,
        )
        document.summary()
        removed = {
            (block["tag"], block["class"]) for block in document.removed_profile_blocks
        }
        assert ("div", "grid") in removed
        assert ("nav", "global-nav") in removed

    def test_keeps_syndicated_intro_and_column_note(self, trained_profile):
        summary = Document(
            build_page(99),
            url="https://{}/a/99".format(DOMAIN),
            site_profile=trained_profile,
        ).summary()
        assert "republished in full" in summary
        assert "policy desk explains regulation" in summary
        assert "substantive news paragraph" in summary

    def test_matching_signature_holding_prose_is_kept(self, trained_profile):
        editorial_grid = """
        <html><body><main><article>
          <h1>Story 100</h1>
          <div class="article-body"><p>{body}</p></div>
          <div class="grid" data-module="cards">
            <div class="grid__item"><p>{legit}</p></div>
            <div class="grid__item"><p>{legit}</p></div>
            <div class="grid__item"><p>{legit}</p></div>
          </div>
        </article></main></body></html>
        """.format(
            body=ARTICLE_BODY,
            legit=(
                "Legitimate editorial guidance written for this article only, "
                "with no navigation links and substantial prose of its own."
            ),
        )
        summary = Document(
            editorial_grid,
            url="https://{}/a/100".format(DOMAIN),
            site_profile=trained_profile,
        ).summary()
        assert "Legitimate editorial guidance" in summary
        assert "substantive news paragraph" in summary
        assert document_removed_classes(editorial_grid, trained_profile) == set()

    def test_target_page_revalidation_blocks_removal(self, trained_profile):
        # Same grid signature, but on this page it contains one inline link
        # inside prose instead of a list of link cards.
        prose = (
            "Editorial analysis referencing "
            '<a href="/context">one related document</a> '
            "among many sentences of genuine article prose that must be "
            "retained by the extraction pipeline. "
            + "More genuine sentences to keep this section substantial. " * 6
        )
        repurposed = """
        <html><body><main><article>
          <h1>Story 101</h1>
          <div class="article-body"><p>{body}</p>
            <div class="grid" data-module="cards"><p>{prose}</p></div>
          </div>
        </article></main></body></html>
        """.format(body=ARTICLE_BODY, prose=prose)
        document = Document(
            repurposed,
            url="https://{}/a/101".format(DOMAIN),
            site_profile=trained_profile,
        )
        summary = document.summary()
        assert "genuine article prose that must be retained" in summary
        assert not any(
            block["class"] == "grid" for block in document.removed_profile_blocks
        )

    def test_empty_profile_is_a_noop(self):
        profile = SiteProfile.train(
            ["<html><body><p>Only prose without templates here.</p></body></html>"],
            domain=DOMAIN,
        )
        summary = Document(
            build_page(99),
            url="https://{}/a/99".format(DOMAIN),
            site_profile=profile,
        ).summary()
        assert "substantive news paragraph" in summary


def document_removed_classes(html, profile):
    document = Document(
        html, url="https://{}/x".format(DOMAIN), site_profile=profile
    )
    document.summary()
    return {block["class"] for block in document.removed_profile_blocks}


class TestProfileScope:
    def test_cross_site_profile_rejected_by_default(self, trained_profile):
        with pytest.raises(CrossSiteProfileError):
            Document(
                build_page(99),
                url="https://other.example.org/a/99",
                site_profile=trained_profile,
            )

    def test_cross_site_profile_allowed_explicitly(self, trained_profile):
        summary = Document(
            build_page(99),
            url="https://other.example.org/a/99",
            site_profile=trained_profile,
            allow_cross_site_profile=True,
        ).summary()
        assert "Recommended for you" not in summary
        assert "substantive news paragraph" in summary

    def test_scoped_profile_requires_url(self, trained_profile):
        with pytest.raises(ProfileScopeError):
            Document(build_page(99), site_profile=trained_profile)

    def test_unscoped_profile_requires_url(self):
        profile = SiteProfile.train([build_page(0), build_page(1)])
        with pytest.raises(ProfileScopeError):
            Document(
                build_page(2),
                url="https://{}/a/2".format(DOMAIN),
                site_profile=profile,
            )

    def test_subdomain_matches_profile_scope(self, trained_profile):
        document = Document(
            build_page(99),
            url="https://www.{}/a/99".format(DOMAIN),
            site_profile=trained_profile,
        )
        assert document.site_profile is trained_profile

    def test_invalid_profile_type_rejected(self):
        with pytest.raises(TypeError):
            Document(
                build_page(99),
                url="https://{}/a/99".format(DOMAIN),
                site_profile={"tag": "div"},
            )


class TestProfilePersistence:
    def test_save_and_load_roundtrip(self, trained_profile, tmp_path):
        path = tmp_path / "profile.json"
        trained_profile.save(str(path))

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["domain"] == DOMAIN
        assert data["schema_version"] == 1
        assert all("evidence" in rule for rule in data["rules"])

        loaded = SiteProfile.load(str(path))
        assert loaded.domain == trained_profile.domain
        assert loaded.rules == trained_profile.rules

        summary = Document(
            build_page(7),
            url="https://{}/a/7".format(DOMAIN),
            site_profile=loaded,
        ).summary()
        assert "Recommended for you" not in summary
        assert "republished in full" in summary

    def test_unknown_schema_version_rejected(self, tmp_path):
        path = tmp_path / "profile.json"
        data = {"schema_version": 999, "domain": DOMAIN, "rules": []}
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError):
            SiteProfile.load(str(path))

    def test_describe_explains_scope_and_evidence(self, trained_profile):
        explanation = trained_profile.describe()
        assert DOMAIN in explanation
        assert "Cross-site use is rejected" in explanation
        assert "repeated link items" in explanation
        assert trained_profile.scope == DOMAIN


class TestNaiveFrequencyRisk:
    """Show the false-deletion risk that evidence gating prevents."""

    @staticmethod
    def naive_frequent_signatures(pages, minimum=2):
        counts = {}
        for page in pages:
            doc = _parse_reference(page)
            for elem in doc.findall(".//*"):
                if not isinstance(elem.tag, str):
                    continue
                if not (elem.get("id") or elem.get("class")):
                    continue
                counts[_signature(elem)] = counts.get(_signature(elem), 0) + 1
        return {signature for signature, count in counts.items() if count >= minimum}

    def test_naive_frequency_method_targets_syndicated_intro(self):
        references = [build_page(i) for i in range(3)]
        naive = self.naive_frequent_signatures(references)
        target = _parse_reference(build_page(42))
        intro = target.find('.//p[@class="wire-intro"]')

        # The naive "delete anything repeated" rule would flag the
        # syndicated article intro for removal on a new page.
        assert _signature(intro) in naive

        profile = SiteProfile.train(references, domain=DOMAIN)
        matched = [
            rule for rule in profile.rules if profile.signature_matches(intro, rule)
        ]
        assert matched == []

        summary = Document(
            build_page(42),
            url="https://{}/a/42".format(DOMAIN),
            site_profile=profile,
        ).summary()
        assert "republished in full" in summary
        assert "Recommended for you" not in summary

    def test_naive_frequency_method_targets_column_note(self):
        references = [build_page(i) for i in range(3)]
        naive = self.naive_frequent_signatures(references)
        target = _parse_reference(build_page(42))
        note = target.find('.//div[@class="column-note"]')

        assert _signature(note) in naive

        profile = SiteProfile.train(references, domain=DOMAIN)
        assert not any(
            profile.signature_matches(note, rule) for rule in profile.rules
        )


class TestProfileCommandLine:
    def test_train_save_and_apply(self, tmp_path):
        references = [build_page(i) for i in range(3)]
        reference_paths = []
        for index, html in enumerate(references):
            path = tmp_path / "reference{}.html".format(index)
            path.write_text(html, encoding="utf-8")
            reference_paths.append(str(path))
        target = tmp_path / "target.html"
        target.write_text(build_page(99), encoding="utf-8")
        profile_path = tmp_path / "profile.json"

        train = subprocess.run(
            [
                sys.executable,
                "-m",
                "readability",
                "--train-profile",
                DOMAIN,
                "--save-profile",
                str(profile_path),
                *sum([["-r", path] for path in reference_paths], []),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "evidence-backed" in train.stdout
        assert json.loads(profile_path.read_text(encoding="utf-8"))["domain"] == DOMAIN

        extract = subprocess.run(
            [
                sys.executable,
                "-m",
                "readability",
                "--load-profile",
                str(profile_path),
                "--page-url",
                "https://{}/a/99".format(DOMAIN),
                str(target),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "Related headline number 5" not in extract.stdout
        assert "substantive news paragraph" in extract.stdout
        assert "republished in full" in extract.stdout

        cross_site = subprocess.run(
            [
                sys.executable,
                "-m",
                "readability",
                "--load-profile",
                str(profile_path),
                "--page-url",
                "https://other.example.org/a/99",
                str(target),
            ],
            capture_output=True,
            text=True,
        )
        assert cross_site.returncode != 0
        assert "CrossSiteProfileError" in cross_site.stderr
