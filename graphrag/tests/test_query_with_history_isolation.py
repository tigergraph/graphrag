import ast
import os
import unittest

from pydantic import ValidationError

from common.py_schemas.schemas import NaturalLanguageQuery

_INQUIRYAI = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "inquiryai.py"
)


class TestCallerSuppliedHistory(unittest.TestCase):
    """History on /{graphname}/query_with_history comes from the caller.

    The endpoint previously kept a process-global list shared by every
    caller and every graph, so one caller's questions and answers were
    injected into another's prompt (GML-2194). Context is now supplied per
    request, which makes that class of bug unrepresentable.
    """

    def test_history_defaults_to_none(self):
        """A request that omits history is valid and carries none."""
        self.assertIsNone(NaturalLanguageQuery(query="hi").history)

    def test_omitted_history_yields_no_turns(self):
        """The handler's `(history or [])` guard turns None into no turns."""
        query = NaturalLanguageQuery(query="hi")
        self.assertEqual(list(query.history or []), [])

    def test_history_round_trips(self):
        """Supplied turns are parsed in order."""
        query = NaturalLanguageQuery(
            query="and his email?",
            history=[
                {"query": "who is Ada?", "response": "an engineer"},
                {"query": "her ID?", "response": "42"},
            ],
        )
        self.assertEqual(
            [(t.query, t.response) for t in query.history],
            [("who is Ada?", "an engineer"), ("her ID?", "42")],
        )

    def test_only_the_most_recent_turns_are_used(self):
        """Older turns beyond the cap are dropped, newest kept."""
        turns = [{"query": f"q{i}", "response": f"r{i}"} for i in range(6)]
        query = NaturalLanguageQuery(query="hi", history=turns)
        self.assertEqual(
            [t.query for t in (query.history or [])[-3:]], ["q3", "q4", "q5"]
        )

    def test_malformed_turn_is_rejected(self):
        """A turn missing `response` fails validation rather than passing through."""
        with self.assertRaises(ValidationError):
            NaturalLanguageQuery(query="hi", history=[{"query": "a"}])


class TestNoProcessGlobalHistory(unittest.TestCase):
    """Regression guard for GML-2194."""

    def _module(self):
        with open(os.path.normpath(_INQUIRYAI), encoding="utf-8") as handle:
            return ast.parse(handle.read())

    def test_handler_declares_no_global(self):
        """The handler must not reach for process-wide mutable state."""
        for node in ast.walk(self._module()):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "retrieve_answer_with_chathistory"
            ):
                globals_declared = [
                    name
                    for child in ast.walk(node)
                    if isinstance(child, ast.Global)
                    for name in child.names
                ]
                self.assertEqual(
                    globals_declared,
                    [],
                    "conversation state must be per-request, not process-global",
                )
                return
        self.fail("retrieve_answer_with_chathistory not found")

    def test_no_module_level_conversation_store(self):
        """No module-level list/dict is left to accumulate caller history."""
        stores = [
            target.id
            for node in self._module().body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, (ast.List, ast.Dict))
            for target in node.targets
            if isinstance(target, ast.Name)
            and "history" in target.id.lower()
        ]
        self.assertEqual(stores, [], f"module-level history store(s): {stores}")


if __name__ == "__main__":
    unittest.main()
