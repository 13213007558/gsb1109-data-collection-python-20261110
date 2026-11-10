"""Offline mutation tests: decorative changes must not move the article date.

Each transformation starts from a page with a known article date
(2023-05-14) and only adds or moves unrelated decoration (footer copyright,
sidebar "hot articles", comment timestamps). The docstrings state why the
transformation should not change the extracted article date.

Expectations are classified, not all flips are defects:
- stable behavior          -> plain assertion on the invariant
- reproducible problem     -> xfail(strict=True), judgment source cited
- site-structure dependent -> asserted as current, documented behavior

Run with: .venv/bin/python -m pytest tests/mutation/test_mutation.py -v
"""

from pathlib import Path

import pytest
from lxml import html as lh

from htmldate import find_date

PAGES = Path(__file__).parent / "pages"
EXPECTED = "2023-05-14"


def load(name: str) -> str:
    return (PAGES / name).read_text()


def mutate(doc: str, fragment: str, position: str = "bottom") -> str:
    "Append or prepend an HTML fragment to <body>."
    tree = lh.fromstring(doc)
    elem = lh.fragment_fromstring(fragment, create_parent=False)
    if position == "top":
        tree.body.insert(0, elem)
    else:
        tree.body.append(elem)
    return lh.tostring(tree, encoding="unicode")


BASE_CUED = load("base.html")  # date in <p class="byline"><time datetime>
BASE_TIME = load("base_time_bare.html")  # date in bare <time datetime>
BASE_TEXT = load("base_text.html")  # date as plain text only

FOOTER_COPYRIGHT = (
    '<footer><p>&copy; 2024 Example GmbH. Alle Rechte vorbehalten.</p></footer>'
)
SIDEBAR_HOT_TIME = (
    '<aside><div class="widget"><h2>Beliebte Artikel</h2><ul>'
    '<li><a href="/a1">Artikel Eins</a> <time datetime="2024-03-12">12.03.2024</time></li>'
    '<li><a href="/a2">Artikel Zwei</a> <time datetime="2024-01-28">28.01.2024</time></li>'
    "</ul></div></aside>"
)
COMMENTS_PLAIN = (
    '<section id="comments"><div>'
    "<p>Anna schrieb am 03.01.2024 um 10:22</p><p>Toller Artikel!</p>"
    "</div></section>"
)

ALL_BASES = pytest.mark.parametrize("base", [BASE_CUED, BASE_TIME, BASE_TEXT])


# --- stable behavior -------------------------------------------------------


@ALL_BASES
def test_footer_copyright_year_ignored(base):
    """A year-only copyright notice is not a full date and must be ignored.

    Why invariant: '© 2024' carries no month/day, so it can never be the
    article date; examine_text() cannot parse a bare year into a valid date.
    """
    mutated = mutate(base, FOOTER_COPYRIGHT)
    assert find_date(mutated) == EXPECTED
    assert find_date(mutated, original_date=True) == EXPECTED


@ALL_BASES
def test_footer_copyright_position_irrelevant(base):
    """Moving the year-only copyright footer before the article changes nothing.

    Why invariant: same reasoning as above; DOM order of an unparseable
    fragment cannot influence candidate selection.
    """
    mutated = mutate(base, FOOTER_COPYRIGHT, position="top")
    assert find_date(mutated) == EXPECTED


@ALL_BASES
def test_nav_archive_years_ignored(base):
    """Archive links with bare years in link text must be ignored.

    Why invariant: 'Archiv 2024' is navigation chrome, and a bare year is
    not a parseable full date.
    """
    mutated = mutate(
        base,
        '<nav><ul><li><a href="/archiv/2024">Archiv 2024</a></li></ul></nav>',
        position="top",
    )
    assert find_date(mutated) == EXPECTED


@pytest.mark.parametrize("base", [BASE_CUED, BASE_TIME])
def test_plain_comment_dates_ignored_when_date_is_typed(base):
    """Plain-text comment dates must not distract from a <time>/cued date.

    Why invariant: comment paragraphs without cued classes are not date
    candidates, and examine_time_elements() only sees the article's <time>.
    """
    mutated = mutate(base, COMMENTS_PLAIN)
    assert find_date(mutated) == EXPECTED
    assert find_date(mutated, original_date=True) == EXPECTED


def test_cued_byline_beats_all_decoration():
    """A cued byline (class='byline') wins over every tested decoration.

    Why invariant: the byline is a high-precision candidate found first in
    document order; appended decoration never precedes it.
    Judgment source: is_date_candidate() CLASS_CUES, htmldate/core.py:246.
    """
    for fragment in (FOOTER_COPYRIGHT, SIDEBAR_HOT_TIME, COMMENTS_PLAIN):
        assert find_date(mutate(BASE_CUED, fragment)) == EXPECTED
        assert find_date(mutate(BASE_CUED, fragment), original_date=True) == EXPECTED


# --- reproducible problems (xfail: desired invariant, current code flips) --


@pytest.mark.xfail(reason="comment class='meta' matches ID_CLASS_CUES and the "
                          "first candidate in document order wins; "
                          "htmldate/core.py:244 + examine_elements core.py:285",
                   strict=True)
def test_comment_meta_class_does_not_hijack_date():
    """A comment's <p class='meta'> must not outrank the article's <time>.

    Why invariant: the comment timestamp describes the comment, not the
    article; the article date is the only date inside <article>.
    Minimal pair: pages/min_comment_meta_off.html vs min_comment_meta_on.html
    (differ only in the class attribute; 'on' yields 2024-01-03 in all modes).
    """
    mutated = mutate(
        BASE_TIME,
        '<section><div><p class="meta">Anna schrieb am 03.01.2024 um 10:22</p>'
        "<p>Toller Artikel!</p></div></section>",
    )
    assert find_date(mutated) == EXPECTED
    assert find_date(mutated, original_date=True) == EXPECTED


@pytest.mark.xfail(reason="sidebar span class='date' is a candidate and wins "
                          "by document order; is_date_candidate 'date' cue, "
                          "htmldate/core.py:261",
                   strict=True)
def test_sidebar_date_class_does_not_hijack_date():
    """A sidebar 'hot articles' <span class='date'> must not outrank <time>.

    Why invariant: sidebar dates describe other articles; the only date
    inside <article> is 2023-05-14.
    """
    mutated = mutate(
        BASE_TIME,
        '<aside><ul><li><a href="/a1">Artikel Eins</a> '
        '<span class="date">12.03.2024</span></li></ul></aside>',
    )
    assert find_date(mutated) == EXPECTED
    assert find_date(mutated, original_date=True) == EXPECTED


@pytest.mark.xfail(reason="<footer> is in ALWAYS_TAGS and scanned in document "
                          "order, so a footer above the article beats even a "
                          "cued byline; htmldate/core.py:243 + core.py:285",
                   strict=True)
def test_footer_full_date_position_does_not_matter():
    """Moving a full-date footer above the article must not change the result.

    Why invariant: the byline date is the article's publication date; a
    footer 'Stand:' date is page chrome, and its DOM position is a layout
    detail. Minimal pair: pages/min_footer_bottom.html (2023-05-14) vs
    pages/min_footer_top.html (2024-03-02, all modes incl. original_date).
    """
    mutated = mutate(BASE_CUED, "<footer><p>Stand: 02.03.2024</p></footer>",
                     position="top")
    assert find_date(mutated) == EXPECTED
    assert find_date(mutated, original_date=True) == EXPECTED


@pytest.mark.xfail(reason="original_date=True still returns a footer "
                          "'last updated' date from the candidate scan; "
                          "examine_elements has no notion of originality, "
                          "htmldate/core.py:285",
                   strict=True)
def test_original_date_ignores_last_updated_footer():
    """original_date=True must not return a footer modification date.

    Why invariant: 'Zuletzt aktualisiert' is a modification date, the
    opposite of what original_date asks for. Currently returned in all
    modes whenever the article date is not in an earlier cued element.
    """
    mutated = mutate(BASE_TIME,
                     "<footer><p>Zuletzt aktualisiert: 02.03.2024</p></footer>")
    assert find_date(mutated, original_date=True) == EXPECTED


@pytest.mark.xfail(reason="examine_time_elements runs before the free-text "
                          "stage, so any decorative <time> outranks a "
                          "plain-text article date; htmldate/core.py:562 "
                          "called before FREE_TEXT_EXPRESSIONS loop",
                   strict=True)
def test_original_date_text_article_vs_decorative_time():
    """original_date=True: plain-text article date vs. sidebar <time>.

    Why invariant: the article date (plain text, 2023-05-14) is the only
    date inside <article>; the sidebar <time> (2023-11-05) describes another
    article. Currently the sidebar date wins in every mode because the
    time-element stage returns before plain text is ever examined.
    Minimal page: pages/min_text_plus_sidebar_time.html.
    """
    mutated = mutate(
        BASE_TEXT,
        '<aside><ul><li><a href="/a1">Artikel Eins</a> '
        '<time datetime="2023-11-05">05.11.2023</time></li></ul></aside>',
    )
    assert find_date(mutated, original_date=True) == EXPECTED


# --- site-structure dependent (documented current behavior, not defects) ---


def test_default_mode_takes_newest_time_element():
    """Default mode returns the newest <time> on the page, article or not.

    This matches the documented semantics of find_date() (most recent date)
    and flips only because the sidebar uses <time>; original_date=True keeps
    the article date. Minimal page: pages/min_sidebar_time.html.
    Judgment source: examine_time_elements, htmldate/core.py:562.
    """
    mutated = mutate(BASE_TIME, SIDEBAR_HOT_TIME)
    assert find_date(mutated) == "2024-03-12"  # newest <time> wins (default)
    assert find_date(mutated, original_date=True) == EXPECTED


def test_default_mode_takes_newest_freetext_date():
    """Default mode returns the newest free-text date when nothing is typed.

    With a plain-text article date and plain-text comments, the free-text
    stage compares all segments and keeps the newest (default) or oldest
    (original_date=True). Judgment source: compare_reference loop,
    htmldate/core.py:961.
    """
    mutated = mutate(BASE_TEXT, COMMENTS_PLAIN)
    assert find_date(mutated) == "2024-01-03"  # newest free-text date
    assert find_date(mutated, original_date=True) == EXPECTED


def test_fast_mode_cannot_read_plain_text_dates():
    """extensive_search=False finds no date on a plain-text-only page.

    Known limitation, not decoration-related: the free-text stage only runs
    in extensive mode, so fast mode returns None with or without decoration.
    """
    assert find_date(BASE_TEXT, extensive_search=False) is None
    mutated = mutate(BASE_TEXT, FOOTER_COPYRIGHT)
    assert find_date(mutated, extensive_search=False) is None
