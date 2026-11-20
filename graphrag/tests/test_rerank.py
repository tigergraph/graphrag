"""Hybrid search reranking: one ranking call orders a larger candidate pool."""
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from supportai.retrievers.HybridRetriever import HybridRetriever
    import importlib
    from supportai import keyword_index
    # The package exports a class of the same name; patch the module.
    base_module = importlib.import_module("supportai.retrievers.BaseRetriever")
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


def _labels(paths=None, summaries=None):
    paths, summaries = paths or {}, summaries or {}

    def get(vtype, cid, select=""):
        return [{"attributes": {"heading_path": paths.get(cid, ""), "summary": summaries.get(cid, "")}}]
    return get


def _retriever(ranked=None, fail=False, labels=None):
    r = HybridRetriever.__new__(HybridRetriever)
    r.conn = mock.Mock(graphname="G")
    r.conn.getVerticesById.side_effect = labels or _labels()
    r.logger = mock.Mock()
    seen = {}

    def invoke(prompt, parser, variables, caller_name="", light=False):
        seen["passages"] = variables["passages"]
        seen["light"] = light
        if fail:
            raise RuntimeError("model error")
        return SimpleNamespace(ranked=ranked)
    r.llm_service = SimpleNamespace(rerank_prompt="{question}{passages}{format_instructions}",
                                    invoke_with_parser=invoke)
    return r, seen


def _cfg(**kw):
    return mock.patch.object(base_module, "get_graphrag_config", return_value=kw)


class TestRerank(unittest.TestCase):
    def test_orders_by_the_ranking_and_keeps_the_best(self):
        r, _ = _retriever(ranked=[3, 1, 2])
        with _cfg():
            self.assertEqual(r._rerank("q", {"a": "x", "b": "y", "c": "z"}, keep=2), ["c", "a"])

    def test_numbers_the_model_leaves_out_follow_in_search_order(self):
        r, _ = _retriever(ranked=[2, 9, 2])
        with _cfg():
            self.assertEqual(r._rerank("q", {"a": "x", "b": "y", "c": "z"}, keep=3), ["b", "a", "c"])

    def test_the_ranking_call_uses_light_reasoning(self):
        r, seen = _retriever(ranked=[1, 2])
        with _cfg():
            r._rerank("q", {"a": "x", "b": "y"}, keep=2)
        self.assertTrue(seen["light"])

    def test_a_failed_call_keeps_the_search_order(self):
        r, _ = _retriever(fail=True)
        with _cfg():
            self.assertEqual(r._rerank("q", {"a": "x", "b": "y"}, keep=2), ["a", "b"])

    def test_passages_show_path_summary_and_the_start_of_the_text(self):
        long_text = "word " * 1000
        r, seen = _retriever(ranked=[1, 2], labels=_labels(
            paths={"a": "Guide > Accumulators"}, summaries={"a": "TOPIC: previous value"}))
        with _cfg():
            r._rerank("q", {"a": long_text, "b": long_text}, keep=2)
        first, second = seen["passages"].split("\n\n")
        self.assertIn("[1] Guide > Accumulators", first)
        self.assertIn("Summary: TOPIC: previous value", first)
        self.assertLess(len(first), 600)
        # Without a summary, more of the text is shown.
        self.assertGreater(len(second), 1000)


class TestHybridSearchReranks(unittest.TestCase):
    def _search(self, cfg, keyword_matches=False):
        r, seen = _retriever(ranked=[3, 2, 1])
        r._generate_embedding = lambda q: [0.0]
        r._check_query_install = lambda name: None
        r._log_retrieval = lambda *a: None
        r.conn.runInstalledQuery.return_value = [{"final_retrieval": {"v1": ["one"], "v2": ["two"]}}]
        match = keyword_index.KeywordMatch(chunks=[("k1", 1.0)], query_terms=3)
        with mock.patch("common.config.get_graphrag_config", return_value=cfg), \
             _cfg(**cfg), \
             mock.patch("supportai.keyword_index.keyword_match", return_value=match), \
             mock.patch.object(keyword_index.KeywordMatch, "strength", new_callable=mock.PropertyMock,
                               return_value="weak"), \
             mock.patch("supportai.keyword_index.chunk_texts", return_value={"k1": "keyword"}):
            res = r.search("q", ["DocumentChunk"], top_k=1, keyword_matches=keyword_matches)
        params = r.conn.runInstalledQuery.call_args.kwargs["params"]
        return res[0], params

    def test_a_larger_pool_is_searched_and_ranked(self):
        out, params = self._search({"rerank": True, "keyword_search": True})
        self.assertEqual(params["top_k"], 3)
        self.assertEqual(params["max_results"], 6)
        # Weak keyword matches join the pool; the ranking call orders all.
        self.assertEqual(list(out["final_retrieval"]), ["k1", "v2"])
        self.assertTrue(out["reranked"])

    def test_keyword_search_turns_reranking_on(self):
        out, params = self._search({"keyword_search": True})
        self.assertEqual(params["top_k"], 3)
        self.assertTrue(out["reranked"])

    def test_without_rerank_the_search_is_unchanged(self):
        out, params = self._search({"keyword_search": True, "rerank": False}, keyword_matches=True)
        self.assertEqual(params["top_k"], 1)
        self.assertEqual(list(out["final_retrieval"]), ["v1", "v2"])
        self.assertNotIn("reranked", out)


class TestRerankSetting(unittest.TestCase):
    def test_follows_keyword_search_when_unset(self):
        from supportai.retrievers.HybridRetriever import rerank_enabled
        self.assertTrue(rerank_enabled({"keyword_search": True}))
        self.assertFalse(rerank_enabled({}))
        self.assertFalse(rerank_enabled({"keyword_search": True, "rerank": False}))
        self.assertTrue(rerank_enabled({"rerank": True}))


class TestLightReasoning(unittest.TestCase):
    def _google(self, model):
        from common.llm_services.google_genai_service import GoogleGenAI
        g = GoogleGenAI.__new__(GoogleGenAI)
        g.config = {"llm_model": model}
        g.llm = mock.Mock()
        g.llm.bind.side_effect = lambda **kw: kw
        return g

    def test_gemini_settings_per_model(self):
        cases = {
            "gemini-3.5-flash": {"thinking_level": "minimal"},
            "gemini-3-pro-preview": {"thinking_level": "low"},
            "gemini-2.5-flash": {"thinking_budget": 0},
            "gemini-2.5-pro": {"thinking_budget": 128},
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertEqual(self._google(model)._light_llm(), expected)

    def test_models_without_reasoning_are_unchanged(self):
        g = self._google("gemini-2.0-flash")
        self.assertIs(g._light_llm(), g.llm)

    def test_a_rejected_setting_falls_back_to_the_default(self):
        from langchain_core.prompts import PromptTemplate
        from langchain_core.output_parsers import StrOutputParser
        from langchain_core.runnables import RunnableLambda
        from langchain_core.messages import AIMessage
        from common.llm_services.base_llm import LLM_Model
        m = LLM_Model.__new__(LLM_Model)
        m.config = {}
        m.llm = RunnableLambda(lambda _: AIMessage(content="default answer"))
        m._light_llm = lambda: RunnableLambda(lambda _: (_ for _ in ()).throw(ValueError("bad setting")))
        out = m.invoke_with_parser(PromptTemplate.from_template("{q}"), StrOutputParser(),
                                   {"q": "x"}, caller_name="t", light=True)
        self.assertEqual(out, "default answer")
