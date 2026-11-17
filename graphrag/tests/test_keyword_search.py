"""BM25 keyword search: tokenizer, index, saved index and per-graph enablement
(GML-2367)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from common.utils.keyword_tokens import tokenize

try:
    from supportai import keyword_index
    from supportai.keyword_index import BM25Index
    from tools import tool_registry
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


class TestTokenize(unittest.TestCase):
    def test_latin_words_numbers_and_stopwords(self):
        self.assertEqual(
            tokenize("The State Aid decision (Note 5) for U.S. e-commerce"),
            ["state", "aid", "decision", "note", "5", "u.s", "e-commerce"],
        )

    def test_possessive_matches_the_plain_name(self):
        self.assertEqual(tokenize("Apple’s"), tokenize("Apple's"))
        self.assertEqual(tokenize("Apple's"), ["apple"])

    def test_cjk_runs_become_character_pairs(self):
        self.assertEqual(tokenize("東京都"), ["東京", "京都"])
        self.assertEqual(tokenize("年"), ["年"])

    def test_numbers_split_from_cjk(self):
        self.assertEqual(tokenize("2023年度"), ["2023", "年度"])

    def test_width_and_case_are_normalized(self):
        self.assertEqual(tokenize("２０２１ ＡＢＣ ﾃｽﾄ"), ["2021", "abc", "テス", "スト"])

    def test_repeated_words_are_kept(self):
        self.assertEqual(tokenize("deposit deposit"), ["deposit", "deposit"])


class TestBM25Index(unittest.TestCase):
    def _index(self):
        return BM25Index.build([
            ("c1", tokenize("Apple net sales rose. Apple products.")),
            ("c2", tokenize("Apple income taxes: the State Aid decision, see Note 5.")),
            ("c3", tokenize("Apple services revenue and Apple margins.")),
        ])

    def test_rare_term_outranks_common_term(self):
        hits = self._index().search(tokenize("Apple state aid"), 3)
        self.assertEqual(hits[0][0], "c2")

    def test_only_matching_chunks_are_returned(self):
        hits = self._index().search(tokenize("state aid"), 10)
        self.assertEqual([cid for cid, _ in hits], ["c2"])

    def test_top_k_limits_results(self):
        self.assertEqual(len(self._index().search(tokenize("apple"), 2)), 2)

    def test_empty_index_and_no_match(self):
        self.assertEqual(BM25Index.build([]).search(["x"], 5), [])
        self.assertEqual(self._index().search(tokenize("zebra"), 5), [])


class TestKeywordMatch(unittest.TestCase):
    """How well keyword results cover the question's distinctive words."""

    def _index(self):
        docs = [("c1", tokenize("Apple net sales rose. Apple products.")),
                ("c2", tokenize("Apple income taxes: the State Aid decision, see Note 5.")),
                ("c3", tokenize("Apple services revenue and Apple margins."))]
        docs += [(f"f{i}", tokenize(f"Apple filler passage number {i}.")) for i in range(20)]
        return BM25Index.build(docs)

    def test_all_distinctive_words_in_one_passage_is_strong(self):
        m = self._index().match(tokenize("Apple State Aid decision"), 5)
        self.assertEqual(m.chunks[0][0], "c2")
        self.assertAlmostEqual(m.coverage, 1.0)
        self.assertEqual(m.missing_terms, [])
        self.assertEqual(m.strength, "strong")

    def test_missing_rare_words_are_reported_and_weaken_the_match(self):
        m = self._index().match(tokenize("Apple State Aid escrow appeal"), 5)
        self.assertEqual(set(m.missing_terms), {"escrow", "appeal"})
        self.assertLess(m.coverage, keyword_index.STRONG_COVERAGE)
        self.assertNotEqual(m.strength, "strong")

    def test_only_a_common_word_matches_is_weak(self):
        m = self._index().match(tokenize("Apple quarterly dividend policy"), 5)
        self.assertTrue(m.chunks)
        self.assertEqual(m.strength, "weak")
        self.assertEqual(set(m.missing_terms), {"quarterly", "dividend", "policy"})

    def test_rare_words_weigh_more_than_common_words(self):
        index = self._index()
        rare = index.match(tokenize("state aid"), 5)          # rare words found
        common = index.match(tokenize("apple zebra"), 5)      # common word found, rare missing
        self.assertGreater(rare.coverage, common.coverage)

    def test_no_result_is_none(self):
        m = self._index().match(tokenize("zebra"), 5)
        self.assertEqual(m.strength, "none")
        self.assertEqual(m.missing_terms, ["zebra"])


class TestHybridAddsOnlyStrongMatches(unittest.TestCase):
    def _run(self, strength):
        from supportai.retrievers.HybridRetriever import HybridRetriever
        retriever = HybridRetriever.__new__(HybridRetriever)
        retriever.conn = mock.Mock(graphname="G")
        retriever.logger = mock.Mock()
        match = keyword_index.KeywordMatch(chunks=[("k1", 3.0)], query_terms=2)
        res = [{"final_retrieval": {"v1": ["vector text"]}}]
        with mock.patch("common.config.get_graphrag_config", return_value={"keyword_search": True}), \
             mock.patch("supportai.keyword_index.keyword_match", return_value=match), \
             mock.patch.object(keyword_index.KeywordMatch, "strength", new_callable=mock.PropertyMock,
                               return_value=strength), \
             mock.patch("supportai.keyword_index.chunk_texts", return_value={"k1": "keyword text"}):
            retriever._add_keyword_matches("q", res, 5)
        return res[0]["final_retrieval"]

    def test_strong_match_is_added(self):
        self.assertIn("k1", self._run("strong"))

    def test_weaker_match_is_not_added(self):
        for strength in ("partial", "weak", "none"):
            self.assertEqual(list(self._run(strength)), ["v1"], strength)


class _Conn:
    """A graph with chunk Content and one document Content vertex. With
    ``paths``, its chunks have the heading_path attribute; without, asking
    for it fails as on a graph created before chunk section paths."""
    def __init__(self, chunks, graphname="G", paths=None):
        self.chunks, self.graphname = dict(chunks), graphname
        self.paths = paths
        self.loads = 0

    def getVertexCount(self, vtype, where=""):
        return len(self.chunks)

    def getVertices(self, vtype, select=""):
        if vtype == "DocumentChunk":
            if select == "heading_path":
                if self.paths is None:
                    raise Exception("invalid attribute name 'heading_path'")
                self.loads += 1
                return [{"v_id": c, "attributes": {"heading_path": self.paths.get(c, "")}}
                        for c in self.chunks]
            self.loads += 1
            return [{"v_id": c, "attributes": {"idx": 0}} for c in self.chunks]
        rows = [{"v_id": c, "attributes": {"text": t}} for c, t in self.chunks.items()]
        return rows + [{"v_id": "doc1", "attributes": {"text": "state aid " * 50}}]

    def runInstalledQuery(self, name, params=None, usePost=False):
        self.batches = getattr(self, "batches", 0) + 1
        ids = [d["v"] for d in json.loads(params["json_list_vts"])]
        return [{"results": [{"v_id": c, "attributes": {"context": [self.chunks[c]]}}
                             for c in ids if c in self.chunks]}]


class _CacheTest(unittest.TestCase):
    """Runs in a temporary working directory, so saved indexes go there."""
    enabled = True

    def setUp(self):
        keyword_index._indexes.clear()
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        patcher = mock.patch.object(
            keyword_index, "get_graphrag_config",
            side_effect=lambda g=None: {"keyword_search": self.enabled},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()
        keyword_index._indexes.clear()


class TestKeywordIndexCache(_CacheTest):
    def test_builds_from_chunk_text_only(self):
        conn = _Conn({"c1": "state aid", "c2": "net sales"})
        hits = keyword_index.keyword_search(conn, "state aid", 5)
        self.assertEqual([c for c, _ in hits], ["c1"])  # document Content is not indexed
        keyword_index.keyword_search(conn, "aid", 5)
        self.assertEqual(conn.loads, 1)

    def test_rebuilds_when_the_chunk_count_changes(self):
        conn = _Conn({"c1": "state aid"})
        keyword_index.keyword_search(conn, "aid", 5)
        conn.chunks["c2"] = "state aid note"
        keyword_index._indexes["G"].checked_at = 0
        self.assertEqual(len(keyword_index.keyword_search(conn, "aid", 5)), 2)
        self.assertEqual(conn.loads, 2)

    def test_status_after_build(self):
        conn = _Conn({"c1": "state aid", "c2": "net sales"})
        keyword_index.build_keyword_index(conn)
        status = keyword_index.keyword_index_status("G")
        self.assertTrue(status["enabled"] and status["built"])
        self.assertEqual((status["chunks"], status["terms"]), (2, 4))


class TestSavedIndex(_CacheTest):
    def test_restart_loads_the_saved_index(self):
        conn = _Conn({"c1": "state aid", "c2": "net sales"})
        keyword_index.build_keyword_index(conn)
        self.assertTrue(os.path.exists("configs/graph_data/G/keyword_index.json.gz"))
        keyword_index._indexes.clear()  # a restart
        hits = keyword_index.keyword_search(conn, "state aid", 5)
        self.assertEqual([c for c, _ in hits], ["c1"])
        self.assertEqual(conn.loads, 1)  # read from the file, not built again

    def test_saved_index_with_another_chunk_count_is_rebuilt(self):
        conn = _Conn({"c1": "state aid"})
        keyword_index.build_keyword_index(conn)
        keyword_index._indexes.clear()
        conn.chunks["c2"] = "state aid note"
        self.assertEqual(len(keyword_index.keyword_search(conn, "aid", 5)), 2)
        self.assertEqual(conn.loads, 2)

    def test_saved_index_from_another_tokenizer_is_rebuilt(self):
        conn = _Conn({"c1": "state aid"})
        keyword_index.build_keyword_index(conn)
        keyword_index._indexes.clear()
        with mock.patch.object(keyword_index, "TOKENIZER_VERSION", "other"):
            keyword_index.keyword_search(conn, "aid", 5)
        self.assertEqual(conn.loads, 2)

    def test_saved_index_of_other_content_is_rebuilt(self):
        conn = _Conn({"c1": "state aid"})
        keyword_index.build_keyword_index(conn)
        keyword_index._indexes.clear()
        with mock.patch.object(keyword_index, "_INDEX_CONTENT", "text"):
            keyword_index.keyword_search(conn, "aid", 5)
        self.assertEqual(conn.loads, 2)

    def test_invalidate_removes_the_saved_index(self):
        conn = _Conn({"c1": "state aid"})
        keyword_index.build_keyword_index(conn)
        keyword_index.invalidate_keyword_index("G")
        self.assertFalse(os.path.exists("configs/graph_data/G/keyword_index.json.gz"))


class TestKeywordSearchOff(_CacheTest):
    enabled = False

    def test_search_returns_nothing_and_builds_nothing(self):
        conn = _Conn({"c1": "state aid"})
        self.assertEqual(keyword_index.keyword_search(conn, "aid", 5), [])
        self.assertEqual(conn.loads, 0)
        self.assertFalse(keyword_index.keyword_index_status("G")["enabled"])


class TestChunkTexts(unittest.TestCase):
    def test_reads_all_texts_in_one_query(self):
        conn = mock.Mock()
        conn.runInstalledQuery.return_value = [{"results": [
            {"v_id": "c1", "attributes": {"context": ["state aid"]}},
            {"v_id": "c2", "attributes": {"context": ["net sales"]}},
        ]}]
        self.assertEqual(keyword_index.chunk_texts(conn, ["c1", "c2"]),
                         {"c1": "state aid", "c2": "net sales"})
        conn.runInstalledQuery.assert_called_once()
        conn.getVerticesById.assert_not_called()

    def test_reads_one_by_one_when_the_query_fails(self):
        conn = mock.Mock()
        conn.runInstalledQuery.side_effect = RuntimeError("c9 is not valid DocumentChunk vertex")
        conn.getVerticesById.side_effect = lambda vt, cid: (
            [{"attributes": {"text": f"text of {cid}"}}] if cid != "c9" else []
        )
        self.assertEqual(keyword_index.chunk_texts(conn, ["c1", "c9"]), {"c1": "text of c1"})

    def test_no_ids_makes_no_request(self):
        conn = mock.Mock()
        self.assertEqual(keyword_index.chunk_texts(conn, []), {})
        conn.runInstalledQuery.assert_not_called()


class TestAgentToolFollowsTheSetting(unittest.TestCase):
    def _ctx(self, enabled):
        return mock.Mock(graphrag_cfg={"keyword_search": enabled}, external_tools={})

    def test_tool_offered_only_when_enabled(self):
        self.assertIn(tool_registry.KEYWORD_SEARCH_TOOL, tool_registry.tool_names(self._ctx(True)))
        self.assertNotIn(tool_registry.KEYWORD_SEARCH_TOOL, tool_registry.tool_names(self._ctx(False)))
        self.assertNotIn(tool_registry.KEYWORD_SEARCH_TOOL, tool_registry.tool_names(None))

    def test_other_tools_stay(self):
        names = tool_registry.tool_names(self._ctx(False))
        self.assertIn("graphrag__hybrid_search", names)


if __name__ == "__main__":
    unittest.main()


class TestSectionPathsAreIndexed(_CacheTest):
    def test_a_word_only_in_the_section_path_finds_the_chunk(self):
        conn = _Conn({"c1": "The company recorded an escrow balance.", "c2": "Net sales rose."},
                     paths={"c1": "Annual Report > Note 5 – Income Taxes > State Aid"})
        hits = keyword_index.keyword_search(conn, "income taxes", 5)
        self.assertEqual([cid for cid, _ in hits], ["c1"])

    def test_a_graph_without_the_attribute_indexes_the_text(self):
        conn = _Conn({"c1": "Note 5 – Income Taxes\n\nThe escrow balance.", "c2": "Net sales."})
        hits = keyword_index.keyword_search(conn, "income taxes", 5)
        self.assertEqual([cid for cid, _ in hits], ["c1"])


class TestIndexBuildInBatches(_CacheTest):
    def test_chunk_text_is_read_in_batches(self):
        conn = _Conn({f"c{i}": f"word{i} common" for i in range(5)}, paths={})
        with mock.patch.object(keyword_index, "_LOAD_BATCH", 2):
            entry = keyword_index.build_keyword_index(conn)
        self.assertEqual(conn.batches, 3)
        self.assertEqual(len(entry.index), 5)

    def test_a_batch_too_large_for_the_database_is_split(self):
        conn = _Conn({f"c{i}": f"word{i}" for i in range(4)}, paths={})
        answer = conn.runInstalledQuery

        def limited(name, params=None, usePost=False):
            if len(json.loads(params["json_list_vts"])) > 1:
                raise Exception("('The query response size is 30MB, which exceeds limit 4MB.', 'REST-4000')")
            return answer(name, params, usePost)

        conn.runInstalledQuery = limited
        with mock.patch.object(keyword_index, "_LOAD_BATCH", 4):
            entry = keyword_index.build_keyword_index(conn)
        self.assertEqual(len(entry.index), 4)
