"""Tests for structured retrieval without a graph-backed document index."""

from tools.generate_function import _load_local_function_docs


def test_bundled_function_docs_cover_core_structured_operations():
    documents, headers = _load_local_function_docs()

    assert documents
    assert "getVertexCount" in headers
    assert "getVerticesById" in headers
    assert "getEdges" in headers
    assert all(document.page_content for document in documents)
