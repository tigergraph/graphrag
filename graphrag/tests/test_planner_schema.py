"""The planner gets a compact, cached schema summary (GML-2331)."""
import types
import unittest
from unittest import mock

try:
    import agent.agentic_planner as ap
    import agent.agentic_graph as g
    from common.db.schema_utils import AllowedSchema
    from common.llm_services.base_llm import LLM_Model
    from common.py_schemas import GraphRAGResponse, Plan, PlanStep
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


def _rep(version=1):
    return AllowedSchema(
        schema_version=version,
        vertex_types=["Ticket", "Document"],
        edge_types=["ASSIGNED_TO", "HAS_CHILD"],
        vertex_attributes={
            "Ticket": {"status": "STRING", "created_at": "DATETIME", "epoch_added": "UINT",
                       "embedding": "LIST<FLOAT>"},
            "Document": {"epoch_added": "UINT"},
        },
        edge_attributes={"ASSIGNED_TO": {"since": "DATETIME"}},
        vertex_definitions={"Ticket": "A Jira ticket"},
        edge_definitions={"ASSIGNED_TO": "Who works on the ticket"},
        edge_endpoints={"ASSIGNED_TO": [("Ticket", "Person")], "HAS_CHILD": [("Document", "DocumentChunk")]},
    )


class TestSummary(unittest.TestCase):
    def test_types_attributes_endpoints_and_descriptions(self):
        text = ap.render_schema_summary(_rep())
        self.assertIn("- Ticket(status, created_at): A Jira ticket", text)
        self.assertIn("- Document()", text)
        self.assertIn("- ASSIGNED_TO: Ticket -> Person (since): Who works on the ticket", text)
        self.assertIn("- HAS_CHILD: Document -> DocumentChunk", text)
        self.assertNotIn("epoch_added", text)
        self.assertNotIn("embedding", text)

    def test_long_schema_is_cut_at_a_line(self):
        rep = _rep()
        rep.vertex_types = [f"Type{i}" for i in range(3000)]
        text = ap.render_schema_summary(rep)
        self.assertLessEqual(len(text), ap._SCHEMA_SUMMARY_MAX_CHARS + 30)
        self.assertTrue(text.endswith("(more types not shown)"))


class TestCache(unittest.TestCase):
    def setUp(self):
        ap._schema_cache.clear()
        self.conn = types.SimpleNamespace(host="h", graphname="g")

    def _summary(self, versions, now):
        with mock.patch("common.db.connections.get_schema_ver", side_effect=versions) as ver, \
             mock.patch("common.db.schema_utils.render_schema_rep",
                        side_effect=lambda c: _rep(version=versions_seen[-1])) as render, \
             mock.patch.object(ap.time, "time", return_value=now):
            return ap.schema_summary(self.conn), ver, render

    def test_read_once_then_cached(self):
        global versions_seen
        versions_seen = [1]
        _, ver, render = self._summary([1], 1000.0)
        self.assertEqual((ver.call_count, render.call_count), (1, 1))
        _, ver, render = self._summary([1], 1030.0)  # within the recheck window
        self.assertEqual((ver.call_count, render.call_count), (0, 0))

    def test_same_version_is_not_rebuilt(self):
        global versions_seen
        versions_seen = [1]
        self._summary([1], 1000.0)
        _, ver, render = self._summary([1], 1100.0)
        self.assertEqual((ver.call_count, render.call_count), (1, 0))

    def test_new_version_is_rebuilt(self):
        global versions_seen
        versions_seen = [1]
        self._summary([1], 1000.0)
        versions_seen = [2]
        _, ver, render = self._summary([2], 1100.0)
        self.assertEqual(render.call_count, 1)

    def test_read_error_keeps_the_last_summary(self):
        global versions_seen
        versions_seen = [1]
        first, _, _ = self._summary([1], 1000.0)
        with mock.patch("common.db.connections.get_schema_ver", side_effect=RuntimeError("down")), \
             mock.patch.object(ap.time, "time", return_value=1100.0):
            self.assertEqual(ap.schema_summary(self.conn), first)

    def test_no_connection(self):
        self.assertEqual(ap.schema_summary(None), "")


class TestPlannerGetsTheSchema(unittest.TestCase):
    def _user_text(self, schema):
        seen = {}

        def invoke(messages, schema_cls, **kw):
            seen["user"] = messages[-1][1]
            return Plan(steps=[PlanStep(id="A", kind="answer", tool="")])

        llm = types.SimpleNamespace(agentic_planner_prompt="sys", config={}, invoke_structured=invoke)
        ap.plan_question(llm, "How many tickets?", [], schema_rep=schema)
        return seen["user"]

    def test_schema_in_the_planner_input(self):
        self.assertIn("## Graph schema\nVertex types", self._user_text("Vertex types (attributes):\n- Ticket(status)"))

    def test_missing_schema_is_stated(self):
        self.assertIn("## Graph schema\nNot available", self._user_text(""))

    def test_engine_passes_the_schema_to_every_plan(self):
        ctx = types.SimpleNamespace(graphrag_cfg={}, emit=lambda *a, **k: None, conn=object())
        answer_only = Plan(steps=[PlanStep(id="A", kind="answer", tool="")])
        with mock.patch.object(g, "schema_summary", return_value="SCHEMA") as summ, \
             mock.patch.object(g, "plan_question", return_value=answer_only) as planner, \
             mock.patch.object(g, "check_conversation", return_value=None), \
             mock.patch.object(g, "synthesize", return_value=GraphRAGResponse(
                 natural_language_response="ok", answered_question=True, response_type="agentic")):
            resp = g.run_agentic(ctx, llm=object(), question="q")
        summ.assert_called_once()
        self.assertEqual(planner.call_args.kwargs["schema_rep"], "SCHEMA")
        self.assertEqual(resp.query_sources["agent_steps"][0]["input"]["graph_schema"], "SCHEMA")

    def test_prompt_uses_the_schema(self):
        prompt = LLM_Model._AGENTIC_PLANNER_SYSTEM
        self.assertIn("## Graph schema lists the vertex types", prompt)
        self.assertNotIn("NOT provided", prompt)


versions_seen = [1]

if __name__ == "__main__":
    unittest.main()
