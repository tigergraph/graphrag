"""ECC regenerate actions run on the service's event loop: a shared LLM or
embedding client must not be used from a new event loop per request."""
import asyncio
import os
import sys
import unittest
from unittest import mock

_ECC_APP = os.path.join(os.path.dirname(__file__), "..", "..", "ecc", "app")
# ECC has its own ``supportai`` and ``graphrag`` packages. Set the GraphRAG
# app's modules of those names aside while ECC's main imports, then put them
# back, so later tests still get the GraphRAG ones.
_SHARED = ("main", "supportai", "graphrag")
_saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] in _SHARED}
try:
    for k in _saved:
        del sys.modules[k]
    sys.path.insert(0, os.path.abspath(_ECC_APP))
    import main as ecc_main
except Exception as exc:  # needs the app environment
    raise unittest.SkipTest(f"ECC app unavailable: {exc}")
finally:
    sys.path.pop(0)
    for k in [k for k in sys.modules if k.split(".")[0] in _SHARED]:
        del sys.modules[k]
    sys.modules.update(_saved)


class TestRegenerateRunsOnTheServiceLoop(unittest.TestCase):
    def test_every_regenerate_endpoint_is_async(self):
        for name in ("regenerate_embeddings_endpoint", "regenerate_summaries_endpoint",
                     "move_heading_paths_endpoint", "regenerate_chunk_summaries_endpoint"):
            self.assertTrue(asyncio.iscoroutinefunction(getattr(ecc_main, name)), name)

    def test_actions_share_one_loop(self):
        loops = []

        async def action(graphname, conn):
            loops.append(asyncio.get_running_loop())
            return {"regenerated": 1}

        async def run_twice():
            with mock.patch.object(ecc_main, "_regen_build_conn", return_value=object()):
                a = await ecc_main._run_regen("G", None, "regen_a", action)
                b = await ecc_main._run_regen("G", None, "regen_b", action)
            return a, b, asyncio.get_running_loop()

        a, b, loop = asyncio.run(run_twice())
        self.assertEqual(a["regenerated"], 1)
        self.assertEqual(b["status"], "completed")
        self.assertEqual(loops, [loop, loop])


if __name__ == "__main__":
    unittest.main()
