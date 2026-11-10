import unittest

import lxml.html

from readability import Document


ARTICLE_TEXT = "Physics article explaining particle positions clearly. " * 8


class TestKeepFormulas(unittest.TestCase):
    def test_inline_math_with_subscript_preserved_in_place(self):
        sample = f"""
        <html><body><article>
            <p>{ARTICLE_TEXT}</p>
            <p>The position <math><msub><mi>x</mi><mi>i</mi></msub></math>
            of each particle is tracked over time in the experiment.</p>
        </article></body></html>
        """

        summary = Document(sample, keep_formulas=True).summary()

        self.assertIn("<math", summary)
        self.assertIn("<msub>", summary)
        self.assertIn('data-readability-formula="inline"', summary)
        self.assertIn("The position", summary)
        self.assertIn("of each particle is tracked", summary)

    def test_block_math_with_fraction_preserved(self):
        sample = f"""
        <html><body><article>
            <p>{ARTICLE_TEXT}</p>
            <div><math display="block"><mfrac>
                <mi>a</mi><mi>b</mi>
            </mfrac></math></div>
            <p>{ARTICLE_TEXT}</p>
        </article></body></html>
        """

        summary = Document(sample, keep_formulas=True).summary()

        self.assertIn("<mfrac>", summary)
        self.assertIn('data-readability-formula="block"', summary)

    def test_alttext_and_annotation_counted_once(self):
        sample = f"""
        <html><body><article>
            <p>{ARTICLE_TEXT}</p>
            <p>The energy
            <math alttext="E = mc^2"><semantics>
                <mrow><mi>E</mi><mo>=</mo><mi>m</mi>
                <msup><mi>c</mi><mn>2</mn></msup></mrow>
                <annotation encoding="application/x-tex">E = mc^2</annotation>
            </semantics></math>
            is famous.</p>
        </article></body></html>
        """

        summary = Document(sample, keep_formulas=True).summary()

        self.assertIn('data-alttext="E = mc^2"', summary)
        self.assertNotIn("<annotation", summary)
        text = lxml.html.document_fromstring(summary).text_content()
        self.assertNotIn("mc^2", text)
        self.assertIn("is famous.", summary)

    def test_hidden_mathml_and_fallback_image_deduplicated(self):
        sample = f"""
        <html><body><article>
            <p>{ARTICLE_TEXT}</p>
            <p>The solution
            <span class="mwe-math-element"><span class="mwe-math-mathml-inline"
                style="display: none;"><math alttext="x=2">
                <mi>x</mi><mo>=</mo><mn>2</mn></math></span><img
                class="mwe-math-fallback-image-inline" alt="x=2"
                src="math.png"></span>
            holds for every case.</p>
        </article></body></html>
        """

        summary = Document(sample, keep_formulas=True).summary()

        self.assertIn("<math", summary)
        self.assertIn('data-alttext="x=2"', summary)
        self.assertNotIn("mwe-math-fallback-image-inline", summary)
        self.assertNotIn("math.png", summary)
        self.assertIn("holds for every case.", summary)

    def test_unmatched_fallback_keeps_both_with_uncertainty_marker(self):
        sample = f"""
        <html><body><article>
            <p>{ARTICLE_TEXT}</p>
            <p>The result <math><mi>y</mi></math> follows.<span
                class="math-fallback">see the figure notes</span></p>
        </article></body></html>
        """

        summary = Document(sample, keep_formulas=True).summary()

        self.assertIn("<math", summary)
        self.assertIn("see the figure notes", summary)
        self.assertIn('data-readability-formula-uncertain="true"', summary)

    def test_unmatched_fallback_image_keeps_both(self):
        sample = f"""
        <html><body><article>
            <p>{ARTICLE_TEXT}</p>
            <p>The value <span class="mwe-math-element"><math alttext="x=2">
                <mi>x</mi><mo>=</mo><mn>2</mn></math><img
                class="mwe-math-fallback-image-inline" alt="y=3"
                src="other.png"></span> is used.</p>
        </article></body></html>
        """

        summary = Document(sample, keep_formulas=True).summary()

        self.assertIn("<math", summary)
        self.assertIn("other.png", summary)
        self.assertIn('data-readability-formula-uncertain="true"', summary)

    def test_plain_variables_in_prose_not_removed(self):
        sample = f"""
        <html><body><article>
            <p>{ARTICLE_TEXT}</p>
            <p>Here x and y are ordinary variables mentioned in prose, and the
            span <span>x</span> must remain untouched by formula handling.</p>
            <p>The value <math><mi>x</mi></math> is discussed
            <span class="note">in the next section</span> carefully.</p>
        </article></body></html>
        """

        summary = Document(sample, keep_formulas=True).summary()

        self.assertIn("ordinary variables", summary)
        self.assertIn("<span>x</span>", summary)
        self.assertIn("in the next section", summary)
        self.assertNotIn("data-readability-formula-uncertain", summary)

    def test_default_mode_leaves_math_untouched(self):
        sample = f"""
        <html><body><article>
            <p>{ARTICLE_TEXT}</p>
            <p>The energy
            <math alttext="E = mc^2"><semantics>
                <mrow><mi>E</mi><mo>=</mo><mi>m</mi></mrow>
                <annotation encoding="application/x-tex">E = mc^2</annotation>
            </semantics></math>
            is famous.</p>
        </article></body></html>
        """

        summary = Document(sample).summary()

        self.assertIn("<annotation", summary)
        self.assertNotIn("data-readability-formula", summary)
        self.assertNotIn("data-alttext", summary)

    def test_summary_argument_overrides_document_setting(self):
        sample = f"""
        <html><body><article>
            <p>{ARTICLE_TEXT}</p>
            <p>The position <math><msub><mi>x</mi><mi>i</mi></msub></math>
            of each particle is tracked over time in the experiment.</p>
        </article></body></html>
        """

        summary = Document(sample).summary(keep_formulas=True)

        self.assertIn('data-readability-formula="inline"', summary)


if __name__ == "__main__":
    unittest.main()
