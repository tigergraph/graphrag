"""Document parsing with Docling: parser selection and the Markdown it
writes for the structured chunker."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.chunkers.structured import StructuredChunker
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


class TestCleanMarkdown(unittest.TestCase):
    """Running text, page numbers and headings without a section."""

    def _clean(self, blocks):
        return [t for _, t in docling_extractor._clean_markdown(blocks)]

    def test_text_at_the_top_of_every_page_is_left_out(self):
        blocks = []
        for p in (1, 2, 3):
            blocks += [(p, "Apple Inc. | 2024 Form 10-K | %d" % p), (p, "Body " + "abc"[p - 1])]
        self.assertEqual(self._clean(blocks), ["Body a", "Body b", "Body c"])

    def test_a_bare_page_number_is_left_out(self):
        out = self._clean([(1, "## Sales"), (1, "Text."), (1, "- 12 -")])
        self.assertEqual(out, ["## Sales", "Text."])

    def test_a_number_inside_the_page_is_kept(self):
        blocks = [(1, "First."), (1, "Second."), (1, "42"), (1, "Fourth."), (1, "Fifth.")]
        self.assertIn("42", self._clean(blocks))

    def test_a_sentence_marked_as_heading_becomes_text(self):
        out = self._clean([(1, "## Sales"), (1, "## 売上高は増加しました。"), (1, "Text.")])
        self.assertEqual(out, ["## Sales", "売上高は増加しました。", "Text."])

    def test_a_heading_at_the_end_is_left_out(self):
        out = self._clean([(1, "## Sales"), (1, "Text."), (1, "### Last"), (1, "## Last too")])
        self.assertEqual(out, ["## Sales", "Text."])

    def test_a_heading_followed_by_a_heading_is_kept(self):
        # Docling can give a section a deeper level than its first subheading.
        blocks = [(1, "###### Note 5 - Income Taxes"), (1, "Tax text."),
                  (1, "###### Note 6 - Debt"), (1, "### Commercial Paper"), (1, "Paper text.")]
        self.assertEqual(self._clean(blocks), [t for _, t in blocks])

    def test_a_parent_heading_keeps_its_children(self):
        out = self._clean([(1, "# Part I"), (1, "## Item 1"), (1, "Text.")])
        self.assertEqual(out, ["# Part I", "## Item 1", "Text."])


class TestDocumentToMarkdown(unittest.TestCase):
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
        for p in (1, 2):
            doc.add_page(page_no=p, size={"width": 100, "height": 100})
        doc.add_text(label=DocItemLabel.PAGE_HEADER, text="Company confidential", prov=prov(1))
        doc.add_title(text="Report", prov=prov(1))
        doc.add_heading(text="Sales", level=2, prov=prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="A & B\x08 rose.", prov=prov(1))
        cells = [TableCell(text=t, start_row_offset_idx=r, end_row_offset_idx=r + 1,
                           start_col_offset_idx=c, end_col_offset_idx=c + 1)
                 for r, row in enumerate([["Year", "Sales"], ["2022", "5"]]) for c, t in enumerate(row)]
        table = doc.add_table(data=TableData(num_rows=2, num_cols=2, table_cells=cells), prov=prov(1))
        cap = doc.add_text(label=DocItemLabel.CAPTION, text="Table 1", prov=prov(1))
        table.captions.append(cap.get_ref())
        doc.add_text(label=DocItemLabel.PAGE_FOOTER, text="Page 1", prov=prov(1))
        doc.add_heading(text="Outlook", level=2, prov=prov(2))
        doc.add_text(label=DocItemLabel.FORMULA, text="garbled", prov=prov(2))
        chart = doc.add_picture(prov=prov(2))
        logo = doc.add_picture(prov=prov(2))
        formula = next(i for i in doc.texts if i.label == DocItemLabel.FORMULA)
        self.md = docling_extractor.document_to_markdown(
            doc,
            figures={chart.self_ref: {"src": "tg://t_image_1", "description": "Bar chart. 2022: 5"},
                     logo.self_ref: None},
            formulas={formula.self_ref: "x^2"},
        )

    def test_pages_headings_and_text(self):
        self.assertTrue(self.md.startswith("<!-- PAGE 1 -->"))
        self.assertIn("<!-- PAGE 2 -->", self.md)
        self.assertIn("# Report", self.md)
        self.assertIn("## Sales", self.md)
        self.assertIn("A & B  rose.", self.md)
        self.assertNotIn("\x08", self.md)

    def test_a_table_is_markdown_with_its_caption(self):
        self.assertIn("Table 1", self.md)
        self.assertRegex(self.md, r"\|\s*2022\s*\|\s*5\s*\|")
        self.assertNotIn("<td>", self.md)

    def test_pictures_link_to_their_image_documents(self):
        self.assertIn("![Bar chart. 2022: 5](tg://t_image_1)", self.md)
        self.assertEqual(self.md.count("!["), 1)
        self.assertNotIn("<!--  docling", self.md)
        self.assertNotIn("docling-image", self.md)

    def test_a_formula_is_latex(self):
        self.assertIn("x^2", self.md)
        self.assertNotIn("garbled", self.md)

    def test_page_headers_footers_and_captions_appear_once(self):
        self.assertNotIn("Company confidential", self.md)
        self.assertNotIn("Page 1", self.md)
        self.assertEqual(self.md.count("Table 1"), 1)

    def test_chunks_keep_the_page_without_markers(self):
        chunks = StructuredChunker(chunk_size=2000, overlap_size=0).chunk(self.md)
        table = next(c for c in chunks if "2022" in str(c))
        self.assertEqual(table.page_no, 1)
        self.assertNotIn("<!-- PAGE", str(table))


if __name__ == "__main__":
    unittest.main()
