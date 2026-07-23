# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from app.main import app

# main.py uses ``import routers`` (absolute), so the module is registered
# as ``routers.ui`` in sys.modules.  Alias it to ``app.routers.ui`` so the
# @patch() targets resolve to the same module object.
sys.modules.setdefault("app.routers.ui", sys.modules["routers.ui"])

from app.routers.ui import _sweep_legacy_schema_subdirs
from common.utils.graph_locks import (
    acquire_graph_lock,
    get_current_operation,
    release_graph_lock,
)


GRAPH = "TestGraph"


def _ok_auth():
    """Mock ``auth()`` to return a single accessible graph + dummy creds."""
    return ([GRAPH], MagicMock(username="testuser", password="testpass"))


class _ChdirTempDir:
    """Context manager that chdirs into a fresh temp dir for the duration
    of a test. Restores CWD on exit even if the test raises.
    """

    def __init__(self):
        self.tmp = None
        self.prev = None

    def __enter__(self):
        self.prev = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="graphrag_v141_test_")
        os.chdir(self.tmp)
        return Path(self.tmp)

    def __exit__(self, *exc):
        os.chdir(self.prev)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestUploadsCheck(unittest.TestCase):
    """``POST /ui/{graph}/uploads/check`` reports which planned filenames
    already exist for the graph, so the client can prompt once before
    sending bytes.
    """

    def setUp(self):
        self.client = TestClient(app)

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_returns_conflicts_for_existing_files(self, _mock_auth):
        with _ChdirTempDir() as tmp:
            upload_dir = tmp / "uploads" / GRAPH
            upload_dir.mkdir(parents=True)
            (upload_dir / "report.pdf").write_bytes(b"old")
            (upload_dir / "summary.docx").write_bytes(b"old")

            resp = self.client.post(
                f"/ui/{GRAPH}/uploads/check",
                json={"filenames": ["report.pdf", "summary.docx", "new.csv"]},
                auth=("testuser", "testpass"),
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(set(body["conflicts"]), {"report.pdf", "summary.docx"})

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_returns_empty_when_no_conflicts(self, _mock_auth):
        with _ChdirTempDir() as tmp:
            (tmp / "uploads" / GRAPH).mkdir(parents=True)

            resp = self.client.post(
                f"/ui/{GRAPH}/uploads/check",
                json={"filenames": ["fresh.pdf"]},
                auth=("testuser", "testpass"),
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["conflicts"], [])

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_returns_empty_when_graph_dir_missing(self, _mock_auth):
        with _ChdirTempDir():
            resp = self.client.post(
                f"/ui/{GRAPH}/uploads/check",
                json={"filenames": ["whatever.pdf"]},
                auth=("testuser", "testpass"),
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["conflicts"], [])

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_rejects_non_list_filenames(self, _mock_auth):
        with _ChdirTempDir():
            resp = self.client.post(
                f"/ui/{GRAPH}/uploads/check",
                json={"filenames": "report.pdf"},
                auth=("testuser", "testpass"),
            )

        self.assertEqual(resp.status_code, 400)


class TestUploadsOverwriteSkip(unittest.TestCase):
    """``POST /ui/{graph}/uploads`` honours the overwrite / skip params."""

    def setUp(self):
        self.client = TestClient(app)

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_overwrite_false_returns_conflict_envelope_on_collision(self, _mock_auth):
        with _ChdirTempDir() as tmp:
            (tmp / "uploads" / GRAPH).mkdir(parents=True)
            (tmp / "uploads" / GRAPH / "report.pdf").write_bytes(b"old")

            resp = self.client.post(
                f"/ui/{GRAPH}/uploads",
                files=[("files", ("report.pdf", io.BytesIO(b"new"), "application/pdf"))],
                auth=("testuser", "testpass"),
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "conflict")
        self.assertIn("report.pdf", body["existing_files"])

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_overwrite_true_drops_cached_jsonl(self, _mock_auth):
        with _ChdirTempDir() as tmp:
            upload_dir = tmp / "uploads" / GRAPH
            upload_dir.mkdir(parents=True)
            (upload_dir / "report.pdf").write_bytes(b"old")
            temp_folder = tmp / "uploads" / "ingestion_temp" / GRAPH
            temp_folder.mkdir(parents=True)
            (temp_folder / "report.jsonl").write_text("stale-extract")

            resp = self.client.post(
                f"/ui/{GRAPH}/uploads?overwrite=true",
                files=[("files", ("report.pdf", io.BytesIO(b"new"), "application/pdf"))],
                auth=("testuser", "testpass"),
            )

            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "success")
            # File replaced with new bytes
            self.assertEqual((upload_dir / "report.pdf").read_bytes(), b"new")
            # Cached extract removed so the next ingest re-converts
            self.assertFalse((temp_folder / "report.jsonl").exists())

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_skip_drops_listed_files_from_upload_set(self, _mock_auth):
        with _ChdirTempDir() as tmp:
            (tmp / "uploads" / GRAPH).mkdir(parents=True)
            (tmp / "uploads" / GRAPH / "keep_me.pdf").write_bytes(b"old")

            resp = self.client.post(
                f"/ui/{GRAPH}/uploads?skip=keep_me.pdf",
                files=[
                    ("files", ("keep_me.pdf", io.BytesIO(b"new"), "application/pdf")),
                    ("files", ("fresh.pdf", io.BytesIO(b"fresh"), "application/pdf")),
                ],
                auth=("testuser", "testpass"),
            )

            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "success")
            self.assertEqual(
                [u["filename"] for u in body["uploaded_files"]],
                ["fresh.pdf"],
            )
            # Skipped file untouched on disk
            self.assertEqual(
                (tmp / "uploads" / GRAPH / "keep_me.pdf").read_bytes(),
                b"old",
            )


class TestExtractSchemaRequiresFilenames(unittest.TestCase):
    """``POST /ui/{graph}/extract_schema_from_jsonl`` rejects requests
    that don't name the samples to feed to the LLM.
    """

    def setUp(self):
        self.client = TestClient(app)

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_empty_filenames_returns_400(self, _mock_auth):
        with _ChdirTempDir():
            resp = self.client.post(
                f"/ui/{GRAPH}/extract_schema_from_jsonl",
                json={"filenames": []},
                auth=("testuser", "testpass"),
            )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("non-empty", resp.json()["detail"].lower())

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_missing_filenames_returns_400(self, _mock_auth):
        with _ChdirTempDir():
            resp = self.client.post(
                f"/ui/{GRAPH}/extract_schema_from_jsonl",
                json={},
                auth=("testuser", "testpass"),
            )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("non-empty", resp.json()["detail"].lower())

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    @patch("app.routers.ui.acquire_graph_lock", return_value=False)
    def test_lock_held_returns_409(self, _mock_lock, _mock_auth):
        with _ChdirTempDir():
            resp = self.client.post(
                f"/ui/{GRAPH}/extract_schema_from_jsonl",
                json={"filenames": ["report.pdf"]},
                auth=("testuser", "testpass"),
            )

        self.assertEqual(resp.status_code, 409)
        self.assertIn("schema extraction", resp.json()["detail"].lower())


class TestConvertSampleFiles(unittest.TestCase):
    """``POST /ui/{graph}/convert_sample_files`` writes samples to the
    flat ``uploads/{graph}/`` layout and prompts on filename collisions.
    """

    def setUp(self):
        self.client = TestClient(app)

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_writes_to_flat_layout_and_returns_saved_files(self, _mock_auth):
        async def _fake_process(self, folder, graphname, temp):
            stem = "report"
            Path(temp).mkdir(parents=True, exist_ok=True)
            Path(temp, f"{stem}.jsonl").write_text('{"text":"hello"}\n')
            return {"num_documents": 1}

        with _ChdirTempDir() as tmp, patch(
            "app.routers.ui.TextExtractor._process_folder_async",
            new=_fake_process,
        ):
            resp = self.client.post(
                f"/ui/{GRAPH}/convert_sample_files",
                files=[("files", ("report.pdf", io.BytesIO(b"x"), "application/pdf"))],
                auth=("testuser", "testpass"),
            )

            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertEqual(body["status"], "success")
            self.assertEqual(body["saved_files"], ["report.pdf"])
            # File landed in the flat upload directory, NOT a _schema_* subdir
            self.assertTrue((tmp / "uploads" / GRAPH / "report.pdf").exists())
            # No request-id field surfaces to the caller in the new contract
            self.assertNotIn("request_id", body)

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_sweeps_legacy_schema_subdirs_on_entry(self, _mock_auth):
        async def _fake_process(self, folder, graphname, temp):
            Path(temp).mkdir(parents=True, exist_ok=True)
            return {"num_documents": 0}

        with _ChdirTempDir() as tmp, patch(
            "app.routers.ui.TextExtractor._process_folder_async",
            new=_fake_process,
        ):
            stale_a = tmp / "uploads" / GRAPH / "_schema_old1"
            stale_b = tmp / "uploads" / "ingestion_temp" / GRAPH / "_schema_old2"
            stale_a.mkdir(parents=True)
            stale_b.mkdir(parents=True)
            (stale_a / "ghost.pdf").write_bytes(b"x")
            (stale_b / "ghost.jsonl").write_text("x")

            resp = self.client.post(
                f"/ui/{GRAPH}/convert_sample_files",
                files=[("files", ("fresh.pdf", io.BytesIO(b"y"), "application/pdf"))],
                auth=("testuser", "testpass"),
            )

            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertFalse(stale_a.exists())
            self.assertFalse(stale_b.exists())

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_returns_conflict_envelope_on_collision(self, _mock_auth):
        with _ChdirTempDir() as tmp:
            (tmp / "uploads" / GRAPH).mkdir(parents=True)
            (tmp / "uploads" / GRAPH / "report.pdf").write_bytes(b"old")

            resp = self.client.post(
                f"/ui/{GRAPH}/convert_sample_files",
                files=[("files", ("report.pdf", io.BytesIO(b"new"), "application/pdf"))],
                auth=("testuser", "testpass"),
            )

            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "conflict")
            self.assertIn("report.pdf", body["existing_files"])

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    @patch("app.routers.ui.acquire_graph_lock", return_value=False)
    def test_lock_held_returns_409(self, _mock_lock, _mock_auth):
        with _ChdirTempDir():
            resp = self.client.post(
                f"/ui/{GRAPH}/convert_sample_files",
                files=[("files", ("any.pdf", io.BytesIO(b"x"), "application/pdf"))],
                auth=("testuser", "testpass"),
            )

        self.assertEqual(resp.status_code, 409)


class TestLegacySubdirSweep(unittest.TestCase):
    """``_sweep_legacy_schema_subdirs`` is idempotent and tolerant of
    missing directories.
    """

    def test_removes_schema_subdirs_under_both_trees(self):
        with _ChdirTempDir() as tmp:
            a = tmp / "uploads" / GRAPH / "_schema_x"
            b = tmp / "uploads" / "ingestion_temp" / GRAPH / "_schema_y"
            other = tmp / "uploads" / GRAPH / "regular_subdir"
            for d in (a, b, other):
                d.mkdir(parents=True)
                (d / "file.bin").write_bytes(b"x")

            _sweep_legacy_schema_subdirs(GRAPH)

            self.assertFalse(a.exists())
            self.assertFalse(b.exists())
            # Non-schema subdirs are untouched
            self.assertTrue(other.exists())
            self.assertTrue((other / "file.bin").exists())

    def test_is_idempotent_when_no_subdirs_exist(self):
        with _ChdirTempDir():
            # Nothing to do; must not raise.
            _sweep_legacy_schema_subdirs(GRAPH)
            _sweep_legacy_schema_subdirs(GRAPH)


class TestGraphLockOperationTracking(unittest.TestCase):
    """``acquire_graph_lock`` records the operation name so
    ``get_current_operation`` can report it; release clears it.
    """

    def setUp(self):
        # Ensure each test starts with a free lock for the test graph.
        release_graph_lock(GRAPH, "test_cleanup")

    def tearDown(self):
        release_graph_lock(GRAPH, "test_cleanup")

    def test_get_current_operation_none_when_unlocked(self):
        self.assertIsNone(get_current_operation(GRAPH))

    def test_get_current_operation_returns_op_name_while_held(self):
        acquired = acquire_graph_lock(GRAPH, "create_ingest")
        self.assertTrue(acquired)
        self.assertEqual(get_current_operation(GRAPH), "create_ingest")

    def test_release_clears_current_operation(self):
        acquire_graph_lock(GRAPH, "ingest")
        self.assertEqual(get_current_operation(GRAPH), "ingest")
        release_graph_lock(GRAPH, "ingest")
        self.assertIsNone(get_current_operation(GRAPH))


class TestUploadStatusEndpoint(unittest.TestCase):
    """``GET /ui/{graph}/upload_status`` surfaces the current lock state
    so the Document Ingestion dialog can stay in sync with server-side
    work that's still in flight after the dialog was closed.
    """

    def setUp(self):
        self.client = TestClient(app)
        release_graph_lock(GRAPH, "test_cleanup")

    def tearDown(self):
        release_graph_lock(GRAPH, "test_cleanup")

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_processing_false_when_no_lock_held(self, _mock_auth):
        resp = self.client.get(
            f"/ui/{GRAPH}/upload_status", auth=("testuser", "testpass"),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["processing"])
        self.assertIsNone(body["operation"])

    @patch("app.routers.ui.auth", return_value=_ok_auth())
    def test_processing_true_while_create_ingest_holds_lock(self, _mock_auth):
        acquire_graph_lock(GRAPH, "create_ingest")
        try:
            resp = self.client.get(
                f"/ui/{GRAPH}/upload_status", auth=("testuser", "testpass"),
            )
        finally:
            release_graph_lock(GRAPH, "create_ingest")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["processing"])
        self.assertEqual(body["operation"], "create_ingest")


class TestTextExtractorSkipFilter(unittest.TestCase):
    """``TextExtractor`` must skip ``_schema_*`` subdirs when walking a
    folder so sample-doc staging is never re-ingested as regular
    documents.
    """

    def test_safe_walk_filter_includes_schema_prefix(self):
        src = Path(__file__).resolve().parents[2] / "common" / "utils" / "text_extractors.py"
        text = src.read_text()
        # The filter is a tuple of literal prefixes inside safe_walk.
        self.assertIn("'_schema_'", text)


if __name__ == "__main__":
    unittest.main()
