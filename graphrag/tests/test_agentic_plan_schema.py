"""Regression tests for provider-normalized agent plans."""

from common.py_schemas import Plan
from agent.agentic_planner import _bind_implicit_question


def test_plan_accepts_name_and_input_aliases():
    plan = Plan.model_validate(
        {
            "steps": [
                {
                    "name": "S1",
                    "tool": "graphrag__structural_retrieve",
                    "input": {"question": "How many transactions are there?"},
                },
                {
                    "name": "A1",
                    "kind": "answer",
                    "tool": "",
                    "depends_on": ["S1"],
                },
            ]
        }
    )

    assert plan.steps[0].id == "S1"
    assert plan.steps[0].args == {
        "question": "How many transactions are there?"
    }
    assert plan.steps[1].id == "A1"


def test_plan_moves_flat_tool_arguments_into_args():
    plan = Plan.model_validate(
        {
            "steps": [
                {
                    "name": "S1",
                    "tool": "graphrag__structural_retrieve",
                    "question": "List transactions for card 123",
                    "top_k": 10,
                }
            ]
        }
    )

    assert plan.steps[0].args == {
        "question": "List transactions for card 123",
        "top_k": 10,
    }


def test_retrieval_step_inherits_request_question_when_omitted():
    plan = Plan.model_validate(
        {
            "steps": [
                {
                    "id": "S1",
                    "tool": "graphrag__structural_retrieve",
                    "args": {},
                }
            ]
        }
    )

    bound = _bind_implicit_question(
        plan, "How many transactions are there?"
    )

    assert bound.steps[0].args["question"] == (
        "How many transactions are there?"
    )
