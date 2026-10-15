"""A failed answer asks to rephrase the question, and the trace log records
the error without sending it to the chat (GML-2328)."""
import json
import os
import tempfile
import unittest
from unittest import mock

try:
    from routers import ui
    from common.py_schemas import GraphRAGResponse
except Exception as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


def _failed(steps=None, error="RuntimeError: quota exceeded"):
    resp = GraphRAGResponse(natural_language_response=ui._agent_error_text(RuntimeError("x")),
                            answered_question=False, response_type="agentic",
                            query_sources={"agent_steps": steps} if steps else {})
    resp._error = error
    return resp


class TestErrorMessage(unittest.TestCase):
    def test_message_asks_to_rephrase_the_question(self):
        text = ui._agent_error_text(RuntimeError("boom"))
        self.assertIn("rephrase your question", text)
        self.assertNotIn("prompt", text)
        self.assertNotIn("boom", text)

    def test_admin_gets_a_reference_not_the_error(self):
        text = ui._agent_error_text(RuntimeError("secret-url"), is_superadmin=True)
        self.assertIn("Admin reference ID", text)
        self.assertNotIn("secret-url", text)

    def test_task_group_error_is_unwrapped(self):
        group = ExceptionGroup("unhandled errors in a TaskGroup", [ValueError("real cause")])
        self.assertEqual(ui._trace_error_text(group), "ValueError: real cause")


class TestTraceLogRecordsTheError(unittest.TestCase):
    def _save(self, resp):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ui, "TRACE_LOGS_DIR", d):
            ui._save_trace_log("m1", "c1", "the question", resp, 1.0, "alice")
            with open(os.path.join(d, "m1.json")) as f:
                return json.load(f)

    def test_error_step_is_added_after_the_completed_steps(self):
        data = self._save(_failed(steps=[{"node": "plan", "kind": "plan"}]))
        steps = data["query_sources"]["agent_steps"]
        self.assertEqual([s["node"] for s in steps], ["plan", "error"])
        self.assertEqual(steps[-1]["output"], {"error": "RuntimeError: quota exceeded"})
        self.assertEqual(data["error"], "RuntimeError: quota exceeded")

    def test_error_step_without_completed_steps(self):
        data = self._save(_failed())
        self.assertEqual([s["node"] for s in data["query_sources"]["agent_steps"]], ["error"])

    def test_error_is_not_in_the_chat_response(self):
        resp = _failed()
        self.assertNotIn("quota exceeded", resp.model_dump_json())
        self.assertNotIn("error", (resp.query_sources or {}))

    def test_successful_answer_has_no_error_step(self):
        resp = GraphRAGResponse(natural_language_response="ok", answered_question=True,
                                response_type="agentic", query_sources={"agent_steps": [{"node": "plan"}]})
        data = self._save(resp)
        self.assertEqual([s["node"] for s in data["query_sources"]["agent_steps"]], ["plan"])
        self.assertIsNone(data["error"])


if __name__ == "__main__":
    unittest.main()
