"""Unit tests for the Deep Agent chat engine (deep_agent package).

A scripted, tool-calling fake chat model drives the real Deep Agent graph
(LangGraph + deepagents middleware) against in-memory STM/LTM backends and
fake TigerGraph / GraphRAG tool layers, so no LLM, database or server config
is needed.
"""
import json
import sys
import types
import unittest
from unittest import mock
from types import SimpleNamespace

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, SystemMessage

from deep_agent import memory as mem
from deep_agent.agent import DeepGraphRAGAgent, history_messages
from deep_agent import settings
from deep_agent.settings import use_deep_agent
from deep_agent.tools import ToolEnv, build_tools, query_is_runnable


# --------------------------------------------------------------- fakes

class ScriptedModel(GenericFakeChatModel):
    """Replays scripted AI messages and records the prompts it was given."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        type(self).seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def call(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def answer(text, citations=(), answered=True, cid="final"):
    return AIMessage(content="", tool_calls=[call(
        "DeepAgentAnswer",
        {"answer": text, "citations": list(citations), "answered_question": answered}, cid)])


class FakeConn:
    graphname = "TestGraph"
    username = "alice"
    auth_mode = "pwd"

    def __init__(self):
        self.calls = []

    def getVertexCount(self, vertex_type, where=""):
        self.calls.append(("getVertexCount", vertex_type, where))
        return {"Person": 3} if vertex_type == "*" else 3

    def getEndpoints(self, dynamic=True):
        return {"GET /query/TestGraph/top_customers": {}, "POST /query/TestGraph/graphrag_delete_all_communities": {},
                "GET /query/Other/foreign": {}}

    def getQueryDescription(self, names):
        return [{"queryName": n, "description": "d", "parameters": []} for n in names]

    def runInstalledQuery(self, name, params=None):
        self.calls.append(("runInstalledQuery", name, params))
        return [{"rows": [1, 2]}]


def fake_graphrag_tools_module():
    """Stand-in for tools.graphrag_tools (which needs the server config)."""
    mod = types.ModuleType("tools.graphrag_tools")

    def search(ctx, question, **kw):
        return {"ok": True, "summary": "1 chunk",
                "context": {"function_call": "x", "result": {"final_retrieval": {
                    "doc1_chunk_0": f"Passage about {question}"}}}}

    mod.hybrid_search = mod.similarity_search = mod.contextual_search = mod.community_search = search
    mod.get_schema = lambda ctx: {"ok": True, "summary": "schema", "context": {
        "schema_rep": "Person(name)", "vertex_types": ["Person"], "edge_types": [], "schema_version": 1}}
    mod.structural_retrieve = lambda ctx, q: {"ok": True, "summary": "1 row",
                                              "context": {"result": [{"n": 3}]}, "query": "getVertexCount('Person')"}
    pkg = types.ModuleType("tools")
    pkg.graphrag_tools = mod
    pkg.__path__ = []
    return {"tools": pkg, "tools.graphrag_tools": mod}


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: sys.modules.get(k) for k in ("tools", "tools.graphrag_tools")}
        sys.modules.update(fake_graphrag_tools_module())
        mem.reset_backends_for_tests(mem._memory_backends())
        ScriptedModel.seen = []

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        mem.reset_backends_for_tests(None)

    def make_agent(self, script, conversation_id="c1", user="alice", conn=None, cfg=None):
        conn = conn or FakeConn()
        llm = SimpleNamespace(llm=ScriptedModel(messages=iter(script)), config={},
                              get_user_portion=lambda name: "")
        agent = DeepGraphRAGAgent(
            llm, conn, SimpleNamespace(model_name="emb"), SimpleNamespace(conn=conn),
            conversation_id=conversation_id, user=user, ws=object(),
            graphrag_cfg=cfg if cfg is not None else {"deep_agent": {"subagents": False}},
        )
        agent._graph_tool_context = lambda convo: SimpleNamespace(conn=conn, emit=lambda m: None)
        agent._external_tools = lambda ctx: {}
        return agent


# --------------------------------------------------------------- tests

class TestDeepAgentRun(_Base):
    def test_tool_call_then_structured_answer(self):
        agent = self.make_agent([
            AIMessage(content="", tool_calls=[call("hybrid_search", {"query": "Acme revenue"}, "t1")]),
            answer("Acme earned $5M [doc1_chunk_0].", ["doc1_chunk_0"]),
        ])
        resp = agent.question_for_agent("What did Acme earn?")
        self.assertEqual(resp.natural_language_response, "Acme earned $5M [doc1_chunk_0].")
        self.assertTrue(resp.answered_question)
        self.assertEqual(resp.response_type, "deep_agent")
        qs = resp.query_sources
        self.assertEqual(qs["engine"], "deep_agent")
        self.assertEqual(qs["citations"], ["doc1_chunk_0"])
        self.assertEqual(qs["retrieved_citations"], ["doc1_chunk_0"])
        self.assertTrue(qs["reasoning"][0].startswith("* doc1_chunk_0"))
        self.assertEqual(qs["agent_steps"][0]["node"], "1: hybrid_search")
        self.assertEqual(qs["agent_steps"][-1]["kind"], "answer")
        # The tool result reached the model as the next observation.
        tool_msgs = [m for m in ScriptedModel.seen[-1] if m.type == "tool"]
        self.assertIn("Passage about Acme revenue", tool_msgs[0].content)
        # Progress reached the UI queue, ending with DONE.
        items = []
        while (i := agent.q.pop()) is not None:
            items.append(i)
        self.assertIn("Searching the documents", items)
        self.assertEqual(items[-1], "DONE")

    def test_tigergraph_tool_runs_on_user_connection(self):
        conn = FakeConn()
        agent = self.make_agent([
            AIMessage(content="", tool_calls=[call("get_vertex_count", {"vertex_type": "Person"}, "t1")]),
            answer("There are 3 people."),
        ], conn=conn)
        resp = agent.question_for_agent("How many people?")
        self.assertEqual(conn.calls, [("getVertexCount", "Person", "")])
        self.assertEqual(resp.natural_language_response, "There are 3 people.")

    def test_short_term_memory_continues_thread(self):
        self.make_agent([answer("Hi Alice!")]).question_for_agent("Hello, I'm Alice")
        ScriptedModel.seen = []
        self.make_agent([answer("You are Alice.")]).question_for_agent("Who am I?")
        texts = [str(m.content) for m in ScriptedModel.seen[0]]
        self.assertTrue(any("Hello, I'm Alice" in t for t in texts))
        self.assertTrue(any("Hi Alice!" in t for t in texts))

    def test_threads_are_isolated_by_user_and_conversation(self):
        self.make_agent([answer("noted")]).question_for_agent("secret plan")
        ScriptedModel.seen = []
        self.make_agent([answer("no")], user="bob").question_for_agent("what was said?")
        self.make_agent([answer("no")], conversation_id="c2").question_for_agent("what was said?")
        for prompt in ScriptedModel.seen:
            self.assertFalse(any("secret plan" in str(m.content) for m in prompt))

    def test_history_seeds_a_new_thread_only(self):
        hist = [{"query": "Q1", "response": "A1"}]
        self.make_agent([answer("ok")]).question_for_agent("Q2", hist)
        ScriptedModel.seen = []
        self.make_agent([answer("ok")]).question_for_agent("Q3", hist)
        texts = [str(m.content) for m in ScriptedModel.seen[0]]
        self.assertEqual(sum("Q1" == t for t in texts), 1)   # not seeded twice
        self.assertEqual([m.content for m in history_messages(hist)], ["Q1", "A1"])

    def test_long_term_memory_persists_across_conversations(self):
        self.make_agent([
            AIMessage(content="", tool_calls=[call("write_file", {
                "file_path": "/memories/AGENTS.md", "content": "- Prefers answers in bullet points"}, "w1")]),
            answer("I'll remember that."),
        ]).question_for_agent("Always answer in bullet points")
        self.assertIn("bullet points", mem.read_memory("TestGraph", "alice", "/memories/AGENTS.md"))
        self.assertEqual([m["path"] for m in mem.list_memories("TestGraph", "alice")], ["/memories/AGENTS.md"])

        ScriptedModel.seen = []
        self.make_agent([answer("ok")], conversation_id="c-new").question_for_agent("hi")
        system = " ".join(str(m.content) for m in ScriptedModel.seen[0] if isinstance(m, SystemMessage))
        self.assertIn("Prefers answers in bullet points", system)

        # Another user does not see it.
        ScriptedModel.seen = []
        self.make_agent([answer("ok")], user="bob").question_for_agent("hi")
        system = " ".join(str(m.content) for m in ScriptedModel.seen[0] if isinstance(m, SystemMessage))
        self.assertNotIn("bullet points", system)

    def test_token_connection_gets_no_persistent_memory(self):
        conn = FakeConn()
        conn.auth_mode = "token"
        agent = self.make_agent([answer("ok")], user=None, conn=conn)
        self.assertIsNone(agent.user)
        self.assertIsNone(agent.conversation_id)
        resp = agent.question_for_agent("hi")
        self.assertIsNone(resp.query_sources["thread_id"])
        self.assertEqual(mem.list_threads("TestGraph", "tigergraph"), [])
        system = " ".join(str(m.content) for m in ScriptedModel.seen[0] if isinstance(m, SystemMessage))
        self.assertNotIn("/memories/AGENTS.md is loaded", system)

    def test_answer_is_from_this_turn_not_the_previous_one(self):
        self.make_agent([answer("first answer")]).question_for_agent("q1")
        resp = self.make_agent([AIMessage(content="second answer")]).question_for_agent("q2")
        self.assertEqual(resp.natural_language_response, "second answer")

    def test_unknown_or_sentinel_user_gets_no_memory(self):
        for user in ("", "__GSQL__secret", "__graphrag_token__"):
            agent = self.make_agent([answer("ok")], user=user)
            self.assertIsNone(agent.user)
            self.assertIsNone(agent.conversation_id)
        conn = FakeConn()
        conn.username = "__GSQL__secret"
        self.assertIsNone(self.make_agent([answer("ok")], user=None, conn=conn).user)
        self.assertEqual(self.make_agent([answer("ok")], user=None).user, "alice")

    def test_plain_text_final_message_is_accepted(self):
        resp = self.make_agent([AIMessage(content="Hello there")]).question_for_agent("hi")
        self.assertEqual(resp.natural_language_response, "Hello there")


class TestSubagents(_Base):
    def test_delegates_to_document_researcher(self):
        agent = self.make_agent([
            AIMessage(content="", tool_calls=[call("task", {
                "description": "Find Acme revenue in the documents",
                "subagent_type": "document-researcher"}, "t1")]),
            # subagent turn(s), same scripted model
            AIMessage(content="", tool_calls=[call("hybrid_search", {"query": "Acme revenue"}, "s1")]),
            AIMessage(content="Acme earned $5M (doc1_chunk_0)."),
            answer("Acme earned $5M.", ["doc1_chunk_0"]),
        ], cfg={})
        resp = agent.question_for_agent("What did Acme earn?")
        self.assertEqual(resp.natural_language_response, "Acme earned $5M.")
        self.assertEqual([s["node"] for s in resp.query_sources["agent_steps"]],
                         ["1: hybrid_search", "2: answer"])
        self.assertEqual(resp.query_sources["retrieved_citations"], ["doc1_chunk_0"])


class TestRouter(_Base):
    def test_thread_and_memory_routes(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import importlib.util
        import os
        # Load the router module alone: the routers package imports every
        # router and their full dependency set.
        path = os.path.join(os.path.dirname(__file__), "..", "app", "routers", "deep_agent.py")
        spec = importlib.util.spec_from_file_location("deep_agent_router_under_test", path)
        r = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(r)

        r._access = lambda request, graphname: (FakeConn(), "alice")
        app = FastAPI()
        app.include_router(r.router)
        client = TestClient(app)

        self.make_agent([answer("Hi!")], conversation_id="conv9").question_for_agent("Hello")
        threads = client.get("/TestGraph/agent/threads").json()["threads"]
        self.assertEqual([t["conversation_id"] for t in threads], ["conv9"])
        msgs = client.get("/TestGraph/agent/threads/conv9").json()["messages"]
        self.assertEqual(msgs[0], {"type": "human", "content": "Hello"})
        self.assertEqual(client.get("/TestGraph/agent/threads/nope").status_code, 404)

        self.assertEqual(client.put("/TestGraph/agent/memories/AGENTS.md",
                                    json={"content": "- likes tables"}).json()["path"],
                         "/memories/AGENTS.md")
        self.assertEqual(client.get("/TestGraph/agent/memories/AGENTS.md").json()["content"],
                         "- likes tables")
        self.assertEqual(len(client.get("/TestGraph/agent/memories").json()["memories"]), 1)
        self.assertEqual(client.delete("/TestGraph/agent/memories/AGENTS.md").status_code, 200)
        self.assertEqual(client.delete("/TestGraph/agent/threads/conv9").status_code, 200)
        self.assertEqual(client.get("/TestGraph/agent/threads").json()["threads"], [])

        r._access = lambda request, graphname: (FakeConn(), None)
        self.assertEqual(client.get("/TestGraph/agent/memories").status_code, 400)


class TestTools(_Base):
    def test_tool_catalog(self):
        env = ToolEnv(graph_ctx=SimpleNamespace(conn=FakeConn()), use_cypher=True)
        groups = build_tools(env)
        names = {f: [t.name for t in ts] for f, ts in groups.items()}
        self.assertEqual(names["graphrag"], ["get_graph_schema", "query_graph", "hybrid_search",
                                             "similarity_search", "contextual_search", "community_search"])
        self.assertIn("run_cypher_query", names["tigergraph"])
        self.assertIn("run_installed_query", names["tigergraph"])
        env2 = ToolEnv(graph_ctx=SimpleNamespace(conn=FakeConn()), allow_installed_queries=False)
        self.assertNotIn("run_installed_query", [t.name for t in build_tools(env2)["tigergraph"]])

    def test_installed_queries_exclude_graphrag_internal_and_mutating(self):
        self.assertFalse(query_is_runnable("graphrag_delete_all_communities", ["*"]))
        self.assertFalse(query_is_runnable("StreamIds", ["*"]))
        self.assertFalse(query_is_runnable("delete_customer", ["*"]))
        self.assertTrue(query_is_runnable("top_customers", ["*"]))
        self.assertFalse(query_is_runnable("top_customers", ["report_*"]))

        conn = FakeConn()
        env = ToolEnv(graph_ctx=SimpleNamespace(conn=conn))
        tools = {t.name: t for t in build_tools(env)["tigergraph"]}
        listed = json.loads(tools["list_installed_queries"].invoke({}))
        self.assertEqual([q["name"] for q in listed["queries"]], ["top_customers"])
        out = json.loads(tools["run_installed_query"].invoke(
            {"query_name": "graphrag_delete_all_communities", "params": {}}))
        self.assertFalse(out["ok"])
        self.assertEqual(conn.calls, [])

    def test_mcp_tools_are_wrapped(self):
        seen = {}

        def fn(ctx, **kwargs):
            seen.update(kwargs)
            return {"ok": True, "summary": "sunny", "context": {"temp": 21}}

        spec = SimpleNamespace(fn=fn, description="Weather by city", args_schema_json={
            "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]})
        env = ToolEnv(graph_ctx=SimpleNamespace(conn=FakeConn()))
        tool = build_tools(env, {"weather.get_forecast": spec})["mcp"][0]
        self.assertEqual(tool.name, "weather__get_forecast")
        out = json.loads(tool.invoke({"city": "Oslo"}))
        self.assertEqual(seen, {"city": "Oslo"})
        self.assertEqual(out["context"], {"temp": 21})
        self.assertEqual(env.trace[0]["tool"], "weather__get_forecast")

    def test_tool_output_is_capped(self):
        env = ToolEnv(graph_ctx=SimpleNamespace(conn=FakeConn()), max_output_chars=2000)
        mod = sys.modules["tools.graphrag_tools"]
        mod.hybrid_search = lambda ctx, q, **kw: {"ok": True, "summary": "big", "context": {
            "result": {"final_retrieval": {"c": "x" * 10000}}}}
        tool = {t.name: t for t in build_tools(env)["graphrag"]}["hybrid_search"]
        out = tool.invoke({"query": "q"})
        self.assertLess(len(out), 2300)
        self.assertIn("truncated", out)

    def test_tool_errors_are_returned_not_raised(self):
        conn = FakeConn()
        conn.getVertexCount = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        env = ToolEnv(graph_ctx=SimpleNamespace(conn=conn))
        tool = {t.name: t for t in build_tools(env)["tigergraph"]}["get_vertex_count"]
        out = json.loads(tool.invoke({"vertex_type": "Person"}))
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["summary"])
        self.assertFalse(env.trace[0]["ok"])


class TestMemoryStore(_Base):
    def test_memory_crud_and_path_safety(self):
        p = mem.write_memory("G", "u", "notes/prefs.md", "x")
        self.assertEqual(p, "/memories/notes/prefs.md")
        self.assertEqual(mem.read_memory("G", "u", "/memories/notes/prefs.md"), "x")
        self.assertIsNone(mem.read_memory("G", "v", "notes/prefs.md"))
        with self.assertRaises(ValueError):
            mem.write_memory("G", "u", "../escape", "x")
        self.assertTrue(mem.delete_memory("G", "u", "notes/prefs.md"))
        self.assertFalse(mem.delete_memory("G", "u", "notes/prefs.md"))

    def test_thread_ids_cannot_collide_across_users(self):
        self.assertNotEqual(mem.thread_id("G", "a:b", "c"), mem.thread_id("G", "a", "b:c"))
        self.assertTrue(mem.thread_id("G", "u", "c").startswith(mem.thread_prefix("G", "u")))

    def test_sqlite_backend_persists(self):
        import os
        import tempfile
        from deep_agent.settings import MemorySettings
        with tempfile.TemporaryDirectory() as d:
            mem.reset_backends_for_tests(None)
            path = os.path.join(d, "m.sqlite")
            b = mem.init_backends(MemorySettings(backend="sqlite", path=path))
            self.assertEqual(b.kind, "sqlite")
            mem.write_memory("G", "u", "AGENTS.md", "hello")
            mem.close_backends()
            mem.init_backends(MemorySettings(backend="sqlite", path=path))
            self.assertEqual(mem.read_memory("G", "u", "AGENTS.md"), "hello")
            mem.close_backends()


class TestAgentEngineSetting(unittest.TestCase):
    def test_default_engine_is_the_deep_agent(self):
        for cfg in (None, {}, {"agent_engine": None}, {"agent_engine": ""},
                    {"agent_engine": "default"}, {"agent_engine": " Default "},
                    {"agent_engine": "deepagent"}, {"agent_engine": "DeepAgent"}):
            self.assertTrue(use_deep_agent(cfg), cfg)

    def test_legacy_selects_the_previous_engine(self):
        self.assertFalse(use_deep_agent({"agent_engine": "legacy"}))
        self.assertFalse(use_deep_agent({"agent_engine": "LEGACY"}))

    def test_unknown_value_uses_the_default(self):
        self.assertTrue(use_deep_agent({"agent_engine": "deep"}))

    def test_named_engines_do_not_follow_the_default(self):
        with mock.patch.object(settings, "DEFAULT_AGENT_ENGINE", settings.AGENT_ENGINE_LEGACY):
            self.assertFalse(use_deep_agent({}))
            self.assertFalse(use_deep_agent({"agent_engine": "default"}))
            self.assertTrue(use_deep_agent({"agent_engine": "deepagent"}))


if __name__ == "__main__":
    unittest.main()
