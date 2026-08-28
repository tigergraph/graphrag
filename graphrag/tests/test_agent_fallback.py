"""Unit tests for the deterministic structural->hybrid fallback in the
agentic engine.

A structural retrieve that returns no rows must not leave the answer
ungrounded: ``run_agentic`` guarantees a single hybrid search runs before
giving up, rather than depending on the LLM planner to add one on replan.
"""
import unittest
from unittest import mock

import agent.agentic_graph as g
from common.py_schemas import GraphRAGResponse, Plan, PlanStep, StepResult


class _Ctx:
    """Minimal GraphRAGToolContext stand-in for run_agentic."""
    def __init__(self, cfg=None):
        self.graphrag_cfg = {} if cfg is None else cfg
        self.emit = lambda *a, **k: None


def _structural_plan():
    return Plan(steps=[PlanStep(id="S1", kind="structural",
                                tool=g._STRUCTURAL_TOOL)], strategy="")


def _synth(*a, **k):
    return GraphRAGResponse(natural_language_response="ok",
                            answered_question=True, response_type="agentic")


class TestStructuralHybridFallback(unittest.TestCase):
    def _run(self, exec_results, exec_traces, run_step_side_effect, cfg=None):
        with mock.patch.object(g, "plan_question", return_value=_structural_plan()), \
             mock.patch.object(g, "execute_plan",
                               return_value=(exec_results, exec_traces)), \
             mock.patch.object(g, "synthesize", side_effect=_synth), \
             mock.patch.object(g, "_run_step",
                               side_effect=run_step_side_effect) as run_step:
            resp = g.run_agentic(_Ctx(cfg), llm=object(), question="q")
        return resp, run_step

    def test_fallback_runs_when_structural_empty(self):
        # Structural step returned no rows -> no context.
        empty = {"S1": StepResult(step_id="S1", ok=False,
                                  summary="structural query returned no rows",
                                  context=None)}
        traces = [{"tool": g._STRUCTURAL_TOOL, "node": "S1"}]

        def fallback(step, args, ctx, results, out_traces):
            # The fallback fills context so the loop can synthesize.
            results["fallback_hybrid"] = StepResult(
                step_id="fallback_hybrid", ok=True, summary="hybrid ok",
                context={"function_call": "GraphRAG_Hybrid_Vector_Search",
                         "result": [{"chunk": "table row"}]})

        _, run_step = self._run(empty, traces, fallback)
        self.assertEqual(run_step.call_count, 1)
        called_step = run_step.call_args[0][0]
        called_args = run_step.call_args[0][1]
        self.assertEqual(called_step.tool, g._HYBRID_TOOL)
        self.assertEqual(called_args, {"question": "q"})

    def test_no_fallback_when_structural_has_context(self):
        # Structural returned rows -> already grounded, no fallback.
        filled = {"S1": StepResult(step_id="S1", ok=True, summary="rows",
                                   context={"result": [{"row": 1}]})}
        traces = [{"tool": g._STRUCTURAL_TOOL, "node": "S1"}]
        _, run_step = self._run(filled, traces, lambda *a, **k: None)
        self.assertEqual(run_step.call_count, 0)

    def test_no_fallback_when_disabled_by_config(self):
        # enable_router_fallback=False disables it, matching the classic engine.
        empty = {"S1": StepResult(step_id="S1", ok=False, summary="none",
                                  context=None)}
        traces = [{"tool": g._STRUCTURAL_TOOL, "node": "S1"}]
        _, run_step = self._run(empty, traces, lambda *a, **k: None,
                                cfg={"enable_router_fallback": False})
        self.assertEqual(run_step.call_count, 0)

    def test_no_fallback_when_no_structural_attempted(self):
        # Only an unstructured step ran (and returned nothing) -> the
        # structural-specific fallback does not fire.
        empty = {"S1": StepResult(step_id="S1", ok=False, summary="none",
                                  context=None)}
        traces = [{"tool": g._HYBRID_TOOL, "node": "S1"}]
        _, run_step = self._run(empty, traces, lambda *a, **k: None)
        self.assertEqual(run_step.call_count, 0)


if __name__ == "__main__":
    unittest.main()
