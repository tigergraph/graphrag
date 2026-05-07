# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock

# Load `method_selector.py` directly rather than via `app.agent.method_selector`
# because the `app.agent` package's __init__ pulls in agent_graph.py, which
# transitively imports boto3 and other heavy runtime dependencies the selector
# itself does not need. Importing the file in isolation keeps these tests
# tightly scoped to the module under test.
_HERE = os.path.dirname(os.path.abspath(__file__))
_MS_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "app", "agent", "method_selector.py")
)
_spec = importlib.util.spec_from_file_location("method_selector", _MS_PATH)
method_selector = importlib.util.module_from_spec(_spec)
sys.modules["method_selector"] = method_selector
_spec.loader.exec_module(method_selector)

METHOD_COMMUNITY = method_selector.METHOD_COMMUNITY
METHOD_CONTEXTUAL = method_selector.METHOD_CONTEXTUAL
METHOD_HYBRID = method_selector.METHOD_HYBRID
METHOD_SIMILARITY = method_selector.METHOD_SIMILARITY
FALLBACK_METHOD = method_selector.FALLBACK_METHOD
CHUNK_BASED_METHODS = method_selector.CHUNK_BASED_METHODS
INLANE_FALLBACK_TABLE = method_selector.INLANE_FALLBACK_TABLE
RetrieverChoice = method_selector.RetrieverChoice
RetrieverSelector = method_selector.RetrieverSelector
_LLMRetrieverChoice = method_selector._LLMRetrieverChoice
rules_choose = method_selector.rules_choose
has_insufficient_context = method_selector.has_insufficient_context


# ---------- Stage A: rules_choose ----------


class TestRulesChooseCommunity(unittest.TestCase):
    """Global / thematic phrasing → community."""

    def test_summarize(self):
        self.assertEqual(rules_choose("Summarize the documents").method, METHOD_COMMUNITY)

    def test_main_themes(self):
        self.assertEqual(
            rules_choose("What are the main themes in this corpus?").method,
            METHOD_COMMUNITY,
        )

    def test_what_topics(self):
        self.assertEqual(
            rules_choose("Which topics are covered?").method, METHOD_COMMUNITY
        )

    def test_corpus_about(self):
        self.assertEqual(
            rules_choose("What are these documents about?").method, METHOD_COMMUNITY
        )

    def test_overview_of(self):
        self.assertEqual(
            rules_choose("Give me an overview of the dataset").method, METHOD_COMMUNITY
        )


class TestRulesChooseContextual(unittest.TestCase):
    """Process / narrative phrasing → contextual."""

    def test_walk_me_through(self):
        self.assertEqual(
            rules_choose("Walk me through the deployment process").method,
            METHOD_CONTEXTUAL,
        )

    def test_step_by_step(self):
        self.assertEqual(
            rules_choose("Show me step-by-step how onboarding works").method,
            METHOD_CONTEXTUAL,
        )

    def test_what_happens_after(self):
        self.assertEqual(
            rules_choose("What happens after the user logs in?").method,
            METHOD_CONTEXTUAL,
        )

    def test_explain_the_process(self):
        self.assertEqual(
            rules_choose("Explain the process of approval").method, METHOD_CONTEXTUAL
        )

    def test_how_does_it_work(self):
        self.assertEqual(
            rules_choose("How does it work?").method, METHOD_CONTEXTUAL
        )


class TestRulesChooseHybrid(unittest.TestCase):
    """Relational phrasing → hybrid."""

    def test_how_is_x_related_to_y(self):
        self.assertEqual(
            rules_choose("How is Acme related to Globex?").method, METHOD_HYBRID
        )

    def test_relationship_between(self):
        self.assertEqual(
            rules_choose("What is the relationship between Bob and Alice?").method,
            METHOD_HYBRID,
        )

    def test_connection_between(self):
        self.assertEqual(
            rules_choose("Show the connection between fraud and accounts").method,
            METHOD_HYBRID,
        )

    def test_report_to(self):
        self.assertEqual(
            rules_choose("Who does Alice report to?").method, METHOD_HYBRID
        )


class TestRulesChooseSimilarity(unittest.TestCase):
    """Short factoid / lookup → similarity."""

    def test_what_is_short(self):
        choice = rules_choose("What is GraphRAG?")
        self.assertIsNotNone(choice)
        self.assertEqual(choice.method, METHOD_SIMILARITY)

    def test_who_is(self):
        self.assertEqual(rules_choose("Who is the CEO?").method, METHOD_SIMILARITY)

    def test_define(self):
        self.assertEqual(rules_choose("Define embedding").method, METHOD_SIMILARITY)

    def test_long_factoid_falls_through(self):
        # Over the 12-token cap → similarity rule does not fire; nothing else
        # matches → falls through to the LLM stage (rules_choose returns None).
        long_q = (
            "What is the deeper conceptual significance of vector similarity "
            "search in modern enterprise knowledge management systems?"
        )
        self.assertIsNone(rules_choose(long_q))


class TestRulesChooseEdgeCases(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(rules_choose(""))

    def test_whitespace(self):
        self.assertIsNone(rules_choose("   \n  "))

    def test_unmatched_question(self):
        # No pattern matches "tell me about" — falls through to the LLM.
        self.assertIsNone(rules_choose("Tell me about distributed systems"))

    def test_priority_community_over_factoid(self):
        """Community language wins over a factoid `what is` opener."""
        self.assertEqual(
            rules_choose("What is the main theme of these documents?").method,
            METHOD_COMMUNITY,
        )


# ---------- Stage B: RetrieverSelector.choose ----------


def _make_llm_mock():
    """Mock that satisfies what RetrieverSelector reads on the LLM."""
    llm = MagicMock()
    # PromptTemplate validates the template, so use a real string with the
    # placeholders the selector wires in.
    llm.select_retriever_prompt = (
        "Question: {question}\n"
        "Schema: {v_types} {e_types}\n"
        "History: {conversation}\n"
        "{format_instructions}"
    )
    return llm


def _make_db_mock(v_types=None, e_types=None):
    db = MagicMock()
    db.getVertexTypes.return_value = v_types or ["Entity", "Document"]
    db.getEdgeTypes.return_value = e_types or ["RELATIONSHIP"]
    return db


class TestRetrieverSelectorRulesPath(unittest.TestCase):
    """When rules fire, the LLM stage must NOT be invoked."""

    def test_rules_short_circuit_skips_llm(self):
        llm = _make_llm_mock()
        db = _make_db_mock()
        selector = RetrieverSelector(llm, db)

        choice = selector.choose("Summarize the corpus")
        self.assertEqual(choice.method, METHOD_COMMUNITY)
        self.assertEqual(choice.source, "rules")
        # LLM call must not have happened
        llm.invoke_with_parser.assert_not_called()


class TestRetrieverSelectorLLMPath(unittest.TestCase):
    def test_llm_returns_method_label_normalized(self):
        llm = _make_llm_mock()
        db = _make_db_mock()
        # LLM returns the user-facing label "hybrid"; selector must canonicalize
        # to the dispatcher string "hybridsearch".
        llm.invoke_with_parser.return_value = _LLMRetrieverChoice(
            method="hybrid", reason="needs to relate two entities"
        )
        selector = RetrieverSelector(llm, db)

        choice = selector.choose("Tell me about Alice and Bob's collaboration")
        self.assertEqual(choice.method, METHOD_HYBRID)
        self.assertEqual(choice.source, "llm")
        self.assertEqual(choice.reason, "needs to relate two entities")
        llm.invoke_with_parser.assert_called_once()

    def test_llm_returns_each_label(self):
        for label, method in [
            ("similarity", METHOD_SIMILARITY),
            ("contextual", METHOD_CONTEXTUAL),
            ("hybrid", METHOD_HYBRID),
            ("community", METHOD_COMMUNITY),
        ]:
            with self.subTest(label=label):
                llm = _make_llm_mock()
                db = _make_db_mock()
                llm.invoke_with_parser.return_value = _LLMRetrieverChoice(
                    method=label, reason="reason"
                )
                selector = RetrieverSelector(llm, db)
                choice = selector.choose("Tell me about distributed consensus")
                self.assertEqual(choice.method, method)


class TestRetrieverSelectorFallback(unittest.TestCase):
    def test_llm_raises_falls_back_to_hybrid(self):
        llm = _make_llm_mock()
        db = _make_db_mock()
        llm.invoke_with_parser.side_effect = RuntimeError("LLM unavailable")
        selector = RetrieverSelector(llm, db)

        choice = selector.choose("Tell me about distributed systems")
        self.assertEqual(choice.method, FALLBACK_METHOD)
        self.assertEqual(choice.source, "fallback")
        self.assertIn("RuntimeError", choice.reason)

    def test_schema_lookup_failure_does_not_break_selector(self):
        """If the DB schema fetch fails, the LLM stage should still proceed
        with empty type lists rather than aborting the whole selection."""
        llm = _make_llm_mock()
        db = MagicMock()
        db.getVertexTypes.side_effect = RuntimeError("db down")
        db.getEdgeTypes.side_effect = RuntimeError("db down")
        llm.invoke_with_parser.return_value = _LLMRetrieverChoice(
            method="hybrid", reason="default"
        )
        selector = RetrieverSelector(llm, db)

        choice = selector.choose("Tell me about distributed systems")
        self.assertEqual(choice.method, METHOD_HYBRID)
        self.assertEqual(choice.source, "llm")


class TestRetrieverChoice(unittest.TestCase):
    """The public choice model is a Pydantic BaseModel — verify its shape."""

    def test_fields(self):
        c = RetrieverChoice(method="hybridsearch", reason="r", source="rules")
        self.assertEqual(c.method, "hybridsearch")
        self.assertEqual(c.reason, "r")
        self.assertEqual(c.source, "rules")


# ---------- In-lane fallback table + has_insufficient_context ----------


class TestChunkBasedMethods(unittest.TestCase):
    """The CHUNK_BASED_METHODS set governs both the insufficient-context check
    and the in-lane fallback trigger; community must be excluded."""

    def test_chunk_methods_membership(self):
        self.assertIn(METHOD_SIMILARITY, CHUNK_BASED_METHODS)
        self.assertIn(METHOD_CONTEXTUAL, CHUNK_BASED_METHODS)
        self.assertIn(METHOD_HYBRID, CHUNK_BASED_METHODS)

    def test_community_excluded(self):
        self.assertNotIn(METHOD_COMMUNITY, CHUNK_BASED_METHODS)


class TestInlaneFallbackTable(unittest.TestCase):
    """Subset-aware: a fallback method must NOT be a strict subset of the
    method it's falling back from. Specifically, similarity is a subset of
    contextual and hybrid, so neither can fall back to similarity."""

    def test_similarity_falls_back_to_hybrid(self):
        self.assertEqual(INLANE_FALLBACK_TABLE[METHOD_SIMILARITY], METHOD_HYBRID)

    def test_contextual_does_not_fall_back_to_similarity(self):
        self.assertNotEqual(INLANE_FALLBACK_TABLE[METHOD_CONTEXTUAL], METHOD_SIMILARITY)

    def test_hybrid_does_not_fall_back_to_similarity(self):
        self.assertNotEqual(INLANE_FALLBACK_TABLE[METHOD_HYBRID], METHOD_SIMILARITY)

    def test_contextual_falls_back_to_hybrid(self):
        # Different expansion shape (graph hops vs siblings); not a subset.
        self.assertEqual(INLANE_FALLBACK_TABLE[METHOD_CONTEXTUAL], METHOD_HYBRID)

    def test_hybrid_falls_back_to_community(self):
        # Different retrieval surface (community summaries vs chunks).
        self.assertEqual(INLANE_FALLBACK_TABLE[METHOD_HYBRID], METHOD_COMMUNITY)

    def test_community_has_no_fallback(self):
        # Community's top-k semantics differ; the in-lane trigger doesn't fire
        # for it, so a fallback entry would be unused.
        self.assertNotIn(METHOD_COMMUNITY, INLANE_FALLBACK_TABLE)

    def test_no_self_fallback(self):
        # A method should never fall back to itself.
        for method, fallback in INLANE_FALLBACK_TABLE.items():
            self.assertNotEqual(method, fallback, f"{method} falls back to itself")


class TestHasInsufficientContext(unittest.TestCase):
    """`has_insufficient_context` decides whether to trigger in-lane fallback.
    Only chunk-based methods qualify; community is excluded."""

    def test_empty_chunk_method_is_insufficient(self):
        self.assertTrue(has_insufficient_context({}, METHOD_HYBRID, top_k=5))
        self.assertTrue(has_insufficient_context({}, METHOD_SIMILARITY, top_k=5))
        self.assertTrue(has_insufficient_context({}, METHOD_CONTEXTUAL, top_k=5))

    def test_none_chunk_method_is_insufficient(self):
        # Treats malformed/missing input as insufficient.
        self.assertTrue(has_insufficient_context(None, METHOD_HYBRID, top_k=5))

    def test_partial_below_top_k_is_insufficient(self):
        partial = {f"chunk{i}": "text" for i in range(3)}
        self.assertTrue(has_insufficient_context(partial, METHOD_HYBRID, top_k=5))

    def test_full_top_k_is_sufficient(self):
        full = {f"chunk{i}": "text" for i in range(5)}
        self.assertFalse(has_insufficient_context(full, METHOD_HYBRID, top_k=5))

    def test_above_top_k_is_sufficient(self):
        above = {f"chunk{i}": "text" for i in range(7)}
        self.assertFalse(has_insufficient_context(above, METHOD_HYBRID, top_k=5))

    def test_community_always_returns_false(self):
        """Community has different top_k semantics (community summaries, not
        chunks). It should never trigger the insufficient-context path."""
        self.assertFalse(has_insufficient_context({}, METHOD_COMMUNITY, top_k=5))
        self.assertFalse(has_insufficient_context(None, METHOD_COMMUNITY, top_k=5))
        partial = {f"comm{i}": "summary" for i in range(2)}
        self.assertFalse(has_insufficient_context(partial, METHOD_COMMUNITY, top_k=5))

    def test_unknown_method_returns_false(self):
        # An unknown method is not chunk-based; conservative: don't trigger.
        self.assertFalse(has_insufficient_context({}, "somethingelse", top_k=5))

    def test_top_k_one_edge_case(self):
        # With top_k=1, a single chunk is sufficient.
        self.assertFalse(has_insufficient_context({"a": "x"}, METHOD_HYBRID, top_k=1))
        self.assertTrue(has_insufficient_context({}, METHOD_HYBRID, top_k=1))


if __name__ == "__main__":
    unittest.main()
