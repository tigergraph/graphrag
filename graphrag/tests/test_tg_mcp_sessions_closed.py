import unittest


class TestToolCallsCloseTheirSessions(unittest.TestCase):
    """Each tool call runs on its own event loop; with pyTigerGraph keeping one
    session per loop, the call must close what it opened before the loop ends."""

    @classmethod
    def setUpClass(cls):
        try:
            from tools import tg_mcp_tools as t
        except ImportError as exc:  # needs the app environment
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        if not t.AVAILABLE:
            raise unittest.SkipTest("tigergraph-mcp not installed")
        cls.t = t

    def test_connections_used_by_a_call_are_closed(self):
        t = self.t
        closed = []

        class FakeConn:
            async def aclose(self):
                closed.append(self)

        conns = [FakeConn(), FakeConn()]

        async def tool():
            # A tool asks for its connection through the patched helper.
            used = t._used_conns.get()
            for c in conns:
                used.append(c)
            return "done"

        self.assertEqual(t._run(tool()), "done")
        self.assertEqual(closed, conns)
        self.assertIsNone(t._used_conns.get())

    def test_close_failure_does_not_fail_the_call(self):
        t = self.t

        class Broken:
            async def aclose(self):
                raise RuntimeError("already closed")

        async def tool():
            t._used_conns.get().append(Broken())
            return 42

        self.assertEqual(t._run(tool()), 42)

    def test_patched_lookup_records_the_connection(self):
        t = self.t
        from unittest import mock
        sentinel = object()
        with mock.patch.object(t, "_orig_get_connection", return_value=sentinel):
            seen = []
            async def tracked():
                conn = t._patched_get_connection(graph_name="g")
                seen.extend(t._used_conns.get())
                return conn
            self.assertIs(t._run(tracked()), sentinel)
            self.assertEqual(seen, [sentinel])


if __name__ == "__main__":
    unittest.main()
