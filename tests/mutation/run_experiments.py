"""Offline mutation experiments for htmldate robustness.

Applies decorative transformations to a base page with a known article date
and records whether extraction results change. Run with:
    .venv/bin/python tests/mutation/run_experiments.py
"""

from pathlib import Path

from lxml import html as lh
from lxml.html import HtmlElement

from htmldate import find_date

PAGES = Path(__file__).parent / "pages"

BASES = {
    # article date in a cued element (class="byline" + <time>): high-precision tier
    "base_cued": (PAGES / "base.html").read_text(),
    # article date in a bare <time datetime> without class cues: middle tier
    "base_time_bare": (PAGES / "base_time_bare.html").read_text(),
    # article date as plain text only: lowest tier
    "base_text": (PAGES / "base_text.html").read_text(),
}
EXPECTED = "2023-05-14"


def parse(doc: str) -> HtmlElement:
    return lh.fromstring(doc)


def serialize(tree: HtmlElement) -> str:
    return lh.tostring(tree, encoding="unicode")


def add_footer_copyright(doc: str) -> str:
    """Footer with a year-only copyright notice, later than the article date."""
    tree = parse(doc)
    footer = lh.fragment_fromstring(
        '<footer><p>&copy; 2024 Example GmbH. Alle Rechte vorbehalten.</p></footer>',
        create_parent=False,
    )
    tree.body.append(footer)
    return serialize(tree)


def add_footer_copyright_range(doc: str) -> str:
    """Footer with a copyright year range spanning the article date."""
    tree = parse(doc)
    footer = lh.fragment_fromstring(
        '<footer><p>&copy; 2019-2024 Example GmbH</p></footer>',
        create_parent=False,
    )
    tree.body.append(footer)
    return serialize(tree)


def add_footer_moved_top(doc: str) -> str:
    """Same copyright footer, but placed before the article in DOM order."""
    tree = parse(doc)
    footer = lh.fragment_fromstring(
        '<footer><p>&copy; 2024 Example GmbH. Alle Rechte vorbehalten.</p></footer>',
        create_parent=False,
    )
    tree.body.insert(0, footer)
    return serialize(tree)


def add_sidebar_hot_text(doc: str) -> str:
    """Sidebar 'hot articles' widget with plain-text dates newer than the article."""
    tree = parse(doc)
    sidebar = lh.fragment_fromstring(
        """<aside id="sidebar"><div class="widget">
        <h2>Beliebte Artikel</h2>
        <ul>
        <li><a href="/a1">Artikel Eins</a> <span class="date">12.03.2024</span></li>
        <li><a href="/a2">Artikel Zwei</a> <span class="date">28.01.2024</span></li>
        <li><a href="/a3">Artikel Drei</a> <span class="date">05.11.2023</span></li>
        </ul></div></aside>""",
        create_parent=False,
    )
    tree.body.append(sidebar)
    return serialize(tree)


def add_sidebar_hot_time(doc: str) -> str:
    """Sidebar 'hot articles' widget using <time datetime> elements."""
    tree = parse(doc)
    sidebar = lh.fragment_fromstring(
        """<aside id="sidebar"><div class="widget">
        <h2>Beliebte Artikel</h2>
        <ul>
        <li><a href="/a1">Artikel Eins</a> <time datetime="2024-03-12">12.03.2024</time></li>
        <li><a href="/a2">Artikel Zwei</a> <time datetime="2024-01-28">28.01.2024</time></li>
        <li><a href="/a3">Artikel Drei</a> <time datetime="2023-11-05">05.11.2023</time></li>
        </ul></div></aside>""",
        create_parent=False,
    )
    tree.body.append(sidebar)
    return serialize(tree)


def add_comments_text(doc: str) -> str:
    """Comment section with plain-text timestamps newer than the article."""
    tree = parse(doc)
    comments = lh.fragment_fromstring(
        """<section id="comments">
        <h2>Kommentare</h2>
        <div class="comment"><p class="meta">Anna schrieb am 2024-01-03 10:22</p>
        <p>Toller Artikel!</p></div>
        <div class="comment"><p class="meta">Ben schrieb am 2023-12-30 08:15</p>
        <p>Danke für die Info.</p></div>
        </section>""",
        create_parent=False,
    )
    tree.body.append(comments)
    return serialize(tree)


def add_comments_time(doc: str) -> str:
    """Comment section with <time datetime> timestamps newer than the article."""
    tree = parse(doc)
    comments = lh.fragment_fromstring(
        """<section id="comments">
        <h2>Kommentare</h2>
        <div class="comment"><p class="meta">Anna, <time datetime="2024-01-03T10:22:00">3. Januar 2024</time></p>
        <p>Toller Artikel!</p></div>
        <div class="comment"><p class="meta">Ben, <time datetime="2023-12-30T08:15:00">30. Dezember 2023</time></p>
        <p>Danke für die Info.</p></div>
        </section>""",
        create_parent=False,
    )
    tree.body.append(comments)
    return serialize(tree)


def add_comments_time_older(doc: str) -> str:
    """Comment <time> elements older than the article date."""
    tree = parse(doc)
    comments = lh.fragment_fromstring(
        """<section id="comments">
        <h2>Kommentare</h2>
        <div class="comment"><p class="meta">Anna, <time datetime="2023-05-10T10:22:00">10. Mai 2023</time></p>
        <p>Toller Artikel!</p></div>
        </section>""",
        create_parent=False,
    )
    tree.body.append(comments)
    return serialize(tree)


def add_footer_last_updated(doc: str) -> str:
    """Footer 'last updated' notice with a full date newer than the article."""
    tree = parse(doc)
    footer = lh.fragment_fromstring(
        '<footer><p>Zuletzt aktualisiert: 02.03.2024</p></footer>',
        create_parent=False,
    )
    tree.body.append(footer)
    return serialize(tree)


def add_comments_text_nocue(doc: str) -> str:
    """Comment dates in plain <p> elements without any cued class or id."""
    tree = parse(doc)
    comments = lh.fragment_fromstring(
        """<section id="comments">
        <h2>Kommentare</h2>
        <div><p>Anna schrieb am 03.01.2024 um 10:22</p>
        <p>Toller Artikel!</p></div>
        <div><p>Ben schrieb am 30.12.2023 um 08:15</p>
        <p>Danke für die Info.</p></div>
        </section>""",
        create_parent=False,
    )
    tree.body.append(comments)
    return serialize(tree)


def add_footer_full_date_top(doc: str) -> str:
    """Footer with a full date, moved before the article in DOM order."""
    tree = parse(doc)
    footer = lh.fragment_fromstring(
        '<footer><p>Stand: 02.03.2024</p></footer>',
        create_parent=False,
    )
    tree.body.insert(0, footer)
    return serialize(tree)


def add_nav_menu_dates(doc: str) -> str:
    """Navigation menu whose link text happens to contain date-like numbers."""
    tree = parse(doc)
    nav = lh.fragment_fromstring(
        """<nav><ul>
        <li><a href="/archiv/2024">Archiv 2024</a></li>
        <li><a href="/archiv/2023">Archiv 2023</a></li>
        </ul></nav>""",
        create_parent=False,
    )
    tree.body.insert(0, nav)
    return serialize(tree)


TRANSFORMS = {
    "footer_copyright_year": add_footer_copyright,
    "footer_copyright_range": add_footer_copyright_range,
    "footer_moved_top": add_footer_moved_top,
    "sidebar_hot_text": add_sidebar_hot_text,
    "sidebar_hot_time": add_sidebar_hot_time,
    "comments_text": add_comments_text,
    "comments_text_nocue": add_comments_text_nocue,
    "comments_time": add_comments_time,
    "comments_time_older": add_comments_time_older,
    "footer_last_updated": add_footer_last_updated,
    "footer_full_date_top": add_footer_full_date_top,
    "nav_menu_dates": add_nav_menu_dates,
}

MODES = {
    "default": {},
    "original": {"original_date": True},
    "fast": {"extensive_search": False},
}


def main() -> None:
    print(f"{'base':<16} {'transform':<26} {'mode':<10} {'result':<12} changed")
    print("-" * 76)
    for base_name, base_html in BASES.items():
        baselines = {
            mode: find_date(base_html, **kwargs) for mode, kwargs in MODES.items()
        }
        print(f"baselines for {base_name}: {baselines}")
        for name, func in TRANSFORMS.items():
            mutated = func(base_html)
            for mode, kwargs in MODES.items():
                result = find_date(mutated, **kwargs)
                changed = "YES" if result != baselines[mode] else ""
                print(
                    f"{base_name:<16} {name:<26} {mode:<10} {str(result):<12} {changed}"
                )
            print()
        print("=" * 76)


if __name__ == "__main__":
    main()
