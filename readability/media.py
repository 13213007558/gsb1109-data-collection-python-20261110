"""Media group extraction for article content.

A media group ties together one media occurrence (image, video or audio)
with its resource candidates, caption, alternative text and position in
the extracted article.  Nothing is downloaded and no candidate is
selected on behalf of a browser: lazy-loading attributes and <picture>
candidates are all preserved together with the attribute they came from,
and resources that only a browser can pick (srcset descriptors, media
queries) are reported as candidates instead of being resolved.
"""

import re
import urllib.parse


def is_video_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.hostname in {
        "youtube.com",
        "www.youtube.com",
        "vimeo.com",
        "www.vimeo.com",
    }


MEDIA_TAGS = ("img", "video", "audio", "iframe")

# Attributes that lazy-loading scripts use to stash the real resource URL.
LAZY_URL_ATTRS = (
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-original-src",
    "data-url",
    "data-image",
    "data-img",
    "data-hi-res-src",
)
LAZY_SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset")

DECORATIVE_FEATURES_RE = re.compile(r"icon|sprite|logo|avatar|emoji|smiley", re.I)
DECORATIVE_URL_RE = re.compile(
    r"icon|sprite|logo|avatar|emoji|smiley|blank|spacer|pixel|tracking"
    r"|transparent|placeholder|loading|1x1",
    re.I,
)
CAPTION_RE = re.compile(
    r"caption|credit|figcap|bildunterschrift|bildtext", re.I
)

MAX_CAPTION_LENGTH = 600


def _text(elem):
    return " ".join((elem.text_content() or "").split())


def _to_int(value):
    if not value:
        return None
    value = str(value).strip()
    if value.endswith("px"):
        value = value[:-2]
    try:
        return int(value)
    except ValueError:
        return None


def _split_srcset(value):
    entries = []
    for candidate in value.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        parts = candidate.split()
        descriptor = " ".join(parts[1:]) or None
        entries.append((parts[0], descriptor))
    return entries


class _SourceCollector:
    def __init__(self):
        self.sources = []
        self.saw_data_uri = False
        self._seen = set()

    def add(self, url, origin, descriptor=None, media=None, mime=None):
        if not url:
            return
        url = url.strip()
        if not url:
            return
        if url.startswith("data:"):
            # Inline data URIs are self-contained (usually placeholders);
            # there is no resource address to report.
            self.saw_data_uri = True
            return
        key = (url, origin, descriptor)
        if key in self._seen:
            return
        self._seen.add(key)
        source = {"url": url, "origin": origin}
        if descriptor:
            source["descriptor"] = descriptor
        if media:
            source["media"] = media
        if mime:
            source["type"] = mime
        self.sources.append(source)

    def add_srcset(self, value, origin, media=None, mime=None):
        if not value:
            return
        for url, descriptor in _split_srcset(value):
            self.add(url, origin, descriptor=descriptor, media=media, mime=mime)


def _collect_lazy_attributes(elem, collector):
    for attr in LAZY_URL_ATTRS:
        collector.add(elem.get(attr), attr)
    for attr in LAZY_SRCSET_ATTRS:
        collector.add_srcset(elem.get(attr), attr)


def _collect_image_sources(img, collector):
    collector.add(img.get("src"), "src")
    _collect_lazy_attributes(img, collector)
    collector.add_srcset(img.get("srcset"), "srcset")
    # HTML parsers may nest the <img> inside a <source> sibling, so look
    # for an enclosing <picture> instead of only the direct parent.
    picture = next(
        (ancestor for ancestor in img.iterancestors() if ancestor.tag == "picture"),
        None,
    )
    if picture is not None:
        for source in picture.findall(".//source"):
            media = source.get("media")
            mime = source.get("type")
            collector.add_srcset(
                source.get("srcset"), "source[srcset]", media=media, mime=mime
            )
            collector.add(source.get("src"), "source[src]", media=media, mime=mime)
            for attr in LAZY_SRCSET_ATTRS:
                collector.add_srcset(
                    source.get(attr), "source[%s]" % attr, media=media, mime=mime
                )


def _collect_av_sources(elem, collector):
    collector.add(elem.get("src"), "src")
    _collect_lazy_attributes(elem, collector)
    for source in elem.findall(".//source"):
        collector.add(source.get("src"), "source[src]", mime=source.get("type"))
        collector.add_srcset(
            source.get("srcset"),
            "source[srcset]",
            media=source.get("media"),
            mime=source.get("type"),
        )
    collector.add(elem.get("poster"), "poster")


def _is_caption_block(elem):
    if elem.tag == "figcaption":
        return True
    features = "{} {}".format(elem.get("class", ""), elem.get("id", ""))
    if not CAPTION_RE.search(features):
        return False
    if not _text(elem) or len(_text(elem)) > MAX_CAPTION_LENGTH:
        return False
    # A block that itself holds media is a container, not a caption.
    return not any(elem.findall(".//%s" % tag) for tag in MEDIA_TAGS)


def _find_caption(elem):
    for ancestor in (elem,) + tuple(elem.iterancestors()):
        if ancestor.tag == "figure":
            for figcaption in ancestor.findall(".//figcaption"):
                text = _text(figcaption)
                if text:
                    return text
            return None
        if ancestor.tag in ("article", "main", "body"):
            break
    node = elem
    for _ in range(3):
        for sibling in (node.getnext(), node.getprevious()):
            if sibling is not None and _is_caption_block(sibling):
                return _text(sibling)
        parent = node.getparent()
        if parent is None or parent.tag in ("article", "main", "body", "html"):
            break
        node = parent
    return None


def _find_alt(elem):
    for attr in ("alt", "aria-label", "title"):
        value = elem.get(attr)
        if value and value.strip():
            return value.strip()
    return None


def _is_decorative(elem, sources):
    features = "{} {}".format(elem.get("class", ""), elem.get("id", ""))
    if DECORATIVE_FEATURES_RE.search(features):
        return True
    if elem.get("role") == "presentation" or elem.get("aria-hidden") == "true":
        return True
    width = _to_int(elem.get("width"))
    height = _to_int(elem.get("height"))
    if width is not None and height is not None and width <= 2 and height <= 2:
        return True
    urls = [source["url"] for source in sources]
    if urls and all(DECORATIVE_URL_RE.search(url) for url in urls):
        return True
    return False


def iter_media_groups(article):
    """Yield media groups for an extracted article element, in document order.

    Each group is a dict with keys:

    - ``type``: "image", "video" or "audio"
    - ``sources``: list of ``{"url", "origin", ...}`` candidates; ``origin``
      names the attribute the URL came from (``src``, ``data-src``,
      ``srcset``, ``source[srcset]``, ``poster``, ...)
    - ``caption``: text of the associated figcaption/caption block, or None
    - ``alt``: alternative text of the media element, or None
    - ``position``: 0-based index of the group in the article
    - ``missing_reason``: None, or why no resource URL could be reported
    """
    position = 0
    for elem in article.iter():
        tag = elem.tag
        if not isinstance(tag, str) or tag not in MEDIA_TAGS:
            continue
        if tag == "iframe" and not is_video_url(elem.get("src") or ""):
            continue
        collector = _SourceCollector()
        if tag == "img":
            group_type = "image"
            _collect_image_sources(elem, collector)
        elif tag in ("video", "audio"):
            group_type = tag
            _collect_av_sources(elem, collector)
        else:
            group_type = "video"
            collector.add(elem.get("src"), "src")
        if _is_decorative(elem, collector.sources):
            continue
        group = {
            "type": group_type,
            "sources": collector.sources,
            "caption": _find_caption(elem),
            "alt": _find_alt(elem),
            "position": position,
            "missing_reason": None,
        }
        if not collector.sources:
            if collector.saw_data_uri:
                group["missing_reason"] = "only an inline data: URI is available"
            else:
                group["missing_reason"] = "no resource URL found on <%s>" % tag
        xpath = elem.get("x")
        if xpath:
            group["xpath"] = xpath
        position += 1
        yield group
