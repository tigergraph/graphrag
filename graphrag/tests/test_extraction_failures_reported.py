import asyncio
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from common.utils.text_extractors import (
    ExtractionError,
    TextExtractor,
    describe_failures,
    failed_extractions,
)


class TestFailedExtractions(unittest.TestCase):
    """Folder processing records a failed file instead of raising, so callers
    read the failures back out of its result."""

    def test_maps_each_failed_file_to_its_reason(self):
        result = {"files": [
            {"file_path": "/up/g/good.pdf", "status": "success"},
            {"file_path": "/up/g/bad.pdf", "status": "failed", "error": "unreadable"},
        ]}
        self.assertEqual(failed_extractions(result), {"bad.pdf": "unreadable"})

    def test_tolerates_a_result_without_files(self):
        self.assertEqual(failed_extractions({"num_documents": 1}), {})
        self.assertEqual(failed_extractions(None), {})

    def test_description_names_each_file_once(self):
        text = describe_failures({
            "a.pdf": "Could not read text from a.pdf.",
            "b.doc": "legacy format",
        })
        self.assertEqual(text.count("a.pdf"), 1)
        self.assertIn("b.doc: legacy format", text)


class TestRealFolderProcessing(unittest.TestCase):
    """End to end through the real extractor: failures come back with reasons
    fit to show the person who uploaded the files."""

    def test_unreadable_files_are_reported_and_readable_ones_convert(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            with open(os.path.join(src, "good.txt"), "w") as handle:
                handle.write("Approval thresholds are defined below.")
            with open(os.path.join(src, "corrupt.docx"), "wb") as handle:
                handle.write(b"not a zip archive")
            with open(os.path.join(src, "legacy.doc"), "wb") as handle:
                handle.write(b"\xd0\xcf\x11\xe0 legacy")

            result = asyncio.run(
                TextExtractor()._process_folder_async(src, "g", out)
            )
            failures = failed_extractions(result)

            self.assertEqual(sorted(failures), ["corrupt.docx", "legacy.doc"])
            self.assertTrue(os.path.exists(os.path.join(out, "good.jsonl")))
            self.assertIn(".docx", failures["legacy.doc"])
            for reason in failures.values():
                self.assertNotIn(src, reason, "no filesystem path in the reason")
                self.assertNotIn("zip", reason.lower(), "no parser wording")


class TestOnlyCallerSafeReasons(unittest.TestCase):
    """Reasons now reach the person uploading, so unexpected errors — which can
    carry paths, URLs or service messages — must not pass through verbatim."""

    def test_unexpected_error_is_replaced_by_a_message_about_the_file(self):
        leak = RuntimeError("POST http://llm.internal/v1 failed; see /code/uploads/g/bad.pdf")
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            with open(os.path.join(src, "bad.pdf"), "wb") as handle:
                handle.write(b"%PDF-1.4")
            with patch(
                "common.utils.text_extractors.extract_text_from_file_with_images_as_docs",
                side_effect=leak,
            ):
                result = asyncio.run(TextExtractor()._process_folder_async(src, "g", out))
        reason = failed_extractions(result)["bad.pdf"]
        self.assertEqual(reason, "Could not process bad.pdf.")

    def test_server_side_reasons_name_the_file(self):
        """The UI shows only the reason, so it must say which file, and that
        the cause is on the server rather than in the upload."""
        for error in (FileNotFoundError("gone"), PermissionError("denied")):
            with self.subTest(error=type(error).__name__):
                with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
                    with open(os.path.join(src, "report.pdf"), "wb") as handle:
                        handle.write(b"%PDF-1.4")
                    with patch(
                        "common.utils.text_extractors.extract_text_from_file_with_images_as_docs",
                        side_effect=error,
                    ):
                        result = asyncio.run(TextExtractor()._process_folder_async(src, "g", out))
                reason = failed_extractions(result)["report.pdf"]
                self.assertIn("report.pdf", reason)
                self.assertIn("server", reason)

    def test_image_failure_does_not_blame_the_file(self):
        """The image path also fails when the description service does, so the
        message must not tell the user their image is corrupt."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            with open(os.path.join(src, "photo.png"), "wb") as handle:
                handle.write(b"not really a png")
            result = asyncio.run(TextExtractor()._process_folder_async(src, "g", out))
        reason = failed_extractions(result)["photo.png"]
        self.assertIn("photo.png", reason)
        self.assertNotIn("corrupt", reason.lower())


class TestCreateIngestReportsFailures(unittest.TestCase):
    """``create_ingest`` converts the uploaded folder for server ingest."""

    @classmethod
    def setUpClass(cls):
        try:
            from common.py_schemas.schemas import CreateIngestConfig
            from supportai import supportai
        except ImportError as exc:  # needs the full app environment
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.config_type = CreateIngestConfig
        cls.supportai = supportai

    def _config(self, data_path):
        return self.config_type(
            data_source="server",
            file_format="multi",
            data_source_config={"data_path": data_path},
        )

    def _run(self, result):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            TextExtractor, "process_folder", return_value=result
        ):
            return self.supportai.create_ingest(
                "g", self._config(os.path.join(tmp, "uploads")), MagicMock()
            )

    def test_partial_failure_returns_the_failed_files(self):
        res = self._run({"statusCode": 200, "num_documents": 1, "files": [
            {"file_path": "/u/good.pdf", "status": "success", "jsonl_file": "good.jsonl", "num_documents": 1},
            {"file_path": "/u/bad.pdf", "status": "failed", "error": "unreadable"},
        ]})
        self.assertEqual(res["failed_files"], [{"file": "bad.pdf", "error": "unreadable"}])
        # Folder-wide, matching what ingest loads — not the latest upload's size.
        self.assertEqual(res["ready_files"], 1)

    def test_ready_count_matches_the_jsonl_files_ingest_loads(self):
        """Two sources sharing a stem convert to one JSONL, so ingest loads one."""
        res = self._run({"statusCode": 200, "num_documents": 2, "files": [
            {"file_path": "/u/report.pdf", "status": "success", "jsonl_file": "report.jsonl", "num_documents": 1},
            {"file_path": "/u/report.docx", "status": "success", "jsonl_file": "report.jsonl", "num_documents": 1},
            {"file_path": "/u/notes.txt", "status": "success", "jsonl_file": "notes.jsonl", "num_documents": 1},
        ]})
        self.assertEqual(res["ready_files"], 2)

    def test_file_with_no_documents_is_not_counted_ready(self):
        """A skipped decorative image converts to nothing, so ingest loads nothing
        for it — counting it would announce files ingest then can't find."""
        res = self._run({"statusCode": 200, "num_documents": 1, "files": [
            {"file_path": "/u/notes.txt", "status": "success",
             "jsonl_file": "notes.jsonl", "num_documents": 1},
            {"file_path": "/u/logo.png", "status": "success",
             "jsonl_file": "logo.jsonl", "num_documents": 0},
        ]})
        self.assertEqual(res["ready_files"], 1)

    def test_nothing_readable_raises_the_reasons(self):
        """Otherwise ingest later fails with an error naming an internal path."""
        with self.assertRaises(ExtractionError) as caught:
            self._run({"statusCode": 200, "num_documents": 0, "files": [
                {"file_path": "/u/bad.pdf", "status": "failed", "error": "unreadable"},
            ]})
        self.assertIn("bad.pdf", str(caught.exception))
        self.assertNotIn("/u/", str(caught.exception))


class TestPublicCreateIngestRoute(unittest.TestCase):
    """The API route pyTigerGraph uses must answer unreadable input with 400,
    matching the UI route, rather than a 500."""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
            from app.main import app
        except ImportError as exc:
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.client = TestClient(app)

    def test_nothing_readable_is_400_with_the_reasons(self):
        failed = {"statusCode": 200, "num_documents": 0, "files": [
            {"file_path": "/u/bad.pdf", "status": "failed",
             "error": "Could not read text from bad.pdf."},
        ]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            TextExtractor, "process_folder", return_value=failed
        ):
            resp = self.client.post(
                "/g/supportai/create_ingest",
                json={"data_source": "server", "file_format": "multi",
                      "data_source_config": {"data_path": os.path.join(tmp, "uploads")}},
                auth=("user", "pass"),
            )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("bad.pdf", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
