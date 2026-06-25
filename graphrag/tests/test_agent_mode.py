"""Unit tests for chat agent-style resolution behind the v2.0 chat menu.

Covers the rule that drives Agent · Auto / Planned / Reactive: a per-request
style overrides the graph config unless it's "auto", and only "planned"
selects the planner DAG (everything else is the free tool-calling loop).
"""
import unittest

from agent.agentic_agent import _resolve_style


class TestResolveStyle(unittest.TestCase):
    def test_auto_defers_to_config(self):
        self.assertEqual(_resolve_style("auto", "react"), "react")
        self.assertEqual(_resolve_style("auto", "planned"), "planned")

    def test_explicit_request_overrides_config(self):
        self.assertEqual(_resolve_style("planned", "react"), "planned")
        self.assertEqual(_resolve_style("reactive", "planned"), "react")

    def test_none_and_unknown_default_safely(self):
        self.assertEqual(_resolve_style(None, "planned"), "planned")   # None -> auto -> config
        self.assertEqual(_resolve_style("bogus", "react"), "react")    # unknown explicit -> react
        self.assertEqual(_resolve_style("auto", "weird"), "react")     # unknown config -> react

    def test_case_insensitive(self):
        self.assertEqual(_resolve_style("Planned", "react"), "planned")
        self.assertEqual(_resolve_style("AUTO", "Planned"), "planned")


if __name__ == "__main__":
    unittest.main()
