import json
import unittest

from readability import Document
from readability.blocks import (
    blocks_to_html,
    extract_blocks,
    to_plain_text,
)


ARTICLE_TEXT = "Ordinary article sentence with enough useful context. " * 8


NESTED_LIST_ARTICLE = f"""
<html><head><title>Python Packaging Notes</title></head><body>
<article class="post">
<h1>Python Packaging Notes</h1>
<p>Use <code>python -m build</code> instead of the legacy
<code>setup.py sdist</code>, and read the
<a href="https://peps.python.org/pep-0517/">PEP 517</a> specification.
{ARTICLE_TEXT}</p>
<h2>Release steps</h2>
<ol start="2">
  <li>Bump the version and update <code>__version__</code>.
    <ul>
      <li>Run <code>pytest -q</code> with no failures.</li>
      <li>Check the changelog:
        <ol>
          <li>Add missing entries.</li>
          <li>Update the release date.</li>
        </ol>
      </li>
    </ul>
  </li>
  <li>Build both wheels with <code>pip wheel . -w dist/</code>.</li>
</ol>
<blockquote>
  <p>Never run <code>pip install</code> with
  <code>--break-system-packages</code> on a shared host.</p>
</blockquote>
<pre><code class="language-python">def greet(name):
    # keep indentation
    if name:
        message = f"hello {{name}}"

    return message
</code></pre>
<custom-widget>
  <p>Unknown structure carrying real article content.</p>
</custom-widget>
</article>
</body></html>
"""


def block_types(blocks):
    return [block["type"] for block in blocks]


def find_blocks(blocks, block_type):
    found = []
    for block in blocks:
        if block["type"] == block_type:
            found.append(block)
        found.extend(find_blocks(block.get("blocks", []), block_type))
        if block["type"] == "list":
            for item in block["items"]:
                found.extend(find_blocks(item.get("blocks", []), block_type))
    return found


class StructuredBlocksTest(unittest.TestCase):
    def setUp(self):
        self.doc = Document(NESTED_LIST_ARTICLE)
        self.blocks = self.doc.structured_blocks()

    def test_top_level_types_keep_reading_order(self):
        self.assertEqual(
            block_types(self.blocks),
            [
                "heading",
                "paragraph",
                "heading",
                "list",
                "quote",
                "code",
                "block",
            ],
        )

    def test_heading_levels_and_text(self):
        headings = [b for b in self.blocks if b["type"] == "heading"]
        self.assertEqual(
            [(h["level"], h["text"]) for h in headings],
            [(1, "Python Packaging Notes"), (2, "Release steps")],
        )

    def test_paragraph_inline_code_spans_have_valid_offsets(self):
        paragraph = self.blocks[1]
        code_spans = paragraph["inline"]["code"]
        self.assertEqual(
            [span["code"] for span in code_spans],
            ["python -m build", "setup.py sdist"],
        )
        for span in code_spans:
            self.assertEqual(
                paragraph["text"][span["start"]:span["end"]], span["code"]
            )

    def test_paragraph_keeps_links_with_offsets(self):
        paragraph = self.blocks[1]
        self.assertEqual(len(paragraph["inline"]["links"]), 1)
        link = paragraph["inline"]["links"][0]
        self.assertEqual(link["href"], "https://peps.python.org/pep-0517/")
        self.assertEqual(link["text"], "PEP 517")
        self.assertEqual(
            paragraph["text"][link["start"]:link["end"]], "PEP 517"
        )

    def test_ordered_list_keeps_start_and_explicit_numbering(self):
        outer = self.blocks[3]
        self.assertEqual(outer["type"], "list")
        self.assertTrue(outer["ordered"])
        self.assertEqual(outer["start"], 2)
        self.assertEqual(
            [item["number"] for item in outer["items"]], [2, 3]
        )

    def test_nested_lists_keep_hierarchy_and_inline_code(self):
        outer = self.blocks[3]
        first_item = outer["items"][0]
        self.assertEqual(
            first_item["text"], "Bump the version and update __version__."
        )
        code_span = first_item["inline"]["code"][0]
        self.assertEqual(code_span["code"], "__version__")
        self.assertEqual(
            first_item["text"][code_span["start"]:code_span["end"]],
            "__version__",
        )

        inner_ul = first_item["blocks"][0]
        self.assertFalse(inner_ul["ordered"])
        self.assertEqual(
            [item["text"] for item in inner_ul["items"]],
            [
                "Run pytest -q with no failures.",
                "Check the changelog:",
            ],
        )
        nested_ol = inner_ul["items"][1]["blocks"][0]
        self.assertTrue(nested_ol["ordered"])
        self.assertEqual(
            [item["number"] for item in nested_ol["items"]], [1, 2]
        )
        self.assertEqual(
            [item["text"] for item in nested_ol["items"]],
            ["Add missing entries.", "Update the release date."],
        )

    def test_quote_is_nested_block_with_inline_code(self):
        quote = self.blocks[4]
        self.assertEqual(quote["type"], "quote")
        self.assertEqual(len(quote["blocks"]), 1)
        inner = quote["blocks"][0]
        self.assertEqual(inner["type"], "paragraph")
        self.assertIn("shared host", inner["text"])
        self.assertEqual(
            [span["code"] for span in inner["inline"]["code"]],
            ["pip install", "--break-system-packages"],
        )

    def test_code_block_preserves_whitespace_verbatim(self):
        code = self.blocks[5]
        self.assertEqual(code["type"], "code")
        self.assertEqual(code["language"], "python")
        expected = (
            'def greet(name):\n'
            '    # keep indentation\n'
            '    if name:\n'
            '        message = f"hello {name}"\n'
            '\n'
            '    return message\n'
        )
        self.assertEqual(code["code"], expected)

    def test_unknown_content_bearing_structure_is_kept(self):
        unknown = self.blocks[6]
        self.assertEqual(unknown["type"], "block")
        self.assertEqual(unknown["tag"], "custom-widget")
        self.assertEqual(
            unknown["blocks"][0]["text"],
            "Unknown structure carrying real article content.",
        )

    def test_every_block_carries_a_location(self):
        def check(blocks):
            for block in blocks:
                self.assertIn("location", block)
                self.assertIn("tag", block["location"])
                if block["type"] == "list":
                    for item in block["items"]:
                        check([item])
                        check(item["blocks"])
                check(block.get("blocks", []))

        check(self.blocks)
        first = self.blocks[0]
        self.assertEqual(first["location"]["tag"], "h1")
        self.assertTrue(first["location"]["xpath"].startswith("/"))

    def test_locations_point_to_original_document_in_xpath_mode(self):
        blocks = Document(NESTED_LIST_ARTICLE, xpath=True).structured_blocks()
        paragraph_location = blocks[1]["location"]
        self.assertEqual(paragraph_location["tag"], "p")
        self.assertEqual(
            paragraph_location["xpath"], "/html/body/article/p"
        )

    def test_blocks_are_json_serializable(self):
        json.dumps(self.blocks)

    def test_plain_text_renders_meaning_equivalent_body(self):
        text = to_plain_text(self.blocks)
        self.assertIn("# Python Packaging Notes", text)
        self.assertIn(
            "Use `python -m build` instead of the legacy `setup.py sdist`",
            text,
        )
        self.assertIn("2. Bump the version and update `__version__`.", text)
        self.assertIn("3. Build both wheels", text)
        self.assertIn("  - Run `pytest -q` with no failures.", text)
        self.assertIn("    1. Add missing entries.", text)
        self.assertIn("    2. Update the release date.", text)
        self.assertIn("> Never run `pip install`", text)
        self.assertIn('        message = f"hello {name}"', text)
        self.assertIn("\n\n    return message\n", text)
        self.assertIn("Unknown structure carrying real article content.", text)

    def test_plain_text_heading_order_matches_blocks(self):
        text = to_plain_text(self.blocks)
        self.assertLess(text.index("# Python Packaging Notes"), text.index("## Release steps"))
        self.assertLess(text.index("## Release steps"), text.index("2. Bump"))

    def test_blocks_to_html_round_trips_key_content(self):
        rendered = blocks_to_html(self.blocks)
        self.assertIn("<h1>Python Packaging Notes</h1>", rendered)
        self.assertIn('<ol start="2">', rendered)
        self.assertIn("<ul><li>", rendered)
        self.assertIn("<blockquote><p>", rendered)
        self.assertIn('class="language-python"', rendered)
        self.assertIn("<custom-widget>", rendered)

    def test_summary_still_available_alongside_blocks(self):
        doc = Document(NESTED_LIST_ARTICLE)
        summary = doc.summary(html_partial=True)
        blocks = Document(NESTED_LIST_ARTICLE).structured_blocks()
        self.assertIn("<h1>Python Packaging Notes</h1>", summary)
        self.assertIn("<ol", summary)
        self.assertIn("<code>__version__</code>", summary)
        self.assertIn("custom-widget", summary)
        self.assertEqual(
            [block["type"] for block in blocks][0], "heading"
        )

    def test_inline_code_inside_pre_with_spans_keeps_everything(self):
        sample = f"""
        <html><body><article>
            <pre><code>value = 1
                <span class="comment"># keep this comment</span>
                print(value)
            </code></pre>
            <p>{ARTICLE_TEXT}</p>
        </article></body></html>
        """
        blocks = Document(sample).structured_blocks()
        code = find_blocks(blocks, "code")[0]
        self.assertIn("# keep this comment", code["code"])
        self.assertIn("                print(value)", code["code"])


class DirectExtractionTest(unittest.TestCase):
    def test_table_and_definition_list_blocks(self):
        from lxml.html import fragment_fromstring

        root = fragment_fromstring(
            "<div>"
            "<dl><dt>Term</dt><dd>Definition with detail.</dd></dl>"
            "<table><tr><th>A</th><th>B</th></tr>"
            "<tr><td>1</td><td>2</td></tr></table>"
            "<hr/>"
            '<img src="/x.png" alt="Diagram"/>'
            "</div>"
        )
        blocks = extract_blocks(root)
        self.assertEqual(
            block_types(blocks),
            ["definition_list", "table", "thematic_break", "image"],
        )
        definition = blocks[0]
        self.assertEqual(definition["items"][0]["term"], "Term")
        self.assertEqual(
            definition["items"][0]["definitions"][0][0]["text"],
            "Definition with detail.",
        )
        table = blocks[1]
        self.assertEqual(
            [[cell["text"] for cell in row] for row in table["rows"]],
            [["A", "B"], ["1", "2"]],
        )
        self.assertEqual(blocks[3]["src"], "/x.png")
        self.assertEqual(blocks[3]["alt"], "Diagram")

    def test_loose_text_become_paragraphs_without_dropping_content(self):
        from lxml.html import fragment_fromstring

        root = fragment_fromstring(
            "<div>Loose text before.<p>Real paragraph content.</p>tail.</div>"
        )
        blocks = extract_blocks(root)
        self.assertEqual(
            block_types(blocks), ["paragraph", "paragraph", "paragraph"]
        )
        self.assertEqual(blocks[0]["text"], "Loose text before.")
        self.assertEqual(blocks[1]["text"], "Real paragraph content.")
        self.assertEqual(blocks[2]["text"], "tail.")


if __name__ == "__main__":
    unittest.main()
