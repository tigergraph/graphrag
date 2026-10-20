"""ECC regenerate actions count a vertex only when its embedding is saved
(GML-2345)."""
import asyncio
import os
import sys
import unittest
from unittest import mock

_ECC_APP = os.path.join(os.path.dirname(__file__), "..", "..", "ecc", "app")
try:
    sys.path.insert(0, os.path.abspath(_ECC_APP))
    for name in [m for m in sys.modules if m == "graphrag" or m.startswith("graphrag.")]:
        del sys.modules[name]  # the ECC package, not the repo folder of the same name
    from graphrag import regenerate
except Exception as exc:  # needs the app environment
    raise unittest.SkipTest(f"ECC environment unavailable: {exc}")
finally:
    sys.path.pop(0)


class _Conn:
    def __init__(self, missing):
        self.missing = missing

    async def getSchema(self):
        return {"VertexTypes": [{"Name": "Community", "EmbeddingAttributes": [{"Name": "embedding"}]}]}

    async def runInstalledQuery(self, name, params=None):
        return [{"results": [{"v_id": v} for v in self.missing]}]

    async def getVerticesById(self, vtype, vid):
        return [{"attributes": {"description": f"summary of {vid}"}}]


class _Store:
    """Saves every vertex except those in ``fail``, as the real store does:
    a failure is logged and nothing is returned."""
    def __init__(self, fail=()):
        self.fail = set(fail)

    async def aadd_embeddings(self, embeddings, metadatas):
        vid = metadatas[0]["vertex_id"][0]
        return None if vid in self.fail else f"Document {metadatas} registered"


class TestRegenerateEmbeddingsCounts(unittest.TestCase):
    def _run(self, missing, fail=()):
        with mock.patch.object(regenerate, "_make_store", return_value=_Store(fail)):
            return asyncio.run(regenerate.regenerate_embeddings("G", _Conn(missing)))

    def test_saved_embeddings_are_counted(self):
        self.assertEqual(self._run(["c1", "c2"]), {"regenerated": 2, "skipped": 0})

    def test_an_embedding_that_was_not_saved_is_skipped(self):
        self.assertEqual(self._run(["c1", "c2", "c3"], fail={"c2"}),
                         {"regenerated": 2, "skipped": 1})


if __name__ == "__main__":
    unittest.main()
