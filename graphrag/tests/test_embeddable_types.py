"""Regression test: native vector attributes must be detected from the schema's
``EmbeddingAttributes``, not ``getVertexAttrs`` (which omits them). Guards the
GML-2175 fix where embedding health + regenerate found no embeddable types.
"""
import unittest

from common.db import health

# getVertexAttrs would NOT list the vector attribute for these types; the schema
# exposes it under EmbeddingAttributes instead.
_SCHEMA = {
    "VertexTypes": [
        {"Name": "DocumentChunk", "Attributes": [{"AttributeName": "idx"}],
         "EmbeddingAttributes": [{"Name": "embedding", "Dimension": 1536}]},
        {"Name": "Community", "Attributes": [],
         "EmbeddingAttributes": [{"Name": "embedding"}]},
        {"Name": "Entity", "Attributes": [{"AttributeName": "definition"}],
         "EmbeddingAttributes": []},                       # has the key but empty
        {"Name": "Content", "Attributes": [{"AttributeName": "text"}]},  # no key
    ]
}


class _FakeConn:
    def __init__(self, schema):
        self._schema = schema

    def getSchema(self):
        return self._schema


class TestEmbeddableTypes(unittest.TestCase):
    def setUp(self):
        self.conn = _FakeConn(_SCHEMA)

    def test_embeddable_types_from_embedding_attributes(self):
        self.assertEqual(sorted(health.embeddable_types(self.conn)),
                         ["Community", "DocumentChunk"])

    def test_has_vector_attr(self):
        self.assertTrue(health._has_vector_attr(self.conn, "DocumentChunk"))
        self.assertTrue(health._has_vector_attr(self.conn, "Community"))
        self.assertFalse(health._has_vector_attr(self.conn, "Entity"))   # empty
        self.assertFalse(health._has_vector_attr(self.conn, "Content"))  # no key
        self.assertFalse(health._has_vector_attr(self.conn, "Nonexistent"))

    def test_empty_schema_is_safe(self):
        self.assertEqual(health.embeddable_types(_FakeConn({})), [])


if __name__ == "__main__":
    unittest.main()
