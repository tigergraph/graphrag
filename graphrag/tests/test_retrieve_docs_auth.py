import ast
import os
import unittest

_INQUIRYAI = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "inquiryai.py"
    )
)


def _tree():
    with open(_INQUIRYAI, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _route(tree, name):
    return next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name
        ),
        None,
    )


class TestRetrieveDocsAuthenticatesTheCaller(unittest.TestCase):
    """``/{graphname}/retrieve_docs`` must authenticate before returning content.

    The route previously took no ``Request``, so it never touched the
    connection ``auth_middleware`` had built. On the Basic-auth path that
    connection is constructed lazily — nothing contacts the database — so a
    request with absent or invalid credentials was never rejected, and the
    documents came back from a service-credentialed store (GML-2197).
    """

    def setUp(self):
        self.tree = _tree()
        self.fn = _route(self.tree, "retrieve_docs")
        self.assertIsNotNone(self.fn, "retrieve_docs not found")

    def test_route_receives_the_request(self):
        """Without a Request the route cannot reach the caller's connection."""
        annotations = [
            ast.unparse(a.annotation) for a in self.fn.args.args if a.annotation
        ]
        self.assertIn("Request", annotations)

    def test_route_checks_graph_access(self):
        """The caller must be proven against this graph before anything is read."""
        calls = [
            n.func.id
            for n in ast.walk(self.fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        self.assertIn("_require_graph_access", calls)

    def test_access_check_precedes_the_document_read(self):
        """Ordering is the guarantee: authorize, then retrieve."""
        check_line = retrieve_line = None
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "_require_graph_access":
                    check_line = node.lineno
                if isinstance(node.func, ast.Attribute) and node.func.attr == "retrieve_similar":
                    retrieve_line = node.lineno
        self.assertIsNotNone(check_line, "no access check")
        self.assertIsNotNone(retrieve_line, "no retrieval call")
        self.assertLess(check_line, retrieve_line)

    def test_searches_the_graph_named_in_the_path(self):
        """Authorizing graph A then reading graph B would make the check moot."""
        for node in ast.walk(self.fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "get_embedding_store"
            ):
                self.assertTrue(
                    node.args or node.keywords,
                    "get_embedding_store() with no graph returns the default "
                    "store, ignoring the graph in the path",
                )
                return
        self.fail("get_embedding_store not called")


class TestGraphAccessHelper(unittest.TestCase):
    """The helper must be graph-scoped and must reject a missing connection."""

    def setUp(self):
        self.tree = _tree()
        self.fn = _route(self.tree, "_require_graph_access")
        self.assertIsNotNone(self.fn, "_require_graph_access not found")

    def test_rejects_when_no_connection_was_built(self):
        """No Authorization header means the middleware built nothing — 401,
        not an AttributeError surfacing as a 500."""
        source = ast.unparse(self.fn)
        self.assertIn("getattr(request.state, 'conn', None)", source)
        self.assertIn("401", source)

    def test_uses_a_graph_scoped_probe_not_echo(self):
        """``echo()`` pings /echo and succeeds for any valid user on any graph,
        so it cannot establish access to *this* graph."""
        attrs = [
            n.func.attr
            for n in ast.walk(self.fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        ]
        self.assertIn("getVertexTypes", attrs)
        self.assertNotIn("echo", attrs)

    def test_raises_http_401_rather_than_propagating(self):
        raises = [
            ast.unparse(n.exc)
            for n in ast.walk(self.fn)
            if isinstance(n, ast.Raise) and n.exc is not None
        ]
        self.assertTrue(raises, "helper never raises")
        self.assertTrue(
            all("HTTPException" in r for r in raises),
            f"must raise HTTPException, got {raises}",
        )


class TestGraphAccessOutcomes(unittest.TestCase):
    """What the helper actually returns for each failure, run from source."""

    @classmethod
    def setUpClass(cls):
        try:
            import requests
        except ImportError as exc:
            raise unittest.SkipTest(f"needs requests: {exc}")

        class HTTPException(Exception):
            def __init__(self, status_code, detail=None):
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        fn = _route(_tree(), "_require_graph_access")
        # ``Request`` only appears in the signature's annotation.
        namespace = {"requests": requests, "HTTPException": HTTPException, "Request": object}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), _INQUIRYAI, "exec"), namespace)
        cls.check = staticmethod(namespace["_require_graph_access"])
        cls.HTTPException = HTTPException
        cls.requests = requests

    def _request(self, conn):
        request = type("Request", (), {})()
        request.state = type("State", (), {})()
        if conn is not None:
            request.state.conn = conn
        return request

    def _conn(self, raises=None):
        conn = type("Conn", (), {})()

        def get_vertex_types():
            if raises:
                raise raises
            return ["Document"]

        conn.getVertexTypes = get_vertex_types
        return conn

    def _status(self, conn):
        with self.assertRaises(self.HTTPException) as caught:
            self.check(self._request(conn), "g")
        return caught.exception.status_code

    def test_reachable_and_authorized_returns_the_connection(self):
        conn = self._conn()
        self.assertIs(self.check(self._request(conn), "g"), conn)

    def test_missing_connection_is_401(self):
        self.assertEqual(self._status(None), 401)

    def test_rejected_credentials_are_401(self):
        self.assertEqual(self._status(self._conn(Exception("User authentication failed"))), 401)

    def _http_error(self, status):
        response = self.requests.Response()
        response.status_code = status
        return self.requests.exceptions.HTTPError(f"{status}", response=response)

    def test_database_5xx_is_503(self):
        """nginx answers 502/503 while GSQL restarts; valid callers must not be
        told their credentials are wrong."""
        self.assertEqual(self._status(self._conn(self._http_error(502))), 503)

    def test_database_4xx_is_401(self):
        self.assertEqual(self._status(self._conn(self._http_error(401))), 401)

    def test_unreachable_database_is_503_not_401(self):
        """An outage must not read as bad credentials."""
        down = self.requests.exceptions.ConnectionError("connection refused")
        self.assertEqual(self._status(self._conn(down)), 503)
        slow = self.requests.exceptions.Timeout("timed out")
        self.assertEqual(self._status(self._conn(slow)), 503)


if __name__ == "__main__":
    unittest.main()
