"""Regression tests for generated openCypher normalization."""

from tools.generate_cypher import _clean_cypher_output


def test_removes_cypher_markdown_fence():
    assert _clean_cypher_output("```cypher\nMATCH (n) RETURN n\n```") == (
        "MATCH (n) RETURN n"
    )


def test_removes_opencypher_markdown_fence():
    assert _clean_cypher_output("```opencypher\nMATCH (n) RETURN n\n```") == (
        "MATCH (n) RETURN n"
    )


def test_does_not_strip_query_characters():
    query = 'MATCH (issue:JiraIssue) WHERE issue.issue_key = "GML-2191" RETURN issue'
    assert _clean_cypher_output(query) == query


def test_removes_plain_markdown_fence():
    assert _clean_cypher_output("```\nMATCH (n) RETURN n\n```") == (
        "MATCH (n) RETURN n"
    )


def test_removes_uppercase_fence():
    assert _clean_cypher_output("```CYPHER\nMATCH (n) RETURN n\n```") == (
        "MATCH (n) RETURN n"
    )


def test_plain_query_unchanged():
    query = "MATCH (n) RETURN n"
    assert _clean_cypher_output(query) == query
