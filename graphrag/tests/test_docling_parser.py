"""Document parsing with Docling: parser selection, the HTML it writes, and
how the structured chunker reads that HTML."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.chunkers.structured import StructuredChunker, html_to_elements
from common.utils import docling_extractor
from common.utils.text_extractors import (
    ExtractionError,
    extract_text_from_file_with_images_as_docs,
    get_supported_extensions,
)


def _cfg(**kw):
    return mock.patch("common.config.get_graphrag_config", return_value=kw)


class TestParserSetting(unittest.TestCase):
    def test_default_parser_keeps_the_current_extractors(self):
        with _cfg():
            self.assertEqual(docling_extractor.document_parser("g"), "default")
            for ext in (".pdf", ".docx", ".html", ".xlsx"):
                self.assertFalse(docling_extractor.use_docling(ext, "g"), ext)

    def test_docling_parses_all_formats_it_reads(self):
        with _cfg(document_parser="docling"):
            for ext in (".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".csv", ".xml"):
                self.assertTrue(docling_extractor.use_docling(ext, "g"), ext)

    def test_markdown_text_and_images_keep_their_extractors(self):
        with _cfg(document_parser="docling"):
            for ext in (".md", ".txt", ".json", ".jsonl", ".xls", ".png", ".jpg", ".gif"):
                self.assertFalse(docling_extractor.use_docling(ext, "g"), ext)

    def test_powerpoint_always_uses_docling(self):
        with _cfg():
            self.assertTrue(docling_extractor.use_docling(".pptx", "g"))
        self.assertIn(".pptx", get_supported_extensions())

    def test_unknown_value_means_default(self):
        with _cfg(document_parser="pdfplumber"):
            self.assertEqual(docling_extractor.document_parser("g"), "default")


class TestFormulaTranscription(unittest.TestCase):
    def test_reply_is_reduced_to_latex(self):
        from common.utils import image_data_extractor as ide
        for reply in ("```latex\nx^2\n```", "$$x^2$$", "x^2"):
            with mock.patch.object(ide, "_ask_about_image", return_value=reply):
                self.assertEqual(ide.transcribe_formula_with_llm("f.png"), "x^2")

    def test_a_failed_call_gives_no_latex(self):
        from common.utils import image_data_extractor as ide
        with mock.patch.object(ide, "_ask_about_image", side_effect=ValueError("bad image")):
            self.assertEqual(ide.transcribe_formula_with_llm("f.png"), "")


class TestFallback(unittest.TestCase):
    def _file(self, suffix, text="<html><body><p>hello</p></body></html>"):
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, mode="w")
        f.write(text)
        f.close()
        return f.name

    def test_a_docling_failure_uses_the_default_extractor(self):
        path = self._file(".html")
        with _cfg(document_parser="docling"), \
             mock.patch.object(docling_extractor, "extract_with_docling", side_effect=RuntimeError("x")):
            docs = extract_text_from_file_with_images_as_docs(path, "g")
        self.assertIn("hello", docs[0]["content"])

    def test_a_powerpoint_failure_is_reported(self):
        path = self._file(".pptx", "not a deck")
        with _cfg(), mock.patch.object(docling_extractor, "extract_with_docling",
                                       side_effect=RuntimeError("x")):
            with self.assertRaises(ExtractionError):
                extract_text_from_file_with_images_as_docs(path, "g")


DOCLING_HTML = """<html><body data-parser="docling">
<h1 data-page="1">Annual Report</h1>
<p data-page="1">Introduction text.</p>
<h2 data-page="2">Revenue</h2>
<table data-page="2"><caption>Table 1. Sales by year</caption><tbody>
<tr><td>Year</td><td>Sales</td></tr><tr><td>2022</td><td>10,678</td></tr></tbody></table>
<table data-page="2"><tbody><tr><td colspan="2">Merged</td></tr><tr><td>a</td><td>b</td></tr></tbody></table>
<figure data-page="3" data-kind="bar_chart"><img src="tg://report_image_1" alt="Figure 2">
<figcaption>Figure 2. Payouts</figcaption><div class="figure-description">2017: 4,000; 2018: 10,678</div></figure>
<div class="formula" data-page="3">$$\\frac{a}{b} \\tag{2}$$</div>
<ul data-page="3"><li>first</li><li>second</li></ul>
</body></html>"""


class TestDoclingHtmlChunks(unittest.TestCase):
    def setUp(self):
        self.elements = html_to_elements(DOCLING_HTML)

    def _kind(self, kind):
        return [e for e in self.elements if e.kind == kind]

    def test_headings_give_section_paths_and_pages(self):
        table = self._kind("table")[0]
        self.assertEqual(table.heading, "Annual Report > Revenue")
        self.assertEqual(table.page, 2)

    def test_a_table_becomes_markdown_with_its_caption(self):
        table = self._kind("table")[0].text
        self.assertTrue(table.startswith("Table 1. Sales by year"))
        self.assertIn("| 2022 | 10,678 |", table)
        self.assertNotIn("<td>", table)

    def test_merged_cells_are_spread_over_the_grid(self):
        table = self._kind("table")[1].text
        self.assertNotIn("colspan", table)
        self.assertIn("| Merged |  |", table)
        self.assertIn("| a | b |", table)

    def test_a_cell_spanning_rows_repeats_on_each_row(self):
        from bs4 import BeautifulSoup
        from common.chunkers.structured import _html_table_to_markdown
        t = BeautifulSoup('<table><tr><th rowspan="2">2022</th><td>Q1</td></tr>'
                          '<tr><td>Q2</td></tr></table>', "html.parser").table
        self.assertEqual(_html_table_to_markdown(t).splitlines()[2], "| 2022 | Q2 |")

    def test_a_figure_keeps_image_caption_and_data_together(self):
        fig = self._kind("figure")[0]
        self.assertIn("![Figure 2](tg://report_image_1)", fig.text)
        self.assertIn("Figure 2. Payouts", fig.text)
        self.assertIn("2018: 10,678", fig.text)
        self.assertEqual(fig.page, 3)

    def test_formula_and_list_are_plain_text(self):
        prose = [e.text for e in self._kind("prose")]
        self.assertIn("$$\\frac{a}{b} \\tag{2}$$", prose)
        self.assertEqual(self._kind("list")[0].text, "- first\n- second")

    def test_chunks_carry_no_markup(self):
        chunks = StructuredChunker(chunk_size=2000, overlap_size=0).chunk(DOCLING_HTML)
        text = "\n".join(str(c) for c in chunks)
        self.assertNotIn("<figure", text)
        self.assertNotIn("<li>", text)
        self.assertIn("10,678", text)

    def test_a_table_keeps_the_short_lines_above_it(self):
        html = ('<html><body data-parser="docling"><h2 data-page="1">Sales</h2>'
                '<p data-page="1">A long paragraph. ' + "Text. " * 40 + '</p>'
                '<p data-page="1">Net sales were as follows (in millions):</p>'
                '<p data-page="1">(In millions)</p>'
                '<table data-page="1"><tr><td>iPhone</td><td>40,665</td></tr></table>'
                '</body></html>')
        els = html_to_elements(html)
        table = [e for e in els if e.kind == "table"][0].text
        self.assertTrue(table.startswith("Net sales were as follows (in millions):\n(In millions)"))
        self.assertIn("| iPhone | 40,665 |", table)
        self.assertEqual(len([e for e in els if e.kind == "prose"]), 1)

    def test_other_html_keeps_the_current_behavior(self):
        plain = DOCLING_HTML.replace(' data-parser="docling"', "")
        tables = [e for e in html_to_elements(plain) if e.kind == "table"]
        self.assertIn("<td>", tables[0].text)


class TestDoclingHeadings(unittest.TestCase):
    def _headings(self, body):
        html = f'<html><body data-parser="docling">{body}</body></html>'
        return [(e.kind, e.text, e.heading) for e in html_to_elements(html)]

    def test_a_heading_on_every_page_is_a_running_header(self):
        body = "".join(f'<h2 data-page="{p}">Apple Inc.</h2><p data-page="{p}">Page {p} text.</p>'
                       for p in (1, 2, 3))
        out = self._headings('<h1 data-page="1">Report</h1>' + body)
        self.assertNotIn("Apple Inc.", [t for k, t, _ in out if k == "heading"])
        self.assertEqual(out[-1][2], "Report")

    def test_a_heading_without_content_starts_no_section(self):
        out = self._headings('<h2 data-page="1">Empty</h2><h2 data-page="1">Sales</h2>'
                             '<p data-page="1">Text.</p><h3 data-page="1">Last</h3>')
        self.assertEqual([t for k, t, _ in out if k == "heading"], ["Sales"])
        self.assertEqual(out[-1], ("prose", "Text.", "Sales"))

    def test_a_parent_heading_keeps_its_children(self):
        out = self._headings('<h1 data-page="1">Part I</h1><h2 data-page="1">Item 1</h2>'
                             '<p data-page="1">Text.</p>')
        self.assertEqual(out[-1][2], "Part I > Item 1")


class TestDocumentToHtml(unittest.TestCase):
    """The writer, on a document built in code."""

    def setUp(self):
        try:
            from docling_core.types.doc import DoclingDocument, DocItemLabel, TableData
            from docling_core.types.doc.base import BoundingBox
            from docling_core.types.doc.document import ProvenanceItem, TableCell
        except ImportError as exc:  # docling is only in the graphrag image
            self.skipTest(f"docling unavailable: {exc}")
        prov = lambda p: ProvenanceItem(page_no=p, bbox=BoundingBox(l=0, t=0, r=1, b=1), charspan=(0, 1))
        doc = DoclingDocument(name="t")
        doc.add_page(page_no=1, size={"width": 100, "height": 100})
        doc.add_text(label=DocItemLabel.PAGE_HEADER, text="Company confidential", prov=prov(1))
        doc.add_title(text="Report", prov=prov(1))
        doc.add_heading(text="Sales", level=2, prov=prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="A & B\x08", prov=prov(1))
        cells = [TableCell(text=t, start_row_offset_idx=r, end_row_offset_idx=r + 1,
                           start_col_offset_idx=c, end_col_offset_idx=c + 1)
                 for r, row in enumerate([["Year", "Sales"], ["2022", "5"]]) for c, t in enumerate(row)]
        table = doc.add_table(data=TableData(num_rows=2, num_cols=2, table_cells=cells), prov=prov(1))
        cap = doc.add_text(label=DocItemLabel.CAPTION, text="Table 1", prov=prov(1))
        table.captions.append(cap.get_ref())
        doc.add_text(label=DocItemLabel.FORMULA, text="garbled", prov=prov(1))
        pic = doc.add_picture(prov=prov(1))
        doc.add_text(label=DocItemLabel.PAGE_FOOTER, text="Page 1", prov=prov(1))
        formula = next(i for i in doc.texts if i.label == DocItemLabel.FORMULA)
        self.html = docling_extractor.document_to_html(
            doc,
            figures={pic.self_ref: {"src": "tg://t_image_1", "description": "2022: 5"}},
            formulas={formula.self_ref: "x^2"},
        )

    def test_structure_is_kept(self):
        self.assertIn('<body data-parser="docling">', self.html)
        self.assertIn('<h1 data-page="1">Report</h1>', self.html)
        self.assertIn('<h2 data-page="1">Sales</h2>', self.html)
        self.assertIn("A &amp; B", self.html)
        self.assertNotIn("\x08", self.html)
        self.assertRegex(self.html, r"<caption>.*Table 1.*</caption>")
        self.assertIn('<img src="tg://t_image_1"', self.html)
        self.assertIn("2022: 5", self.html)
        self.assertIn("$$x^2$$", self.html)

    def test_page_headers_footers_and_captions_appear_once(self):
        self.assertNotIn("Company confidential", self.html)
        self.assertNotIn("Page 1", self.html)
        self.assertEqual(self.html.count("Table 1"), 1)


if __name__ == "__main__":
    unittest.main()
