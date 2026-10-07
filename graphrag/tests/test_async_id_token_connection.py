import ast
import asyncio
import json
import os
import threading
import unittest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CONNECTIONS = os.path.join(_ROOT, "common", "db", "connections.py")
_ECC_MAIN = os.path.join(_ROOT, "ecc", "app", "main.py")


def _tree(path):
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _function(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _load_verify_helper():
    """Compile ``_verify_async_connection`` straight from source.

    Importing ``common.db.connections`` pulls in the app config and FastAPI; the
    helper needs neither, so it is exercised on its own.
    """
    fn = _function(_tree(_CONNECTIONS), "_verify_async_connection")
    module = ast.Module(body=[fn], type_ignores=[])
    namespace = {}
    exec(compile(module, _CONNECTIONS, "exec"), namespace)
    return namespace["_verify_async_connection"]


class _FakeConnection:
    def __init__(self, fail=False):
        self.fail = fail
        self.statements = []
        self.closed = False

    async def gsql(self, statement):
        self.statements.append(statement)
        if self.fail:
            raise PermissionError("rejected")

    async def aclose(self):
        self.closed = True


class TestVerifyReleasesItsSession(unittest.TestCase):
    """The async token check must not leave a session bound to a dead loop.

    It runs under ``asyncio.run()``, which closes its loop on return. With
    pyTigerGraph 2.0.4 a session opened there stays cached, and the connection's
    next request on another loop — ECC's rebuild — failed with "Event loop is
    closed", reported as "graph does not exist" (GML-2182).
    """

    def setUp(self):
        self.verify = _load_verify_helper()

    def test_checks_the_graph(self):
        conn = _FakeConnection()
        asyncio.run(self.verify(conn, "MyGraph"))
        self.assertEqual(conn.statements, ["USE GRAPH MyGraph"])

    def test_releases_the_session_after_success(self):
        conn = _FakeConnection()
        asyncio.run(self.verify(conn, "MyGraph"))
        self.assertTrue(conn.closed)

    def test_releases_the_session_when_the_check_fails(self):
        """A rejected token must still release the session, and still raise."""
        conn = _FakeConnection(fail=True)
        with self.assertRaises(PermissionError):
            asyncio.run(self.verify(conn, "MyGraph"))
        self.assertTrue(conn.closed)


class TestConnectionSurvivesTheCheck(unittest.TestCase):
    """End to end with the real client against a keep-alive HTTP server."""

    @classmethod
    def setUpClass(cls):
        try:
            from aiohttp import web
            from pyTigerGraph import AsyncTigerGraphConnection  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"needs aiohttp and pyTigerGraph: {exc}")

        async def ok(_request):
            body = json.dumps({"error": False, "message": "ok", "results": []})
            return web.Response(text=body, content_type="application/json")

        ready = threading.Event()
        cls._port = None

        def serve():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            app = web.Application()
            app.router.add_route("*", "/{tail:.*}", ok)
            runner = web.AppRunner(app)
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, "127.0.0.1", 0)
            loop.run_until_complete(site.start())
            cls._port = site._server.sockets[0].getsockname()[1]
            cls._loop = loop
            ready.set()
            loop.run_forever()

        threading.Thread(target=serve, daemon=True).start()
        ready.wait(10)

    @classmethod
    def tearDownClass(cls):
        loop = getattr(cls, "_loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)

    def test_connection_is_usable_on_a_new_loop_after_the_check(self):
        from pyTigerGraph import AsyncTigerGraphConnection

        port = str(self._port)
        conn = AsyncTigerGraphConnection(
            host="http://127.0.0.1", graphname="g", apiToken="t",
            gsPort=port, restppPort=port,
        )
        # The check runs on one throwaway loop; the connection is then reused
        # on another, which is how ECC drives it.
        asyncio.run(_load_verify_helper()(conn, "g"))
        self.assertIsNone(conn._async_client)
        asyncio.run(conn.gsql("ls"))  # raised "Event loop is closed" before


class TestIdTokenPathKeepsValidation(unittest.TestCase):
    """Skipping the check would also skip token validation on this path."""

    def setUp(self):
        self.fn = _function(_tree(_CONNECTIONS), "get_db_connection_id_token")

    def test_async_branch_runs_the_releasing_check(self):
        calls = [
            ast.unparse(n)
            for n in ast.walk(self.fn)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == "asyncio.run"
        ]
        self.assertTrue(
            any("_verify_async_connection" in c for c in calls),
            f"async branch must validate via the releasing helper: {calls}",
        )
        self.assertFalse(
            any("conn.gsql" in c for c in calls),
            "a bare conn.gsql under asyncio.run leaves the session on a dead loop",
        )

    def test_async_auth_failure_maps_to_401(self):
        """The async client raises aiohttp's error, not requests.HTTPError, so the
        sync-only handler never caught it and a bad token surfaced as a 500."""
        handled = [
            ast.unparse(h.type)
            for n in ast.walk(self.fn)
            if isinstance(n, ast.Try)
            for h in n.handlers
            if h.type is not None
        ]
        self.assertIn("aiohttp.ClientResponseError", handled)


class TestIdTokenOutcomes(unittest.TestCase):
    """Run the real function against a port nothing listens on."""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi import HTTPException
            from common.db import connections
        except ImportError as exc:  # needs the app environment
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.HTTPException = HTTPException
        cls.connections = connections

    def test_unreachable_database_is_503(self):
        """A down database raises aiohttp's connection error, which was not
        handled, so the rebuild request failed with a 500."""
        from unittest.mock import patch

        with patch.dict(self.connections.db_config, {"hostname": "http://127.0.0.1"}):
            with self.assertRaises(self.HTTPException) as caught:
                self.connections.get_db_connection_id_token("g", "token", async_conn=True)
        self.assertEqual(caught.exception.status_code, 503)


class TestEccGraphCheckMessage(unittest.TestCase):
    """The failure message is shown to operators; the cause belongs in the log."""

    def setUp(self):
        self.fn = _function(_tree(_ECC_MAIN), "run_with_tracking")

    def _graph_check_handler(self):
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Try) and "getVertexTypes" in ast.unparse(node.body[0]):
                return node.handlers[0]
        self.fail("graph check not found in run_with_tracking")

    def test_message_does_not_carry_the_raw_exception(self):
        handler = self._graph_check_handler()
        raised = [n for n in ast.walk(handler) if isinstance(n, ast.Raise)]
        self.assertTrue(raised)
        for node in raised:
            names = {
                n.id for n in ast.walk(node.exc) if isinstance(n, ast.Name)
            }
            self.assertNotIn(handler.name, names, ast.unparse(node.exc))

    def test_cause_is_logged(self):
        handler = self._graph_check_handler()
        logged = [
            ast.unparse(n)
            for n in ast.walk(handler)
            if isinstance(n, ast.Call) and ast.unparse(n.func).startswith("LogWriter.")
        ]
        self.assertTrue(logged, "the underlying error must be logged")


if __name__ == "__main__":
    unittest.main()
