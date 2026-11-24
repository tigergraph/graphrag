"""Graph setup: the pasted schema is checked before any change, queries are
created one by one and installed in one batch, and the domain schema apply
can leave the retrievers to the final batch."""
import asyncio
import unittest
from unittest import mock

from common.db import retriever_render, schema_utils


GOOD = """
ADD VERTEX Company (PRIMARY_ID id STRING, name STRING) WITH PRIMARY_ID_AS_ATTRIBUTE="true";
ADD VERTEX Person (PRIMARY_ID id STRING, name STRING) WITH PRIMARY_ID_AS_ATTRIBUTE="true";
ADD DIRECTED EDGE WORKS_FOR (FROM Person, TO Company) WITH REVERSE_EDGE="reverse_WORKS_FOR";
"""


def _check(text):
    return schema_utils.check_pasted_schema(text, schema_utils.parse_gsql_schema(text))


class TestPastedSchemaCheck(unittest.TestCase):
    def test_a_valid_schema_has_no_problems(self):
        self.assertEqual(_check(GOOD), [])

    def test_a_schema_without_vertex_types_is_rejected(self):
        self.assertEqual(_check("this is not GSQL"), ["No vertex types were found in the pasted schema."])

    def test_an_edge_to_an_undefined_vertex_type_is_rejected(self):
        text = GOOD.replace("TO Company)", "TO Compnay)")
        problems = _check(text)
        self.assertEqual(len(problems), 1)
        self.assertIn("WORKS_FOR refers to vertex type Compnay", problems[0])

    def test_a_name_used_for_a_vertex_and_an_edge_is_rejected(self):
        text = GOOD + 'ADD DIRECTED EDGE Company (FROM Person, TO Person);\n'
        self.assertIn("Company is used for both a vertex type and an edge type.", _check(text))


class _Conn:
    graphname = "G"

    def __init__(self, fail=()):
        self.blocks = []
        self.fail = set(fail)

    def gsql(self, block):
        self.blocks.append(block)
        for name in self.fail:
            if f"QUERY {name}(" in block:
                return f"SEMANTIC ERROR in query {name}"
        return "Successfully created queries"


class TestRetrieversInstallInOneBatch(unittest.TestCase):
    def test_sync(self):
        conn = _Conn()
        with mock.patch.object(retriever_render, "install_query_set") as install:
            out = retriever_render.install_retrievers(conn, "G", ["Company"], ["WORKS_FOR"], False)
        self.assertEqual(set(out), set(retriever_render.TEMPLATED_RETRIEVERS))
        self.assertFalse(any("INSTALL QUERY" in b for b in conn.blocks))
        install.assert_called_once()
        self.assertEqual(set(install.call_args.args[1]), set(retriever_render.TEMPLATED_RETRIEVERS))

    def test_async(self):
        class AConn(_Conn):
            async def gsql(self, block):
                return _Conn.gsql(self, block)

        conn = AConn(fail=["GraphRAG_Hybrid_Search"])
        with mock.patch.object(retriever_render, "install_query_set_async", new=mock.AsyncMock()) as install:
            out = asyncio.run(retriever_render.install_retrievers_async(conn, "G", ["Company"], [], False))
        self.assertTrue(out["GraphRAG_Hybrid_Search"].startswith("ERROR"))
        install.assert_awaited_once()
        self.assertNotIn("GraphRAG_Hybrid_Search", install.call_args.args[1])


class TestGraphQueries(unittest.TestCase):
    def setUp(self):
        try:
            from supportai import supportai
        except Exception as exc:  # needs the app environment
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        self.supportai = supportai

    def test_a_query_that_does_not_compile_is_named_and_the_rest_install_once(self):
        conn = _Conn(fail=["Scan_For_Updates"])
        files = self.supportai.supportai_query_files(vector=True)
        with mock.patch("common.db.query_install.install_query_set") as install:
            out = self.supportai.create_and_install_queries(conn, "G", files, ["Company"], ["WORKS_FOR"], False)
        self.assertEqual([e["query"] for e in out["errors"]], ["Scan_For_Updates"])
        install.assert_called_once()
        names = install.call_args.args[1]
        self.assertNotIn("Scan_For_Updates", names)
        self.assertIn("GraphRAG_Hybrid_Vector_Search", names)
        self.assertEqual(len(names), len(files) - 1)
        # The templated retrievers were rendered for the domain schema.
        hybrid = next(b for b in conn.blocks if "QUERY GraphRAG_Hybrid_Vector_Search(" in b)
        self.assertIn("WORKS_FOR>", hybrid)


class TestGraphQuerySet(unittest.TestCase):
    """A graph with a domain schema gets every templated retriever, as the
    setup did before the queries moved after the domain schema."""

    def setUp(self):
        try:
            from supportai import supportai
        except Exception as exc:  # needs the app environment
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        self.supportai = supportai

    def _names(self, **domain):
        conn = mock.Mock()
        conn.gsql.return_value = "- VERTEX DocumentChunk(id STRING)\n- embedding(Dimension=1536)"
        with mock.patch.object(self.supportai, "create_and_install_queries",
                               return_value={"created": [], "errors": [], "installed": []}) as cai, \
             mock.patch.object(self.supportai, "_install_vector_utility_queries"):
            self.supportai.install_graph_queries(conn, "G", **domain)
        files = cai.call_args.args[2]
        return {f.rsplit("/", 1)[-1][:-5] for f in files}

    def test_domain_graph_gets_the_text_retrievers_too(self):
        names = self._names(domain_vts=["Company"], domain_edges=["WORKS_FOR"])
        self.assertTrue({"GraphRAG_Hybrid_Search", "GraphRAG_Community_Search",
                         "GraphRAG_Hybrid_Vector_Search"} <= names)

    def test_graph_without_domain_schema_keeps_the_base_set(self):
        names = self._names()
        self.assertNotIn("GraphRAG_Hybrid_Search", names)
        self.assertIn("GraphRAG_Hybrid_Vector_Search", names)


class TestDomainApplyCanDeferRetrievers(unittest.TestCase):
    def test_retrievers_are_left_to_the_caller(self):
        conn = mock.Mock()
        conn.getVertexTypes.return_value = ["Document", "DocumentChunk", "Entity", "Community"]
        conn.getEdgeTypes.return_value = []
        conn.gsql.return_value = "Local schema change succeeded."
        proposal = schema_utils.parse_gsql_schema(GOOD)
        with mock.patch.object(schema_utils, "upsert_type_metadata", return_value={}), \
             mock.patch.object(schema_utils, "_install_retrievers_after_apply") as after:
            out = schema_utils.apply_proposal(conn, "G", proposal, install_retrievers=False)
        self.assertEqual(out["status"], "applied")
        self.assertEqual(out["retrievers"], {"status": "deferred"})
        after.assert_not_called()


if __name__ == "__main__":
    unittest.main()
