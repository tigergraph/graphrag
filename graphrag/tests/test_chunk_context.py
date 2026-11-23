"""Chunk heading path and summary in their own attributes (GML-2369)."""
import asyncio
import os
import sys
import unittest
from unittest import mock

from common.chunkers.structured import StructuredChunk
from common.db.retriever_render import TEMPLATED_RETRIEVERS, load_template, render_retriever_body
from common.utils.chunk_context import (
    chunk_parts,
    embedding_text,
    format_chunk_summary,
    has_chunk_context_attrs,
    schema_change_gsql,
    schema_has_chunk_context,
    split_heading_path,
)


class TestHelpers(unittest.TestCase):
    def test_split_a_text_that_starts_with_its_path(self):
        self.assertEqual(split_heading_path("A > B\n\nbody", "A > B"), ("A > B", "body"))

    def test_text_without_its_path_is_kept_whole(self):
        self.assertEqual(split_heading_path("body", "A > B"), ("", "body"))
        self.assertEqual(split_heading_path("A > B body", "A > B"), ("", "A > B body"))
        self.assertEqual(split_heading_path("A > B\n\nbody", None), ("", "A > B\n\nbody"))

    def test_chunk_parts_reads_the_structured_breadcrumb(self):
        chunk = StructuredChunk("Note 5 > Income Taxes\n\nState Aid", under_heading="Note 5 > Income Taxes")
        self.assertEqual(chunk_parts(chunk), ("Note 5 > Income Taxes", "State Aid"))
        self.assertEqual(chunk_parts("plain chunk"), ("", "plain chunk"))

    def test_embedding_text_skips_empty_parts(self):
        self.assertEqual(embedding_text("P", "S", "B"), "P\n\nS\n\nB")
        self.assertEqual(embedding_text("", "S", "B"), "S\n\nB")
        self.assertEqual(embedding_text("", "", "B"), "B")

    def test_summary_format(self):
        self.assertEqual(
            format_chunk_summary({"topic": "預金", "section": "(3)", "entities": ["東京都", "2021"]}),
            "TOPIC: 預金\nSECTION: (3)\nENTITIES: 東京都, 2021",
        )
        self.assertEqual(format_chunk_summary(None), "")
        self.assertEqual(format_chunk_summary({"topic": " "}), "")

    def test_schema_detection(self):
        def schema(*names):
            return {"VertexTypes": [{"Name": "DocumentChunk",
                                     "Attributes": [{"AttributeName": n} for n in names]}]}
        self.assertTrue(schema_has_chunk_context(schema("idx", "heading_path", "summary")))
        self.assertFalse(schema_has_chunk_context(schema("idx", "heading_path")))
        self.assertFalse(schema_has_chunk_context({}))
        self.assertFalse(has_chunk_context_attrs(None))

    def test_schema_change_has_the_job_and_drops_it(self):
        gsql = schema_change_gsql("G")
        self.assertIn("ALTER VERTEX DocumentChunk ADD ATTRIBUTE (heading_path STRING, summary STRING)", gsql)
        self.assertIn("RUN SCHEMA_CHANGE JOB add_chunk_context", gsql)
        self.assertTrue(gsql.rstrip().endswith("DROP JOB add_chunk_context"))


class TestRetrieverRendering(unittest.TestCase):
    def _render(self, name, on):
        return render_retriever_body(
            load_template(name), domain_vts=[], domain_edges=[], include_entity=True, chunk_context=on
        )

    def test_without_the_attribute_the_queries_do_not_change(self):
        for name in TEMPLATED_RETRIEVERS:
            self.assertEqual(self._render(name, False), load_template(name), name)

    def test_each_chunk_text_is_shown_after_its_path(self):
        expected = {
            "GraphRAG_Hybrid_Vector_Search": "s.@context += (CASE WHEN s.heading_path",
            "GraphRAG_Hybrid_Search": "s.@context += (CASE WHEN s.heading_path",
            "Chunk_Sibling_Vector_Search": "s.@content += (CASE WHEN s.heading_path",
            "Content_Similarity_Vector_Search": 's.getAttr("heading_path", "STRING")',
            "GraphRAG_Community_Vector_Search": "m.@context += (CASE WHEN d.heading_path",
            "GraphRAG_Community_Search": "m.@context += (CASE WHEN d.heading_path",
        }
        for name, marker in expected.items():
            self.assertIn(marker, self._render(name, True), name)

    def test_document_level_text_is_not_changed(self):
        body = self._render("GraphRAG_Hybrid_Vector_Search", True)
        self.assertIn("d.@context += t.text", body)
        community = self._render("GraphRAG_Community_Vector_Search", True)
        self.assertEqual(community.count("CASE WHEN d.heading_path"), 1)


_ECC_APP = os.path.join(os.path.dirname(__file__), "..", "..", "ecc", "app")


def _import_regenerate():
    try:
        sys.path.insert(0, os.path.abspath(_ECC_APP))
        for name in [m for m in sys.modules if m == "graphrag" or m.startswith("graphrag.")]:
            del sys.modules[name]
        from graphrag import regenerate
        return regenerate
    except Exception as exc:  # needs the ECC environment
        raise unittest.SkipTest(f"ECC environment unavailable: {exc}")
    finally:
        sys.path.pop(0)


class _Conn:
    """A graph with one document chunked into two chunks."""
    graphname = "G"

    def __init__(self, stored):
        self.stored = stored
        self.upserts = {}

    async def getSchema(self, force=False):
        return {"VertexTypes": [{"Name": "DocumentChunk", "Attributes": [
            {"AttributeName": "heading_path"}, {"AttributeName": "summary"}]}]}

    async def getVertices(self, vtype, select=""):
        return [{"v_id": "doc1", "attributes": {}}]

    async def getVerticesById(self, vtype, vid):
        return [{"attributes": {"text": "the document", "ctype": "structured"}}]

    async def runInstalledQuery(self, name, params=None, usePost=False):
        return [{"results": [{"v_id": c, "attributes": {"context": [t]}} for c, t in self.stored.items()]}]

    async def upsertVertices(self, vtype, vertices):
        self.upserts.setdefault(vtype, []).extend(vertices)


class TestMoveHeadingPaths(unittest.TestCase):
    def setUp(self):
        self.regenerate = _import_regenerate()
        chunks = [
            StructuredChunk("Note 5\n\nState Aid", under_heading="Note 5"),
            StructuredChunk("Intro text", under_heading=None),
        ]
        chunker = mock.Mock(chunk=mock.Mock(return_value=chunks))
        patcher = mock.patch.object(self.regenerate, "_path_chunker", return_value=chunker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, stored):
        conn = _Conn(stored)
        out = asyncio.run(self.regenerate.move_heading_paths("G", conn))
        return out, conn

    def test_moves_the_path_of_an_exact_match(self):
        out, conn = self._run({"doc1_chunk_0": "Note 5\n\nState Aid", "doc1_chunk_1": "Intro text"})
        self.assertEqual(out, {"moved": 1, "unchanged": 1, "not_matched": 0})
        self.assertEqual(conn.upserts["Content"], [("doc1_chunk_0", {"text": "State Aid"})])
        self.assertEqual(conn.upserts["DocumentChunk"], [("doc1_chunk_0", {"heading_path": "Note 5"})])

    def test_a_chunk_that_differs_is_left_alone(self):
        out, conn = self._run({"doc1_chunk_0": "Note 5\n\nState Aid, edited", "doc1_chunk_1": "Intro text"})
        self.assertEqual(out, {"moved": 0, "unchanged": 1, "not_matched": 1})
        self.assertEqual(conn.upserts, {})


if __name__ == "__main__":
    unittest.main()
