"""Regression tests for the built-in structural read-function surface."""

import pytest

from tools.graphrag_tools import _result_is_empty
from tools.validation_utils import (
    InvalidFunctionCallException,
    SAFE_READ_FUNCTIONS,
    validate_function_call,
)


class _Connection:
    graphname = "JiraTest"

    def getEndpoints(self, dynamic=True):
        return {}


def test_get_vertices_is_available_without_registered_function_documents():
    call = 'getVertices("JiraIssue", where=\'issue_key == "GML-2186"\')'

    assert validate_function_call(_Connection(), call, SAFE_READ_FUNCTIONS) == call


def test_mutating_function_is_not_in_builtin_read_surface():
    with pytest.raises(InvalidFunctionCallException):
        validate_function_call(
            _Connection(),
            'upsertVertex("JiraIssue", "x", {})',
            SAFE_READ_FUNCTIONS,
        )


def test_json_encoded_empty_result_triggers_structural_fallback():
    assert _result_is_empty("[]")
