"""Structured content blocks for knowledge-base ingestion.

The classic :meth:`readability.Document.summary` output is one HTML string.
This module turns the same extracted article tree into an ordered list of
typed blocks (headings, paragraphs, quotes, code, lists, tables, ...) so that
downstream consumers can keep reading order, nesting, code whitespace, list
numbering, links, and source-node locations.

Block dictionaries only contain JSON-serializable values.
"""

import re

from .cleaners import clean_attributes


HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
LIST_TAGS = {"ul", "ol"}
INLINE_CODE_TAGS = {"code", "kbd", "samp", "tt"}
PREFORMATTED_TAGS = {"pre"}
BLOCKQUOTE_TAGS = {"blockquote"}
DEFINITION_TAGS = {"dl"}
TABLE_TAGS = {"table"}
GROUP_TAGS = {
    "div",
    "section",
    "article",
    "main",
    "header",
    "footer",
    "aside",
    "body",
    "html",
}
IMAGE_TAGS = {"img"}
VOID_SKIP_TAGS = {"br", "input", "embed", "area", "base", "col", "link",
                  "meta", "param", "source", "track", "wbr"}

# Tags that always establish a block-level context. Inline code tags are
# deliberately excluded: an unknown inline element that wraps <code> must stay
# inline. Anything else that carries text is kept as an unknown block (when it
# contains block children) or folded into inline text, so unknown structures
# with content are never dropped.
KNOWN_BLOCK_TAGS = (
    HEADING_TAGS
    | LIST_TAGS
    | PREFORMATTED_TAGS
    | BLOCKQUOTE_TAGS
    | DEFINITION_TAGS
    | TABLE_TAGS
    | GROUP_TAGS
    | IMAGE_TAGS
    | {"p", "li", "dt", "dd", "tr", "th", "td", "hr", "figure", "figcaption",
       "details", "summary"}
)


def _normalize_text(value):
    """Collapse whitespace in ordinary prose; never used on code text."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value)


def _new_inline_context():
    return {"parts": [], "links": [], "code_spans": []}


def _context_length(ctx):
    return sum(len(part) for part in ctx["parts"])


def _append_inline_child(child, ctx):
    """Append one inline child, recording link and code spans."""
    if not isinstance(child.tag, str):
        _append_tail(child, ctx)
        return
    tag = child.tag
    if tag == "a":
        start = _context_length(ctx)
        _render_inline(child, ctx)
        end = _context_length(ctx)
        href = child.get("href")
        if href and end > start:
            ctx["links"].append(
                {"href": href, "start": start, "end": end}
            )
    elif tag in INLINE_CODE_TAGS:
        # text_content() preserves every newline and indentation level.
        code_text = child.text_content()
        start = _context_length(ctx)
        ctx["parts"].append(code_text)
        end = _context_length(ctx)
        if end > start:
            ctx["code_spans"].append(
                {"code": code_text, "start": start, "end": end}
            )
    elif tag == "br":
        ctx["parts"].append(" ")
    elif tag in IMAGE_TAGS:
        alt = child.get("alt")
        if alt:
            ctx["parts"].append(_normalize_text(alt))
    else:
        _render_inline(child, ctx)
    _append_tail(child, ctx)


def _render_inline(elem, ctx):
    """Render an inline subtree, keeping code text verbatim."""
    if elem.text:
        ctx["parts"].append(_normalize_text(elem.text))
    for child in elem:
        _append_inline_child(child, ctx)


def _append_tail(node, ctx):
    if node.tail:
        ctx["parts"].append(_normalize_text(node.tail))


def _finish_inline(ctx):
    joined = "".join(ctx["parts"])
    stripped = joined.strip()
    shift = len(joined) - len(joined.lstrip())

    def adjust(span):
        start = max(span["start"] - shift, 0)
        end = max(span["end"] - shift, 0)
        return start, end

    links = []
    for span in ctx["links"]:
        start, end = adjust(span)
        if end > start:
            links.append(
                {"href": span["href"], "text": stripped[start:end],
                 "start": start, "end": end}
            )
    code_spans = []
    for span in ctx["code_spans"]:
        start, end = adjust(span)
        if end > start:
            code_spans.append(
                {"code": stripped[start:end], "start": start, "end": end}
            )
    return stripped, {"links": links, "code": code_spans}


def _render_inline_text(elem):
    ctx = _new_inline_context()
    _render_inline(elem, ctx)
    return _finish_inline(ctx)


class StructuredExtractor:
    """Walks a sanitized article tree and produces structured blocks."""

    def __init__(self, source_tree=None):
        self.source_tree = source_tree

    def extract(self, root):
        blocks = []
        loose = _new_inline_context()
        self._walk(root, blocks, loose)
        self._flush_loose(blocks, loose, root)
        return blocks

    def _location(self, elem):
        location = {"tag": elem.tag}
        elem_id = elem.get("id")
        if elem_id:
            location["id"] = elem_id
        source_xpath = elem.get("x")
        if source_xpath:
            location["xpath"] = source_xpath
        elif self.source_tree is not None:
            try:
                location["xpath"] = self.source_tree.getpath(elem)
            except Exception:
                pass
        return location

    def _flush_loose(self, blocks, loose, context_elem):
        text, inline = _finish_inline(loose)
        if text:
            block = {
                "type": "paragraph",
                "text": text,
                "inline": inline,
                "location": self._location(context_elem),
            }
            blocks.append(block)
        loose["parts"].clear()
        loose["links"].clear()
        loose["code_spans"].clear()

    def _walk(self, elem, blocks, loose):
        if elem.text:
            loose["parts"].append(_normalize_text(elem.text))
        for child in elem:
            if not isinstance(child.tag, str):
                _append_tail(child, loose)
                continue
            tag = child.tag
            if tag in HEADING_TAGS:
                self._flush_loose(blocks, loose, elem)
                blocks.append(self._build_heading(child))
            elif tag == "p":
                self._flush_loose(blocks, loose, elem)
                blocks.append(self._build_paragraph(child))
            elif tag in PREFORMATTED_TAGS:
                self._flush_loose(blocks, loose, elem)
                blocks.append(self._build_code(child))
            elif tag in BLOCKQUOTE_TAGS:
                self._flush_loose(blocks, loose, elem)
                blocks.append(self._build_quote(child))
            elif tag in LIST_TAGS:
                self._flush_loose(blocks, loose, elem)
                blocks.append(self._build_list(child))
            elif tag in DEFINITION_TAGS:
                self._flush_loose(blocks, loose, elem)
                blocks.append(self._build_definition_list(child))
            elif tag in TABLE_TAGS:
                self._flush_loose(blocks, loose, elem)
                blocks.append(self._build_table(child))
            elif tag == "hr":
                self._flush_loose(blocks, loose, elem)
                blocks.append(
                    {"type": "thematic_break",
                     "location": self._location(child)}
                )
            elif tag in IMAGE_TAGS:
                self._flush_loose(blocks, loose, elem)
                blocks.append(self._build_image(child))
            elif tag in GROUP_TAGS:
                # Generic containers do not carry meaning of their own;
                # descend transparently while preserving reading order.
                self._walk(child, blocks, loose)
            elif self._has_block_descendant(child):
                self._flush_loose(blocks, loose, elem)
                blocks.append(self._build_unknown_block(child))
            elif tag in VOID_SKIP_TAGS:
                pass
            else:
                # Unknown inline element: keep its text instead of dropping it.
                _append_inline_child(child, loose)
                continue
            _append_tail(child, loose)

    def _has_block_descendant(self, elem):
        for descendant in elem.iterdescendants():
            if isinstance(descendant.tag, str) and (
                descendant.tag in KNOWN_BLOCK_TAGS
            ):
                return True
        return False

    def _build_heading(self, elem):
        text, inline = _render_inline_text(elem)
        return {
            "type": "heading",
            "level": int(elem.tag[1]),
            "text": text,
            "inline": inline,
            "location": self._location(elem),
        }

    def _build_paragraph(self, elem):
        text, inline = _render_inline_text(elem)
        return {
            "type": "paragraph",
            "text": text,
            "inline": inline,
            "location": self._location(elem),
        }

    def _build_code(self, elem):
        code_node = elem.find("code")
        target = code_node if code_node is not None else elem
        # Verbatim text: indentation, blank lines and inner spacing are part of
        # the program and must survive untouched.
        code_text = target.text_content()
        language = None
        class_attr = "{} {}".format(
            target.get("class") or "", elem.get("class") or ""
        )
        match = re.search(
            r"(?:language|lang)-([A-Za-z0-9_+#.\-]+)", class_attr
        )
        if match:
            language = match.group(1)
        _, inline = _render_inline_text(target)
        block = {
            "type": "code",
            "language": language,
            "code": code_text,
            "inline": inline,
            "location": self._location(elem),
        }
        return block

    def _build_quote(self, elem):
        child_blocks = []
        loose = _new_inline_context()
        self._walk(elem, child_blocks, loose)
        self._flush_loose(child_blocks, loose, elem)
        block = {
            "type": "quote",
            "blocks": child_blocks,
            "location": self._location(elem),
        }
        cite = elem.get("cite")
        if cite:
            block["cite"] = cite
        return block

    def _build_list(self, elem):
        ordered = elem.tag == "ol"
        start_attr = elem.get("start")
        try:
            start = int(start_attr) if start_attr is not None else 1
        except ValueError:
            start = 1
        li_children = [
            child
            for child in elem
            if isinstance(child.tag, str) and child.tag == "li"
        ]
        if not li_children:
            # Misused list element (only inline text or a bare nested list):
            # treat it as a generic container so no content is dropped.
            return self._build_unknown_block(elem)
        items = []
        position = 0
        leading_blocks = []
        last_item = None
        for child in elem:
            if not isinstance(child.tag, str):
                continue
            if child.tag == "li":
                number = start + position if ordered else None
                last_item = self._build_list_item(child, number)
                items.append(last_item)
                position += 1
            else:
                stray = self._build_stray_child(child)
                if stray is None:
                    continue
                if last_item is None:
                    leading_blocks.append(stray)
                else:
                    last_item["blocks"].append(stray)
        if leading_blocks:
            items[0]["blocks"][:0] = leading_blocks
        block = {
            "type": "list",
            "ordered": ordered,
            "start": start if ordered else None,
            "items": items,
            "location": self._location(elem),
        }
        return block

    def _build_stray_child(self, child):
        """Render a non-li element found directly inside a list."""
        if child.tag in LIST_TAGS:
            return self._build_list(child)
        if child.tag in PREFORMATTED_TAGS:
            return self._build_code(child)
        if child.tag in BLOCKQUOTE_TAGS:
            return self._build_quote(child)
        if child.tag in TABLE_TAGS:
            return self._build_table(child)
        return self._build_unknown_block(child)

    def _build_list_item(self, elem, number):
        text_parts = []
        inline_contexts = []
        child_blocks = []
        loose = _new_inline_context()

        def stash_text():
            text, inline = _finish_inline(loose)
            if text:
                text_parts.append(text)
                inline_contexts.append(inline)
            loose["parts"].clear()
            loose["links"].clear()
            loose["code_spans"].clear()

        if elem.text:
            loose["parts"].append(_normalize_text(elem.text))
        for child in elem:
            if not isinstance(child.tag, str):
                _append_tail(child, loose)
                continue
            tag = child.tag
            if tag in LIST_TAGS:
                stash_text()
                child_blocks.append(self._build_list(child))
            elif tag in PREFORMATTED_TAGS:
                stash_text()
                child_blocks.append(self._build_code(child))
            elif tag in BLOCKQUOTE_TAGS:
                stash_text()
                child_blocks.append(self._build_quote(child))
            elif tag in TABLE_TAGS:
                stash_text()
                child_blocks.append(self._build_table(child))
            elif tag == "p":
                stash_text()
                child_blocks.append(self._build_paragraph(child))
            elif tag in GROUP_TAGS:
                if self._has_block_descendant(child):
                    stash_text()
                    child_blocks.append(self._build_unknown_block(child))
                else:
                    _render_inline(child, loose)
                    continue
            elif self._has_block_descendant(child):
                stash_text()
                child_blocks.append(self._build_unknown_block(child))
            else:
                _append_inline_child(child, loose)
                continue
            _append_tail(child, loose)
        stash_text()

        text = " ".join(text_parts)
        merged = {"links": [], "code": []}
        offset = 0
        for part, inline in zip(text_parts, inline_contexts):
            for key in merged:
                for span in inline[key]:
                    new_span = dict(span)
                    new_span["start"] += offset
                    new_span["end"] += offset
                    merged[key].append(new_span)
            offset += len(part) + 1
        block = {
            "type": "list_item",
            "text": text,
            "inline": merged,
            "blocks": child_blocks,
            "location": self._location(elem),
        }
        if number is not None:
            block["number"] = number
        return block

    def _build_definition_list(self, elem):
        items = []
        current = None
        for child in elem:
            if not isinstance(child.tag, str):
                continue
            if child.tag == "dt":
                text, inline = _render_inline_text(child)
                current = {
                    "term": text,
                    "inline": inline,
                    "definitions": [],
                }
                items.append(current)
            elif child.tag == "dd":
                if current is None:
                    current = {
                        "term": "",
                        "inline": {"links": [], "code": []},
                        "definitions": [],
                    }
                    items.append(current)
                definition_blocks = []
                loose = _new_inline_context()
                self._walk(child, definition_blocks, loose)
                self._flush_loose(definition_blocks, loose, child)
                current["definitions"].append(definition_blocks)
        return {
            "type": "definition_list",
            "items": items,
            "location": self._location(elem),
        }

    def _build_table(self, elem):
        rows = []
        for row in elem.xpath(".//tr"):
            cells = []
            for cell in row.xpath("./th | ./td"):
                text, inline = _render_inline_text(cell)
                cells.append(
                    {
                        "tag": cell.tag,
                        "text": text,
                        "inline": inline,
                    }
                )
            if cells:
                rows.append(cells)
        return {
            "type": "table",
            "rows": rows,
            "location": self._location(elem),
        }

    def _build_image(self, elem):
        block = {
            "type": "image",
            "src": elem.get("src"),
            "alt": elem.get("alt") or "",
            "location": self._location(elem),
        }
        return block

    def _build_unknown_block(self, elem):
        """Keep unknown but content-bearing structures with their nesting."""
        child_blocks = []
        loose = _new_inline_context()
        self._walk(elem, child_blocks, loose)
        self._flush_loose(child_blocks, loose, elem)
        return {
            "type": "block",
            "tag": elem.tag,
            "blocks": child_blocks,
            "location": self._location(elem),
        }


def extract_blocks(root, source_tree=None):
    """Convert a parsed article element into a list of structured blocks."""
    return StructuredExtractor(source_tree=source_tree).extract(root)


def _inline_with_code_marks(block):
    text = block.get("text", "")
    for span in reversed(block.get("inline", {}).get("code", [])):
        start, end = span["start"], span["end"]
        text = "{}`{}`{}".format(text[:start], text[start:end], text[end:])
    return text


def _render_block(block, indent=0):
    block_type = block["type"]
    pad = "  " * indent
    lines = []

    if block_type == "heading":
        lines.append(
            "{}{} {}".format(pad, "#" * block["level"], block["text"])
        )
    elif block_type == "paragraph":
        lines.append(pad + _inline_with_code_marks(block))
    elif block_type == "code":
        fence = "```" + (block.get("language") or "")
        lines.append(pad + fence)
        # The stored code stays verbatim; only drop the single final newline
        # produced by the closing tag when rendering the fence.
        rendered_code = block["code"]
        if rendered_code.endswith("\n"):
            rendered_code = rendered_code[:-1]
        for line in rendered_code.split("\n"):
            lines.append(pad + line)
        lines.append(pad + "```")
    elif block_type == "quote":
        inner = []
        for child in block["blocks"]:
            inner.extend(_render_block(child, 0))
        for line in inner:
            lines.append("{}> {}".format(pad, line) if line else pad + ">")
    elif block_type == "list":
        lines.extend(_render_list(block, indent))
    elif block_type == "definition_list":
        for item in block["items"]:
            lines.append("{}{}".format(pad, item["term"]))
            for definitions in item["definitions"]:
                for definition in definitions:
                    for line in _render_block(definition, indent + 1):
                        lines.append(line)
    elif block_type == "table":
        lines.extend(_render_table(block, pad))
    elif block_type == "image":
        alt = block.get("alt") or ""
        src = block.get("src") or ""
        lines.append("{}![{}]({})".format(pad, alt, src))
    elif block_type == "thematic_break":
        lines.append(pad + "---")
    elif block_type == "list_item":
        lines.append(pad + _inline_with_code_marks(block))
        for child in block["blocks"]:
            lines.extend(_render_block(child, indent + 1))
    elif block_type == "block":
        for child in block["blocks"]:
            lines.extend(_render_block(child, indent))
    return lines


def _render_list(block, indent):
    pad = "  " * indent
    lines = []
    for index, item in enumerate(block["items"]):
        if block["ordered"]:
            marker = "{}. ".format(item.get("number", index + 1))
        else:
            marker = "- "
        lines.append(pad + marker + _inline_with_code_marks(item))
        for child in item["blocks"]:
            lines.extend(_render_block(child, indent + 1))
    return lines


def _render_table(block, pad):
    lines = []
    rows = block["rows"]
    if not rows:
        return lines
    header = all(cell["tag"] == "th" for cell in rows[0])
    for row_index, row in enumerate(rows):
        lines.append(
            pad + "| " + " | ".join(cell["text"] for cell in row) + " |"
        )
        if header and row_index == 0:
            lines.append(
                pad
                + "| "
                + " | ".join("---" for _ in row)
                + " |"
            )
    return lines


def to_plain_text(blocks):
    """Render structured blocks back into meaning-equivalent plain text."""
    rendered = [
        "\n".join(_render_block(block, 0))
        for block in blocks
    ]
    return "\n\n".join(chunk for chunk in rendered if chunk.strip())


def blocks_to_html(blocks):
    """Serialize structured blocks back into clean HTML (best effort)."""
    return clean_attributes(_render_html(blocks))


def _render_html(blocks):
    chunks = []
    for block in blocks:
        block_type = block["type"]
        if block_type == "heading":
            level = block["level"]
            chunks.append(
                "<h{0}>{1}</h{0}>".format(level, block["text"])
            )
        elif block_type == "paragraph":
            chunks.append("<p>{}</p>".format(block["text"]))
        elif block_type == "code":
            language = block.get("language")
            class_attr = (
                ' class="language-{}"'.format(language) if language else ""
            )
            chunks.append(
                "<pre><code{}>{}</code></pre>".format(
                    class_attr, block["code"]
                )
            )
        elif block_type == "quote":
            chunks.append(
                "<blockquote>{}</blockquote>".format(
                    _render_html(block["blocks"])
                )
            )
        elif block_type == "list":
            tag = "ol" if block["ordered"] else "ul"
            start_attr = (
                ' start="{}"'.format(block["start"])
                if block["ordered"] and block["start"] not in (None, 1)
                else ""
            )
            items = "".join(
                "<li>{}{}</li>".format(
                    item["text"], _render_html(item["blocks"])
                )
                for item in block["items"]
            )
            chunks.append("<{0}{1}>{2}</{0}>".format(tag, start_attr, items))
        elif block_type == "table":
            rows = "".join(
                "<tr>{}</tr>".format(
                    "".join(
                        "<{0}>{1}</{0}>".format(cell["tag"], cell["text"])
                        for cell in row
                    )
                )
                for row in block["rows"]
            )
            chunks.append("<table>{}</table>".format(rows))
        elif block_type == "definition_list":
            entries = []
            for item in block["items"]:
                definitions = "".join(
                    "<dd>{}</dd>".format(_render_html(definition))
                    for definition in item["definitions"]
                )
                entries.append(
                    "<dt>{}</dt>{}".format(item["term"], definitions)
                )
            chunks.append("<dl>{}</dl>".format("".join(entries)))
        elif block_type == "image":
            chunks.append(
                '<img src="{}" alt="{}"/>'.format(
                    block.get("src") or "", block.get("alt") or ""
                )
            )
        elif block_type == "thematic_break":
            chunks.append("<hr/>")
        elif block_type == "block":
            chunks.append(
                "<{0}>{1}</{0}>".format(
                    block["tag"], _render_html(block["blocks"])
                )
            )
    return "".join(chunks)
