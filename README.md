[![PyPI version](https://img.shields.io/pypi/v/readability-lxml.svg)](https://pypi.python.org/pypi/readability-lxml)

# python-readability

Given an HTML document, extract and clean up the main body text and title.

This is a Python port of a Ruby port of [arc90's Readability project](https://web.archive.org/web/20130519040221/http://www.readability.com/).

## Installation

It's easy using `pip`, just run:

```bash
$ pip install readability-lxml
```

As an alternative, you may also use conda to install, just run:

```bash
$ conda install -c conda-forge readability-lxml
```

## Usage

```python
>>> import requests
>>> from readability import Document

>>> response = requests.get('http://example.com')
>>> doc = Document(response.content)
>>> print(doc.title())
Example Domain

>>> print(doc.summary())
<html><body><div><body id="readabilityBody">
<div>
    <h1>Example Domain</h1>
<p>This domain is established to be used for illustrative examples in documents. You may
use this domain in examples without prior coordination or asking for permission.</p>
    <p><a href="http://www.iana.org/domains/example">More information...</a></p>
</div>
</body>
</div></body></html>
```

The command-line interface accepts a local HTML file or a URL:

```bash
python -m readability -u https://example.com
```

Run the real-page extraction benchmark with the default minimum F1 of 0.95:

```bash
make benchmark
BENCHMARK_MIN_SCORE=0.97 make benchmark
```

In a reproducible comparison of ten extraction engines on 181 saved pages,
Readability 0.9 ranks second overall with F1 `0.975` and first on the 51 pages
outside Mozilla Readability's own fixture corpus with F1 `0.975`. Mozilla
Readability ranks first overall (F1 `0.986`), but 130 of the 181 fixtures come
from its test suite. The [quality evaluation](https://github.com/buriy/python-readability/tree/master/docs/quality)
includes the corpus, methodology, complete results, failures, and per-page
measurements.

## Stitching Paginated Articles

Articles split across several pages usually repeat the end of the previous
page at the top of the next one, so concatenating extracted pages duplicates
paragraphs (and splits a paragraph when the page break lands mid-sentence).
`readability.pagination.stitch_article()` accepts the pages *you already
downloaded*, in the page order *you* give, and stitches them offline. It never
opens a network connection.

```python
from readability.pagination import DownloadedPage, stitch_article

# Downloaded earlier by the caller; stitch_article() only reads these strings.
pages = [
    DownloadedPage(1, html_page_1, url="https://example.test/story?page=1"),
    DownloadedPage(2, html_page_2, url="https://example.test/story?page=2"),
    DownloadedPage(3, html_page_3, url="https://example.test/story?page=3"),
]
article = stitch_article(pages)
article.page_order          # (1, 2, 3) -- caller order is never reordered
article.reading_order()     # [(source_page, paragraph_text), ...]
article.merged_html         # one <div> with data-source-page on every paragraph
article.boundaries          # one evidence record per adjacent page pair
article.issues              # missing pages, conflicting titles, uncertain joins
article.unresolved_positions()
```

Each retained paragraph keeps its source page in `ArticleSegment.source_page`
(a tuple, e.g. `(1, 2)`, when a split paragraph was rebuilt from two pages).
Only boundary repetitions backed by textual evidence are removed; identical
quotations appearing inside the body stay untouched.

**Whole-paragraph overlap.** When page 2 opens with page 1's closing
paragraphs, the repeated copies are skipped and the boundary is recorded as
`overlap`:

```python
# page 1 ends:  [P1, QUOTE, P2]
# page 2 opens: [P2, P3]      page 3 opens: [P3, P4]
article = stitch_article([page1, page2, page3])
article.reading_order()
# [(1, P1), (1, QUOTE), (1, P2), (2, P3), (3, P4)]
[b["relation"] for b in article.boundaries]   # ['overlap', 'overlap']
article.issues                                # []
```

The same quotation appearing in the middle of both pages is preserved twice,
because it is not on a boundary.

**Half-paragraph continuation.** When a paragraph is cut by the page break,
the next page starts with the same opening fragment and then completes it. The
fragments are merged into one paragraph attributed to both pages instead of
producing a truncated duplicate:

```python
# page 1 ends with fragment P5[:52]; page 2 opens with the full P5, then P6
article = stitch_article([page1, page2])
article.boundaries[0]["relation"]             # 'continuation'
article.paragraphs[1].text == P5              # True
article.paragraphs[1].source_page             # (1, 2)
article.reading_order()
# [(1, P1), ((1, 2), P5), (2, P6)]
```

**Same text, different links.** Identical wording with different `href`
targets cannot be proven to be a boundary repeat, so both paragraphs are kept
and the join is reported as an unresolved position rather than guessed away:

```python
# page 1 ends:   <p><a href="/v1">Read the official registry entry…</a></p>
# page 2 opens:  <p><a href="/v2">Read the official registry entry…</a></p>
article = stitch_article([page1, page2])
article.boundaries[0]["relation"]             # 'uncertain'
article.unresolved_positions()
# [{'position': 2, 'between': (1, 2),
#   'reason': 'identical paragraph text points to different link targets;
#              it cannot be proven to be a boundary repeat'}]
article.plain_text().count("Read the official registry entry")   # 2
```

Gaps in an integer page sequence produce a `missing_page` issue, pages whose
titles disagree produce a `conflicting_title` issue, and any boundary without
overlap or continuation evidence produces an `uncertain_join` issue. In every
unresolved case the full text of both pages is retained.

## Security

`Document.summary()` removes common active content while extracting an article,
but readability-lxml is not a security boundary or a general-purpose HTML
sanitizer. Applications that render untrusted output must apply a dedicated
allowlist sanitizer and an appropriate Content Security Policy.

## Change Log
- 0.9
  - Expanded the declared Python range through 3.14, added `lxml` 6 support, and updated packaging metadata for Markdown documentation and current `cssselect` releases.
  - Added `python -m readability` execution for local files and URLs.
  - Fixed bytes input and encoding detection.
  - Fixed `get_clean_html()` before another API call has initialized the document.
  - Fixed shortened-title selection and CJK title length handling.
  - Fixed XPath annotations incorrectly affecting the ruthless-parser retry length.
  - Preserved inline links and formatting elements when converting misused `<div>` elements into paragraphs.
  - Removed inline `display: none`, `visibility: hidden`, HTML `hidden`, and `<noscript>` fallback content before scoring.
  - Preserved content containers whose class or ID also contains an unlikely-candidate term such as `sidebar`.
  - Preserved code blocks and semantic `<main>` or `<article>` containers during unlikely-candidate filtering.
  - Recovered editorial leads, heading preambles, split article segments, and substantial list-based articles.
  - Added offline `readability.pagination.stitch_article()` for caller-ordered downloaded pages: removes evidence-backed boundary paragraph overlaps, merges paragraphs split across page breaks, preserves mid-body repeated quotations, keeps provenance per paragraph, and reports missing pages, conflicting titles, and uncertain joins (including same text with different link targets) without any network requests.
  - Removed trailing linked calls to action without weakening general link-density filtering.
  - Restricted retained video iframes to exact HTTP(S) YouTube and Vimeo hosts, preventing lookalike-host and userinfo URL bypasses, and removed active `srcdoc` content.
  - Added 181 reproducible fixtures: 166 quality fixtures split into base, Dragnet, GitHub user-issue, and complete 130-page Mozilla Readability corpora, plus 15 manually curated real-page regression fixtures from `jcharum/lxml-readability`.
  - Replaced the unmaintained nose test runner with pytest in local, tox, and GitHub Actions workflows.
  - Updated development and release targets for portable module execution, PEP 517 builds, version synchronization, isolated artifact checks, and current-version uploads.
  - Improved the 166-page benchmark from precision 0.971, recall 0.884, and F1 0.926 in 0.8.4.1 to precision 0.991, recall 0.956, and F1 0.973.
  - Corrected the README usage examples.
  - Fixes GitHub issues [#14](https://github.com/buriy/python-readability/issues/14), [#108](https://github.com/buriy/python-readability/issues/108), [#119](https://github.com/buriy/python-readability/issues/119), [#130](https://github.com/buriy/python-readability/issues/130), [#143](https://github.com/buriy/python-readability/issues/143), [#146](https://github.com/buriy/python-readability/issues/146), [#153](https://github.com/buriy/python-readability/issues/153), [#158](https://github.com/buriy/python-readability/issues/158), [#159](https://github.com/buriy/python-readability/issues/159), [#163](https://github.com/buriy/python-readability/issues/163), [#170](https://github.com/buriy/python-readability/issues/170), [#176](https://github.com/buriy/python-readability/issues/176), [#182](https://github.com/buriy/python-readability/issues/182), [#194](https://github.com/buriy/python-readability/issues/194), and [#196](https://github.com/buriy/python-readability/issues/196).
- 0.8.4 Better CJK support, thanks @cdhigh
- 0.8.3.1 Support for python 3.8 - 3.13
- 0.8.3 We can now save all images via keep_all_images=True (default is to save 1 main image), thanks @botlabsDev
- 0.8.2 Added article author(s) (thanks @mattblaha)
- 0.8.1 Fixed processing of non-ascii HTMLs via regexps.
- 0.8 Replaced XHTML output with HTML5 output in summary() call.
- 0.7.1 Support for Python 3.7 . Fixed a slowdown when processing documents with lots of spaces.
- 0.7 Improved HTML5 tags handling. Fixed stripping unwanted HTML nodes (only first matching node was removed before).
- 0.6 Finally a release which supports Python versions 2.6, 2.7, 3.3 - 3.6
- 0.5 Preparing a release to support Python versions 2.6, 2.7, 3.3 and 3.4
- 0.4 Added Videos loading and allowed more images per paragraph
- 0.3 Added Document.encoding, positive\_keywords and negative\_keywords

## Licensing

This code is under [the Apache License 2.0](http://www.apache.org/licenses/LICENSE-2.0) license.

## Thanks to

- Latest [readability.js](https://github.com/MHordecki/readability-redux/blob/master/readability/readability.js)
- Ruby port by starrhorne and iterationlabs
- [Python port](https://github.com/gfxmonk/python-readability) by gfxmonk
- [Decruft effort](https://web.archive.org/web/20110214150709/https://www.minvolai.com/blog/decruft-arc90s-readability-in-python/) to move to lxml
- "BR to P" fix from readability.js which improves quality for smaller texts
- Github users contributions.
