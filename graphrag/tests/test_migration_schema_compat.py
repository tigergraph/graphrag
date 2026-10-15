import copy
import os
import unittest

from common.db.migrate import BASE_SCHEMA_PATH, check_schema_compatibility, parse_base_schema

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BASE = os.path.join(_ROOT, BASE_SCHEMA_PATH)


class FakeConn:
    """Schema metadata in the shapes pyTigerGraph returns."""

    def __init__(self, vertices, edges):
        self.vertices = vertices  # {name: {attr, ...}}
        self.edges = edges        # {name: {"pairs": [(f, t)], "attrs": {...}}}

    def getVertexTypes(self):
        return list(self.vertices)

    def getEdgeTypes(self):
        return list(self.edges)

    def getVertexType(self, vt):
        return {"Name": vt, "Attributes": [{"AttributeName": a} for a in self.vertices[vt]]}

    def getEdgeType(self, et):
        spec = self.edges[et]
        if "meta" in spec:  # verbatim metadata, as TigerGraph reported it
            return dict(spec["meta"], Attributes=[{"AttributeName": a} for a in spec["attrs"]])
        pairs = sorted(spec["pairs"])
        meta = {"Name": et, "Attributes": [{"AttributeName": a} for a in spec["attrs"]]}
        if len(pairs) == 1:
            meta["FromVertexTypeName"], meta["ToVertexTypeName"] = pairs[0]
        else:  # multi-pair edges report "*" plus an EdgePairs list
            meta["FromVertexTypeName"] = meta["ToVertexTypeName"] = "*"
            meta["EdgePairs"] = [{"From": f, "To": t} for f, t in pairs]
        return meta


def _current_graph():
    """A graph created with the current base schema (1.4 or later)."""
    with open(_BASE, encoding="utf-8") as f:
        vertices, edges = parse_base_schema(f.read())
    return copy.deepcopy(vertices), {
        k: {"pairs": set(v["pairs"]), "attrs": set(v["attrs"])} for k, v in edges.items()
    }


def _check(vertices, edges):
    return check_schema_compatibility(FakeConn(vertices, edges), base_schema_path=_BASE)


class TestParseBaseSchema(unittest.TestCase):
    def test_reads_the_shipped_base_schema(self):
        vertices, edges = _current_graph()
        self.assertEqual(
            set(vertices),
            {"DocumentChunk", "Document", "Entity", "RelationshipType", "Content",
             "EntityType", "Community"},
        )
        self.assertEqual(edges["IS_HEAD_OF"]["pairs"], {("EntityType", "RelationshipType")})
        self.assertEqual(
            edges["HAS_CONTENT"]["pairs"],
            {("Document", "Content"), ("DocumentChunk", "Content")},
        )
        self.assertEqual(edges["LINKS_TO"]["attrs"], {"weight"})  # lowercase from/to
        self.assertIn("description", vertices["Entity"])
        self.assertNotIn("id", vertices["Entity"])


class TestSchemaCompatibility(unittest.TestCase):
    def test_current_schema_is_compatible(self):
        result = _check(*_current_graph())
        self.assertTrue(result.compatible, result.differences)

    def test_extra_domain_and_image_types_are_allowed(self):
        vertices, edges = _current_graph()
        vertices["Company"] = {"name", "industry"}
        vertices["Image"] = {"image_data", "image_format"}
        edges["WORKS_FOR"] = {"pairs": {("Company", "Company")}, "attrs": set()}
        edges["CONTAINS_ENTITY"]["pairs"].add(("DocumentChunk", "Company"))
        self.assertTrue(_check(vertices, edges).compatible)

    def test_wildcard_source_accepts_every_type(self):
        """IN_COMMUNITY on a graph with domain types, as TigerGraph reports it."""
        vertices, edges = _current_graph()
        edges["IN_COMMUNITY"]["meta"] = {
            "Name": "IN_COMMUNITY", "FromVertexTypeName": "*",
            "ToVertexTypeName": "Community", "EdgePairs": [], "IsDirected": True,
        }
        self.assertTrue(_check(vertices, edges).compatible)

    def test_wildcard_with_the_wrong_target_is_incompatible(self):
        vertices, edges = _current_graph()
        edges["IN_COMMUNITY"]["meta"] = {
            "Name": "IN_COMMUNITY", "FromVertexTypeName": "*",
            "ToVertexTypeName": "EntityType", "EdgePairs": [],
        }
        self.assertFalse(_check(vertices, edges).compatible)

    def test_13_style_meta_edges_are_incompatible(self):
        vertices, edges = _current_graph()
        edges["IS_HEAD_OF"]["pairs"] = {("Entity", "RelationshipType")}
        edges["HAS_TAIL"]["pairs"] = {("RelationshipType", "Entity")}
        edges["RELATIONSHIP_TYPE"] = {"pairs": {("EntityType", "EntityType")}, "attrs": {"frequency"}}
        result = _check(vertices, edges)
        self.assertFalse(result.compatible)
        self.assertTrue(any("IS_HEAD_OF" in d and "EntityType->RelationshipType" in d
                            for d in result.differences), result.differences)
        self.assertTrue(any("HAS_TAIL" in d for d in result.differences))

    def test_missing_vertex_type_is_incompatible(self):
        vertices, edges = _current_graph()
        del vertices["EntityType"]
        result = _check(vertices, edges)
        self.assertFalse(result.compatible)
        self.assertIn("missing vertex type EntityType", result.differences)

    def test_missing_attribute_is_incompatible(self):
        vertices, edges = _current_graph()
        vertices["Entity"].discard("entity_type")
        edges["RELATIONSHIP"]["attrs"].clear()
        result = _check(vertices, edges)
        self.assertFalse(result.compatible)
        self.assertTrue(any("Entity" in d and "entity_type" in d for d in result.differences))
        self.assertTrue(any("RELATIONSHIP" in d and "relation_type" in d for d in result.differences))

    def test_missing_edge_pair_is_incompatible(self):
        vertices, edges = _current_graph()
        edges["CONTAINS_ENTITY"]["pairs"].discard(("Document", "Entity"))
        result = _check(vertices, edges)
        self.assertFalse(result.compatible)
        self.assertTrue(any("CONTAINS_ENTITY" in d and "Document->Entity" in d
                            for d in result.differences))

    def test_unreadable_schema_raises(self):
        class Broken(FakeConn):
            def getVertexTypes(self):
                raise ConnectionError("TigerGraph unreachable")

        with self.assertRaises(ConnectionError):
            check_schema_compatibility(Broken({}, {}), base_schema_path=_BASE)


class TestRepairGate(unittest.TestCase):
    """The repair endpoint refuses graphs it cannot repair in place."""

    @classmethod
    def setUpClass(cls):
        try:
            from routers import ui
        except Exception as exc:  # needs the app environment
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.ui = ui

    def setUp(self):
        from unittest import mock
        ui = self.ui
        self.inner = mock.Mock(return_value={"success": True})
        self.conn = None
        patches = [
            mock.patch.object(ui, "get_rebuilding_graph", return_value=None),
            mock.patch.object(ui, "acquire_graph_lock", return_value=True),
            mock.patch.object(ui, "release_graph_lock"),
            mock.patch.object(ui, "get_db_connection_pwd_manual", side_effect=lambda *a, **k: self.conn),
            mock.patch.object(ui, "_migration_apply_inner", self.inner),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # The shipped schema path is relative to the app directory; point the
        # check at the repo copy, since tests run from elsewhere.
        import common.db.migrate as migrate
        real = migrate.check_schema_compatibility
        p = mock.patch.object(migrate, "check_schema_compatibility",
                              side_effect=lambda conn: real(conn, base_schema_path=_BASE))
        p.start()
        self.addCleanup(p.stop)

    def _apply(self):
        creds = (["superuser"], type("C", (), {"username": "u", "password": "p"})())
        return self.ui.migration_apply("g", creds, {"outdated": ["Q"]})

    def test_compatible_graph_is_repaired(self):
        self.conn = FakeConn(*_current_graph())
        self.assertEqual(self._apply(), {"success": True})
        self.inner.assert_called_once()

    def test_incompatible_graph_is_refused(self):
        from fastapi import HTTPException
        vertices, edges = _current_graph()
        edges["IS_HEAD_OF"]["pairs"] = {("Entity", "RelationshipType")}
        self.conn = FakeConn(vertices, edges)
        with self.assertRaises(HTTPException) as ctx:
            self._apply()
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("older version of GraphRAG", ctx.exception.detail)
        self.inner.assert_not_called()

    def test_unreadable_schema_is_refused(self):
        from fastapi import HTTPException

        class Broken(FakeConn):
            def getVertexTypes(self):
                raise ConnectionError("TigerGraph unreachable")

        self.conn = Broken({}, {})
        with self.assertRaises(HTTPException) as ctx:
            self._apply()
        self.assertEqual(ctx.exception.status_code, 503)
        self.inner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
